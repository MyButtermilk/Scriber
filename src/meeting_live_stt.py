"""Bounded dual-source live meeting transcription.

Durable audio persistence is intentionally upstream of this module. Queue
overflow may sacrifice live preview frames, but can never affect recorded audio.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect

from src.runtime.smart_turn_mel import install_smart_turn_mel_acceleration
from src.soniox_region import soniox_realtime_websocket_url


SONIOX_REALTIME_URL = soniox_realtime_websocket_url("us")
END_TOKENS = {"<end>", "<fin>"}


@dataclass(frozen=True)
class LiveMeetingSegment:
    id: str
    source: str
    text: str
    is_final: bool
    speaker_label: str
    start_ms: int
    end_ms: int
    provider_segment_id: str


@dataclass(frozen=True)
class _QueuedAudio:
    pcm: bytes
    timeline_start_ms: int


@dataclass(frozen=True)
class _SentTimelineSpan:
    provider_start_ms: float
    provider_end_ms: float
    meeting_start_ms: float
    meeting_end_ms: float


SegmentCallback = Callable[[LiveMeetingSegment], Awaitable[None]]
GapCallback = Callable[[str, str], Awaitable[None]]
StatusCallback = Callable[[str, str, int], Awaitable[None]]


def create_meeting_smart_turn_analyzer() -> Any:
    """Create Pipecat's bundled Smart Turn V3 analyzer without its heavyweight extra."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    try:
        install_smart_turn_mel_acceleration()
    except Exception:
        # Keep provider-independent endpoint detection available through the
        # numerically equivalent NumPy fallback. Frozen release gates require
        # the accelerated path, so this is only a runtime resilience boundary.
        pass
    analyzer = LocalSmartTurnAnalyzerV3(cpu_count=1)
    analyzer.set_sample_rate(16_000)
    return analyzer


def _pcm_has_speech(pcm: bytes, *, rms_threshold: int = 260) -> bool:
    if len(pcm) < 2:
        return False
    sample_count = len(pcm) // 2
    total = 0
    for offset in range(0, sample_count * 2, 2):
        sample = int.from_bytes(pcm[offset:offset + 2], "little", signed=True)
        total += sample * sample
    return (total / max(1, sample_count)) ** 0.5 >= rms_threshold


class SonioxMeetingStream:
    def __init__(
        self,
        *,
        meeting_id: str,
        source: str,
        api_key: str,
        model: str,
        language: str,
        diarization: bool,
        on_segment: SegmentCallback,
        on_gap: GapCallback,
        on_status: StatusCallback | None = None,
        queue_frames: int = 256,
        connect_factory: Callable[..., Any] = websocket_connect,
        session_id: str = "",
        timeline_offset_ms: int = 0,
        reconnect_initial_delay_s: float = 0.25,
        reconnect_max_delay_s: float = 5.0,
        smart_turn_analyzer: Any | None = None,
        stop_timeout_s: float = 10.0,
        realtime_url: str = SONIOX_REALTIME_URL,
    ) -> None:
        self.meeting_id = meeting_id
        self.source = source
        self.api_key = api_key
        self.model = model
        self.language = language
        self.diarization = diarization
        self.on_segment = on_segment
        self.on_gap = on_gap
        self.on_status = on_status
        self.connect_factory = connect_factory
        self.session_id = session_id or uuid4().hex
        self.timeline_offset_ms = max(0, int(timeline_offset_ms))
        self.queue: asyncio.Queue[_QueuedAudio | None] = asyncio.Queue(
            maxsize=max(16, queue_frames)
        )
        self.websocket: Any = None
        self.supervisor_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None
        self.receive_task: asyncio.Task | None = None
        self.final_tokens: list[dict[str, Any]] = []
        self.turn_index = 0
        self.dropped_frames = 0
        self.reconnect_count = 0
        self.reconnect_attempts = 0
        self._reconnect_initial_delay_s = max(0.01, reconnect_initial_delay_s)
        self._reconnect_max_delay_s = max(
            self._reconnect_initial_delay_s, reconnect_max_delay_s
        )
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._stop_sentinel_queued = False
        self._stop_timeout_s = min(30.0, max(0.05, float(stop_timeout_s)))
        self._backpressure_reported = False
        self._turn_emitted = False
        self._connection_timeline_offset_ms = self.timeline_offset_ms
        self._next_timeline_ms = float(self.timeline_offset_ms)
        self._provider_audio_cursor_ms = 0.0
        self._sent_timeline_spans: list[_SentTimelineSpan] = []
        self._interim_latency_samples_ms: list[int] = []
        self._speaker_epoch = 0
        self._speaker_numbers: dict[tuple[int, str], int] = {}
        self._next_speaker_number = 1
        self.smart_turn_analyzer = smart_turn_analyzer
        self.smart_turn_analyses = 0
        self.smart_turn_incomplete = 0
        self.smart_turn_failures = 0
        self.smart_turn_last_probability: float | None = None
        self.smart_turn_last_latency_ms: float | None = None
        self.realtime_url = realtime_url

    async def start(self) -> None:
        if self.supervisor_task is not None:
            return
        self._stopping = False
        self._stop_sentinel_queued = False
        self._stop_event.clear()
        websocket = await self._open_websocket()
        self.websocket = websocket
        self.supervisor_task = asyncio.create_task(
            self._connection_loop(websocket),
            name=f"meeting-live-supervisor-{self.source}",
        )

    async def _open_websocket(self) -> Any:
        websocket = await self.connect_factory(self.realtime_url)
        language_hints = [] if not self.language or self.language == "auto" else [self.language.split("-", 1)[0]]
        try:
            await websocket.send(json.dumps({
                "api_key": self.api_key,
                "model": self.model,
                "audio_format": "pcm_s16le",
                "num_channels": 1,
                "sample_rate": 16_000,
                "enable_endpoint_detection": True,
                "max_endpoint_delay_ms": 500,
                "language_hints": language_hints or None,
                "enable_speaker_diarization": self.diarization,
                "enable_language_identification": self.language == "auto",
                "client_reference_id": f"scriber-meeting-{self.meeting_id[:24]}-{self.source}",
            }))
        except Exception:
            await websocket.close()
            raise
        return websocket

    def enqueue(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self.smart_turn_analyzer is not None:
            try:
                self.smart_turn_analyzer.append_audio(pcm, _pcm_has_speech(pcm))
            except Exception:
                # Smart Turn refines preview boundaries only. It must never
                # interrupt provider streaming or durable audio capture.
                self.smart_turn_failures += 1
                self.smart_turn_analyzer = None
        frame = _QueuedAudio(
            pcm=bytes(pcm),
            timeline_start_ms=int(self._next_timeline_ms),
        )
        self._next_timeline_ms += len(pcm) / 32.0
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped_frames += 1
        if self.dropped_frames and not self._backpressure_reported:
            self._backpressure_reported = True
            asyncio.create_task(
                self._report_backpressure(),
                name=f"meeting-live-backpressure-{self.source}",
            )

    async def _report_backpressure(self) -> None:
        await self.on_gap(self.source, "live_stt_backpressure")
        if self.on_status is not None:
            await self.on_status(self.source, "degraded", self.reconnect_count)

    def _enqueue_stop_sentinel(self) -> None:
        """Request preview shutdown without ever waiting on a full audio queue.

        Durable Meeting audio is persisted before it reaches this best-effort
        preview queue. Sacrificing one queued preview frame is therefore safer
        than allowing finalization or app shutdown to wait indefinitely.
        """
        if self._stop_sentinel_queued:
            return
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                discarded = self.queue.get_nowait()
                self.queue.task_done()
                if discarded is not None:
                    self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(None)
        self._stop_sentinel_queued = True

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        try:
            task.exception()
        except BaseException:
            pass

    async def _cleanup_until(
        self,
        *,
        supervisor_task: asyncio.Task,
        websocket: Any,
        deadline: float,
        force: bool,
    ) -> None:
        supervised_tasks = {
            task
            for task in (supervisor_task, self.send_task, self.receive_task)
            if task is not None
        }
        if force:
            for task in supervised_tasks:
                if not task.done():
                    task.cancel()

        cleanup_tasks = set(supervised_tasks)
        if websocket is not None:
            close_task = asyncio.create_task(
                websocket.close(),
                name=f"meeting-live-close-{self.source}",
            )
            cleanup_tasks.add(close_task)

        pending = {task for task in cleanup_tasks if not task.done()}
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if pending and remaining > 0:
            _, pending = await asyncio.wait(pending, timeout=remaining)

        for task in cleanup_tasks - pending:
            self._consume_task_result(task)
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_task_result)

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_timeout_s
        # Keep part of the one total stop budget for forced task cancellation
        # and WebSocket closure if graceful provider finalization stalls.
        cleanup_reserve_s = min(1.0, max(0.01, self._stop_timeout_s * 0.2))
        graceful_deadline = deadline - cleanup_reserve_s
        task = self.supervisor_task
        if task is None:
            return
        self._stopping = True
        self._stop_event.set()
        self._enqueue_stop_sentinel()

        if self.dropped_frames and not self._backpressure_reported:
            self._backpressure_reported = True
            report_timeout = max(0.0, graceful_deadline - loop.time())
            if report_timeout > 0:
                try:
                    await asyncio.wait_for(
                        self._report_backpressure(), timeout=report_timeout
                    )
                except Exception:
                    # Preview degradation reporting is best-effort. A callback
                    # failure must never bypass supervised task/WebSocket cleanup.
                    pass

        graceful = False
        task_error: BaseException | None = None
        try:
            remaining = max(0.0, graceful_deadline - loop.time())
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            graceful = True
        except asyncio.TimeoutError:
            pass
        except BaseException as exc:
            task_error = exc
        finally:
            websocket = self.websocket
            await self._cleanup_until(
                supervisor_task=task,
                websocket=websocket,
                deadline=deadline,
                force=not graceful,
            )
            self.supervisor_task = None
            self.send_task = None
            self.receive_task = None
            self.websocket = None
        if task_error is not None:
            raise task_error

    async def _connection_loop(self, websocket: Any) -> None:
        outage_reported = False
        delay = self._reconnect_initial_delay_s
        try:
            while True:
                self.websocket = websocket
                self._connection_timeline_offset_ms = int(self._next_timeline_ms)
                self._provider_audio_cursor_ms = 0.0
                self._sent_timeline_spans = []
                self.send_task = asyncio.create_task(
                    self._send_loop(websocket), name=f"meeting-live-send-{self.source}"
                )
                self.receive_task = asyncio.create_task(
                    self._receive_loop(websocket),
                    name=f"meeting-live-receive-{self.source}",
                )
                done, pending = await asyncio.wait(
                    {self.send_task, self.receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stopping_from_queue = False
                failure: BaseException | None = None
                if self.send_task in done:
                    try:
                        stopping_from_queue = await self.send_task == "stop"
                    except BaseException as exc:  # task failures must be observed
                        failure = exc

                if stopping_from_queue:
                    try:
                        await websocket.send("")
                        await asyncio.wait_for(self.receive_task, timeout=8.0)
                    except BaseException:
                        self.receive_task.cancel()
                        await asyncio.gather(self.receive_task, return_exceptions=True)
                    await self._emit_final()
                    return

                if self.receive_task in done:
                    try:
                        await self.receive_task
                    except BaseException as exc:  # task failures must be observed
                        failure = failure or exc

                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await websocket.close()
                if self._stopping:
                    return

                await self._finish_interrupted_turn()
                if not outage_reported:
                    await self.on_gap(self.source, "live_stt_reconnect")
                    outage_reported = True
                    self.reconnect_count += 1
                    if self.on_status is not None:
                        await self.on_status(
                            self.source, "reconnecting", self.reconnect_count
                        )

                websocket = None
                while websocket is None and not self._stopping:
                    self.reconnect_attempts += 1
                    try:
                        # Soniox speaker identifiers are scoped to one
                        # WebSocket session. Never silently merge a reused raw
                        # identifier from a reconnect with the earlier person.
                        self._speaker_epoch += 1
                        websocket = await self._open_websocket()
                    except Exception:
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        delay = min(delay * 2.0, self._reconnect_max_delay_s)
                if websocket is None:
                    return
                if self.on_status is not None:
                    await self.on_status(self.source, "recovered", self.reconnect_count)
                outage_reported = False
                delay = self._reconnect_initial_delay_s
        finally:
            for task in (self.send_task, self.receive_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self.send_task, self.receive_task) if task is not None),
                return_exceptions=True,
            )

    async def _finish_interrupted_turn(self) -> None:
        if self.final_tokens:
            await self._emit_final()
        elif self._turn_emitted:
            self.turn_index += 1
            self._turn_emitted = False

    async def _send_loop(self, websocket: Any) -> str:
        first_frame = True
        while True:
            frame = await self.queue.get()
            try:
                if frame is None:
                    return "stop"
                if first_frame:
                    self._connection_timeline_offset_ms = frame.timeline_start_ms
                    first_frame = False
                await websocket.send(frame.pcm)
                self._record_sent_frame(frame)
            finally:
                self.queue.task_done()

    def _record_sent_frame(self, frame: _QueuedAudio) -> None:
        """Map contiguous provider audio back to the possibly gapped Meeting clock."""
        duration_ms = len(frame.pcm) / 32.0
        provider_start = self._provider_audio_cursor_ms
        provider_end = provider_start + duration_ms
        meeting_start = float(frame.timeline_start_ms)
        meeting_end = meeting_start + duration_ms
        previous = self._sent_timeline_spans[-1] if self._sent_timeline_spans else None
        if (
            previous is not None
            and abs(previous.provider_end_ms - provider_start) < 0.001
            and abs(previous.meeting_end_ms - meeting_start) < 0.501
        ):
            self._sent_timeline_spans[-1] = _SentTimelineSpan(
                provider_start_ms=previous.provider_start_ms,
                provider_end_ms=provider_end,
                meeting_start_ms=previous.meeting_start_ms,
                meeting_end_ms=meeting_end,
            )
        else:
            self._sent_timeline_spans.append(_SentTimelineSpan(
                provider_start_ms=provider_start,
                provider_end_ms=provider_end,
                meeting_start_ms=meeting_start,
                meeting_end_ms=meeting_end,
            ))
        self._provider_audio_cursor_ms = provider_end

    def _meeting_time_for_provider_ms(self, value: int, *, endpoint: str) -> int:
        """Translate one provider timestamp through sent-audio gaps.

        Start timestamps use the span to the right of an exact discontinuity;
        end timestamps use the span to the left. This preserves a dropped-frame
        gap instead of attributing the next word to the pre-gap clock.
        """
        provider_ms = max(0.0, float(value))
        spans = self._sent_timeline_spans
        if not spans:
            return self._connection_timeline_offset_ms + round(provider_ms)
        if endpoint == "start":
            for span in spans:
                if span.provider_start_ms <= provider_ms < span.provider_end_ms:
                    return round(
                        span.meeting_start_ms
                        + provider_ms - span.provider_start_ms
                    )
        else:
            for span in spans:
                if span.provider_start_ms < provider_ms <= span.provider_end_ms:
                    return round(
                        span.meeting_start_ms
                        + provider_ms - span.provider_start_ms
                    )
            if provider_ms == spans[0].provider_start_ms:
                return round(spans[0].meeting_start_ms)
        if provider_ms < spans[0].provider_start_ms:
            return round(spans[0].meeting_start_ms)
        last = spans[-1]
        return round(last.meeting_end_ms + provider_ms - last.provider_end_ms)

    async def _receive_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            content = json.loads(raw)
            tokens = content.get("tokens") if isinstance(content.get("tokens"), list) else []
            interim_tokens: list[dict[str, Any]] = []
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                text = str(token.get("text", ""))
                if token.get("is_final"):
                    if text in END_TOKENS:
                        await self._finish_provider_turn()
                    else:
                        self.final_tokens.append(token)
                elif text not in END_TOKENS:
                    interim_tokens.append(token)
            preview_tokens = self.final_tokens + interim_tokens
            if preview_tokens:
                await self._emit(preview_tokens, is_final=False)
            if content.get("error_code") or content.get("error_message"):
                await self._emit_final()
                raise RuntimeError(str(content.get("error_code") or "soniox_live_error"))
            if content.get("finished"):
                await self._emit_final()
                return

    async def _emit_final(self) -> None:
        if self.final_tokens:
            await self._emit(self.final_tokens, is_final=True)
            self.final_tokens = []
            self.turn_index += 1
            self._turn_emitted = False
        if self.smart_turn_analyzer is not None:
            try:
                self.smart_turn_analyzer.clear()
            except Exception:
                self.smart_turn_failures += 1

    async def _finish_provider_turn(self) -> None:
        if not self.final_tokens:
            return
        analyzer = self.smart_turn_analyzer
        if analyzer is None:
            await self._emit_final()
            return
        try:
            state, metrics = await analyzer.analyze_end_of_turn()
            self.smart_turn_analyses += 1
            probability = getattr(metrics, "probability", None)
            latency_ms = getattr(metrics, "e2e_processing_time_ms", None)
            self.smart_turn_last_probability = (
                float(probability) if isinstance(probability, (int, float)) else None
            )
            self.smart_turn_last_latency_ms = (
                round(float(latency_ms), 2) if isinstance(latency_ms, (int, float)) else None
            )
            if str(getattr(state, "name", state)).lower() == "incomplete":
                self.smart_turn_incomplete += 1
                await self._emit(self.final_tokens, is_final=False)
                return
        except Exception:
            self.smart_turn_failures += 1
            self.smart_turn_analyzer = None
        await self._emit_final()

    @staticmethod
    def _contiguous_speaker_runs(
        tokens: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        runs: list[list[dict[str, Any]]] = []
        leading_speakerless: list[dict[str, Any]] = []
        current_speaker: str | None = None
        for token in tokens:
            raw_speaker = token.get("speaker")
            speaker = str(raw_speaker).strip() if raw_speaker is not None else ""
            has_visible_text = bool(str(token.get("text", "")).strip())
            if not speaker or not has_visible_text:
                if runs:
                    runs[-1].append(token)
                else:
                    leading_speakerless.append(token)
                continue
            if runs and speaker != current_speaker:
                runs.append([])
            elif not runs:
                runs.append(leading_speakerless)
                leading_speakerless = []
            runs[-1].append(token)
            current_speaker = speaker
        if leading_speakerless:
            runs.append(leading_speakerless)
        return [run for run in runs if run]

    def _speaker_label(self, tokens: list[dict[str, Any]]) -> str:
        if self.source == "microphone":
            return "You"
        ordered_speakers = [
            str(token.get("speaker")).strip()
            for token in tokens
            if str(token.get("text", "")).strip()
            and token.get("speaker") is not None
            and str(token.get("speaker")).strip()
        ]
        speakers = Counter(ordered_speakers)
        if not speakers:
            return "Meeting audio"
        for ordered_raw_speaker in ordered_speakers:
            ordered_key = (self._speaker_epoch, ordered_raw_speaker)
            if ordered_key not in self._speaker_numbers:
                self._speaker_numbers[ordered_key] = self._next_speaker_number
                self._next_speaker_number += 1
        raw_speaker = speakers.most_common(1)[0][0]
        key = (self._speaker_epoch, raw_speaker)
        number = self._speaker_numbers.get(key)
        if number is None:  # defensive; the ordered pass above should own it
            number = self._next_speaker_number
            self._speaker_numbers[key] = number
            self._next_speaker_number += 1
        return f"Speaker {number}"

    async def _emit(self, tokens: list[dict[str, Any]], *, is_final: bool) -> None:
        runs = (
            self._contiguous_speaker_runs(tokens)
            if is_final and self.diarization
            else [tokens]
        )
        for run_index, run in enumerate(runs):
            await self._emit_run(run, is_final=is_final, run_index=run_index)

    async def _emit_run(
        self,
        tokens: list[dict[str, Any]],
        *,
        is_final: bool,
        run_index: int,
    ) -> None:
        text = "".join(str(token.get("text", "")) for token in tokens).strip()
        if not text:
            return
        start_ms_values = [int(token["start_ms"]) for token in tokens if token.get("start_ms") is not None]
        end_ms_values = [int(token["end_ms"]) for token in tokens if token.get("end_ms") is not None]
        label = self._speaker_label(tokens)
        stable_base = f"live-{self.source}-{self.session_id[:8]}-{self.turn_index}"
        stable_id = stable_base if run_index == 0 else f"{stable_base}-r{run_index}"
        absolute_start_ms = self._meeting_time_for_provider_ms(
            min(start_ms_values, default=0), endpoint="start"
        )
        absolute_end_ms = self._meeting_time_for_provider_ms(
            max(end_ms_values, default=0), endpoint="end"
        )
        if not is_final and end_ms_values:
            latency_ms = max(0, round(self._next_timeline_ms - absolute_end_ms))
            self._interim_latency_samples_ms.append(latency_ms)
            if len(self._interim_latency_samples_ms) > 4096:
                del self._interim_latency_samples_ms[:2048]
        self._turn_emitted = True
        await self.on_segment(LiveMeetingSegment(
            id=stable_id,
            source=self.source,
            text=text,
            is_final=is_final,
            speaker_label=label,
            start_ms=absolute_start_ms,
            end_ms=absolute_end_ms,
            provider_segment_id=stable_id,
        ))

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "droppedFrames": self.dropped_frames,
            "reconnectCount": self.reconnect_count,
            "reconnectAttempts": self.reconnect_attempts,
            "queueDepth": self.queue.qsize(),
            "interimLatencySampleCount": len(self._interim_latency_samples_ms),
            "interimLatencyP95Ms": _percentile_95(self._interim_latency_samples_ms),
            "timelineCursorMs": round(self._next_timeline_ms),
            "smartTurn": {
                "enabled": self.smart_turn_analyzer is not None or self.smart_turn_analyses > 0,
                "engine": "Pipecat local",
                "model": "Smart Turn V3",
                "analyses": self.smart_turn_analyses,
                "incompleteTurns": self.smart_turn_incomplete,
                "failures": self.smart_turn_failures,
                "lastProbability": self.smart_turn_last_probability,
                "lastLatencyMs": self.smart_turn_last_latency_ms,
            },
        }


class MeetingLiveTranscriber:
    def __init__(
        self,
        *,
        meeting_id: str,
        api_key: str,
        model: str,
        language: str,
        on_segment: SegmentCallback,
        on_gap: GapCallback,
        on_status: StatusCallback | None = None,
        connect_factory: Callable[..., Any] = websocket_connect,
        timeline_offsets: dict[str, int] | None = None,
        smart_turn_analyzer: Any | None = None,
        realtime_url: str = SONIOX_REALTIME_URL,
    ) -> None:
        self.loop = asyncio.get_running_loop()
        session_id = uuid4().hex
        timeline_offsets = timeline_offsets or {}
        self.streams = {
            source: SonioxMeetingStream(
                meeting_id=meeting_id,
                source=source,
                api_key=api_key,
                model=model,
                language=language,
                diarization=source == "system",
                on_segment=on_segment,
                on_gap=on_gap,
                on_status=on_status,
                connect_factory=connect_factory,
                session_id=session_id,
                timeline_offset_ms=int(timeline_offsets.get(source, 0)),
                smart_turn_analyzer=smart_turn_analyzer if source == "microphone" else None,
                realtime_url=realtime_url,
            )
            for source in ("microphone", "system")
        }

    async def start(self) -> None:
        started: list[SonioxMeetingStream] = []
        try:
            for stream in self.streams.values():
                await stream.start()
                started.append(stream)
        except Exception:
            await asyncio.gather(*(stream.stop() for stream in started), return_exceptions=True)
            raise

    def enqueue_from_thread(self, source: str, pcm: bytes) -> None:
        stream = self.streams.get(source)
        if stream is not None:
            self.loop.call_soon_threadsafe(stream.enqueue, pcm)

    async def stop(self) -> None:
        await asyncio.gather(*(stream.stop() for stream in self.streams.values()), return_exceptions=True)

    def snapshot(self) -> dict[str, Any]:
        streams = {source: stream.snapshot() for source, stream in self.streams.items()}
        all_latencies = [
            value
            for stream in self.streams.values()
            for value in stream._interim_latency_samples_ms
        ]
        return {
            "streams": streams,
            "droppedFrames": sum(item["droppedFrames"] for item in streams.values()),
            "reconnectCount": sum(item["reconnectCount"] for item in streams.values()),
            "reconnectAttempts": sum(item["reconnectAttempts"] for item in streams.values()),
            "interimLatencySampleCount": len(all_latencies),
            "interimLatencyP95Ms": _percentile_95(all_latencies),
        }


def _percentile_95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return ordered[min(index, len(ordered) - 1)]

"""Muse Voice Transcribe, using Meta's documented Voice API (2026-09-02).

HTTP uploads and WebSocket sessions are distinct contracts. Live dictation uses
ENDPOINTING without diarization; cumulative hypotheses are previews only. The
session ends with endStream, followed by draining results through close 1000.
No request is automatically replayed after audio may have reached Meta.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import wave
from collections.abc import Callable
from typing import Any, BinaryIO

import aiohttp
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601

from src.core.provider_errors import provider_transport_error
from src.runtime.audio_spool import append_pcm_frame, close_pcm_spool, create_pcm_spool, pcm_stream_to_wav
from src.runtime.http_response import read_response_json_limited
from src.runtime.task_supervisor import AsyncTaskSupervisor

META_STT_MODEL = "muse-voice-transcribe-1.0"
META_STT_BATCH_URL = "https://api.meta.ai/v1/asr/transcribe"
META_STT_REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
# The 32 MB cap covers multipart overhead as well as the audio part.
META_STT_MAX_AUDIO_BYTES = 32_000_000 - 65_536
META_STT_MAX_SECONDS = 600
META_STT_REALTIME_MAX_SECONDS = 3600
_LANGUAGES = {
    "ar": "Arabic",
    "bn": "Bengali",
    "nl": "Dutch",
    "en": "English",
    "fr": "French",
    "de": "German",
    "he": "Hebrew",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "ko": "Korean",
    "ms": "Malay",
    "zh": "Mandarin Chinese",
    "mr": "Marathi",
    "pl": "Polish",
    "pt": "Portuguese",
    "es": "Spanish",
    "tl": "Tagalog",
    "fil": "Tagalog",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "vi": "Vietnamese",
}


def meta_request_options(*, model: str, language: Any = None, custom_vocab: str = "") -> dict[str, Any]:
    if model != META_STT_MODEL:
        raise ValueError("Meta STT model is not verified. Select muse-voice-transcribe-1.0.")
    options: dict[str, Any] = {"model": model}
    raw = str(getattr(language, "value", language) or "").strip().replace("_", "-")
    if raw and raw.lower() != "auto":
        name = _LANGUAGES.get(raw.lower().split("-", 1)[0])
        if name is None:
            raise ValueError("Meta STT language is not supported. Select automatic language detection.")
        options["languageBias"] = [name]
    keywords = list(dict.fromkeys(term.strip() for term in custom_vocab.split(",") if term.strip()))
    if keywords:
        options["keywords"] = keywords
    if len(json.dumps(options).encode("utf-8")) > 32_768:
        raise ValueError("Meta STT vocabulary exceeds Scriber's 32 KB request-settings limit.")
    return options


def validate_meta_wav(source: BinaryIO) -> None:
    """Validate without consuming or closing the caller-owned upload stream."""
    position = source.tell()
    try:
        source.seek(0, 2)
        if source.tell() > META_STT_MAX_AUDIO_BYTES:
            raise ValueError("Meta STT audio exceeds the 32 MB multipart limit.")
        source.seek(0)
        with wave.open(source, "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() not in (16000, 24000):
                raise ValueError("Meta STT requires mono PCM16 WAV at 16 or 24 kHz.")
            if audio.getnframes() > META_STT_MAX_SECONDS * audio.getframerate():
                raise ValueError("Meta STT recordings must not exceed 10 minutes.")
            if audio.getnframes() == 0:
                raise ValueError("Meta STT recording is empty.")
            audio.setpos(audio.getnframes() - 1)
            if len(audio.readframes(1)) != 2:
                raise ValueError("Meta STT WAV data is truncated.")
    except wave.Error, EOFError:
        raise ValueError("Meta STT requires a valid PCM16 WAV recording.") from None
    finally:
        source.seek(position)


def _final_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("transcript"), str):
        raise RuntimeError("Meta STT returned an invalid final transcript.")
    duration = payload.get("audioDurationMs")
    turns = payload.get("turns")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0 or not isinstance(turns, list):
        raise RuntimeError("Meta STT returned invalid recording metadata.")
    clean_turns = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("transcript"), str):
            raise RuntimeError("Meta STT returned an invalid turn.")
        if any(type(turn.get(key)) is not int for key in ("turnId", "startMs", "endMs")):
            raise RuntimeError("Meta STT returned invalid turn timing.")
        if not 0 <= turn["startMs"] <= turn["endMs"] <= duration:
            raise RuntimeError("Meta STT returned invalid turn timing.")
        clean = {key: turn[key] for key in ("turnId", "startMs", "endMs", "transcript")}
        if isinstance(turn.get("speaker"), str):
            clean["speaker"] = turn["speaker"]
        clean_turns.append(clean)
    return {"transcript": payload["transcript"], "audioDurationMs": duration, "turns": clean_turns}


async def transcribe_with_meta(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    model: str = META_STT_MODEL,
    language: Any = None,
    custom_vocab: str = "",
    mode: str = "ENDPOINTING",
    timeout_secs: float = 600,
    endpoint: str = META_STT_BATCH_URL,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError("Meta API key is missing. Add the Meta Model API key in Settings.")
    if mode not in {"PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION"}:
        raise ValueError("Invalid Meta STT mode.")
    options = meta_request_options(model=model, language=language, custom_vocab=custom_vocab)
    options.update(audioEncoding="WAV", mode=mode)
    source = io.BytesIO(audio_source) if isinstance(audio_source, bytes) else audio_source
    validate_meta_wav(source)
    source.seek(0)
    data = aiohttp.FormData()
    data.add_field("request", json.dumps(options), content_type="application/json")
    data.add_field("audio", source, filename="audio.wav", content_type="audio/wav")
    try:
        async with session.post(
            endpoint,
            data=data,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=max(1, timeout_secs)),
        ) as response:
            if response.status != 200:
                # Never include response bodies, credentials, or transcript echoes in errors.
                raise provider_transport_error("meta_stt_async", "transcribe", status=response.status)
            payload = await read_response_json_limited(response, 4 * 1024 * 1024)
    except (aiohttp.ClientError, TimeoutError) as exc:
        status = getattr(exc, "status", None)
        raise provider_transport_error("meta_stt_async", "transcribe", status=status or None) from None
    return _final_payload(payload)


class MetaAsyncProcessor(FrameProcessor):
    """One bounded recording, one asynchronous HTTP upload on normal stop."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        api_key: str,
        model: str = META_STT_MODEL,
        language: Any = None,
        custom_vocab: str = "",
        on_progress: Callable[[str], None] | None = None,
    ):
        super().__init__()
        self._session = session
        self._api_key = api_key
        self._options = dict(model=model, language=language, custom_vocab=custom_vocab)
        meta_request_options(**self._options)
        self._on_progress = on_progress
        self._buffer = create_pcm_spool(reserve_wav_header=True)
        self._size = 0
        self._rate: int | None = None
        self._failed = False
        self._finished = False

    async def cleanup(self):
        close_pcm_spool(self._buffer)
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        try:
            if isinstance(frame, AudioRawFrame) and not self._failed and not self._finished:
                if frame.sample_rate not in (16000, 24000) or frame.num_channels != 1 or len(frame.audio) % 2:
                    raise ValueError("Meta STT requires mono PCM16 at 16 or 24 kHz.")
                if self._rate is not None and self._rate != frame.sample_rate:
                    raise ValueError("Meta STT audio format changed within a recording.")
                self._rate = frame.sample_rate
                if self._size + len(frame.audio) > self._rate * 2 * META_STT_MAX_SECONDS:
                    raise ValueError("Meta STT recordings must not exceed 10 minutes.")
                self._size = await append_pcm_frame(self._buffer, self._size, frame.audio)
            elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)) and not self._finished:
                self._finished = True
                try:
                    if (
                        not isinstance(frame, CancelFrame)
                        and not self._failed
                        and self._size
                        and not getattr(self, "_skip_terminal_transcription", False)
                    ):
                        if self._on_progress:
                            self._on_progress("Transcribing...")
                        wav = await asyncio.to_thread(
                            pcm_stream_to_wav,
                            self._buffer,
                            self._rate,
                            1,
                            reserved_wav_header=True,
                            pcm_size=self._size,
                        )
                        try:
                            result = await transcribe_with_meta(
                                session=self._session,
                                api_key=self._api_key,
                                audio_source=wav,
                                mode="PUSH_TO_TALK",
                                **self._options,
                            )
                        finally:
                            wav.close()
                        if result["transcript"].strip():
                            await self.push_frame(
                                TranscriptionFrame(
                                    text=result["transcript"].strip(),
                                    user_id="user",
                                    timestamp=time_now_iso8601(),
                                    finalized=True,
                                    result=None,
                                ),
                                direction,
                            )
                finally:
                    close_pcm_spool(self._buffer)
        except asyncio.CancelledError:
            close_pcm_spool(self._buffer)
            raise
        except Exception as exc:
            self._failed = True
            close_pcm_spool(self._buffer)
            # Our own validation errors and transport errors are already content-free.
            from src.core.provider_errors import ProviderTransportError

            message = (
                str(exc) if isinstance(exc, (ValueError, ProviderTransportError)) else "Meta STT transcription failed."
            )
            await self.push_frame(ErrorFrame(error=message), direction)
        await self.push_frame(frame, direction)


class MetaRealtimeSTTService(FrameProcessor):
    """One authenticated socket per recording; only completed turns are final."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        api_key: str,
        model: str = META_STT_MODEL,
        language: Any = None,
        custom_vocab: str = "",
        sample_rate: int = 16000,
        channels: int = 1,
        endpoint: str = META_STT_REALTIME_URL,
    ):
        super().__init__()
        if sample_rate not in (16000, 24000) or channels != 1:
            raise ValueError("Meta STT requires mono PCM16 at 16 or 24 kHz.")
        self._options = meta_request_options(model=model, language=language, custom_vocab=custom_vocab)
        self._session, self._api_key, self._endpoint = session, api_key, endpoint
        self._rate = sample_rate
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tasks = AsyncTaskSupervisor(owner="meta_stt")
        self._terminal = asyncio.Event()
        self._failed = False
        self._ending = False
        self._closed = False
        self._started = False
        self._audio_bytes = 0
        self._audio_started: float | None = None
        self._turn_order: list[int] = []
        self._completed: dict[int, str] = {}
        self._seen: set[int] = set()
        self._final_timeout = 30.0

    async def _error(self, message: str, direction: FrameDirection) -> None:
        if not self._failed:
            self._failed = True
            await self.push_frame(ErrorFrame(error=f"Meta STT realtime: {message}"), direction)

    async def _connect(self, direction: FrameDirection) -> None:
        if self._started or self._closed:
            return
        self._started = True
        if not self._api_key:
            await self._error("API key is missing.", direction)
            return
        try:
            async with asyncio.timeout(30):
                self._ws = await self._session.ws_connect(self._endpoint, heartbeat=None, max_msg_size=4 * 1024 * 1024)
            async with asyncio.timeout(10):
                await self._ws.send_json(
                    {
                        **self._options,
                        "authorization": {"accessToken": f"Bearer {self._api_key}"},
                        "audioEncoding": f"PCM_{self._rate // 1000}KHZ",
                        "mode": "ENDPOINTING",
                        "partialMode": "CUMULATIVE",
                        "emitAudioProgress": False,
                    }
                )
                ack = await self._ws.receive_json()
            if (
                not isinstance(ack, dict)
                or "type" in ack
                or not isinstance(ack.get("sessionId"), str)
                or not ack["sessionId"]
            ):
                await self._error("handshake rejected; check Meta Model API access.", direction)
                await self._ws.close()
                return
            self._tasks.spawn(self._receive(direction), name="meta_stt_receive")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            await self._error(
                str(provider_transport_error("meta_stt", "connect", status=getattr(exc, "status", None))), direction
            )
            if self._ws is not None:
                await self._ws.close()

    async def _handle_event(self, payload: Any, direction: FrameDirection) -> None:
        if not isinstance(payload, dict):
            raise ValueError("invalid event")
        kind = payload.get("type")
        if kind == "error":
            await self._error("provider rejected the stream; check access, limits and audio format.", direction)
        elif kind == "speechStart":
            turn_id = payload.get("turnId")
            if type(turn_id) is not int:
                raise ValueError("invalid turn")
            if turn_id not in self._seen and turn_id not in self._turn_order:
                self._turn_order.append(turn_id)
        elif kind == "transcript":
            text = payload.get("transcript")
            if not isinstance(text, str):
                raise ValueError("invalid transcript")
            # final:true is NOT turn completion in ENDPOINTING mode.
            if text and not self._ending:
                await self.push_frame(
                    InterimTranscriptionFrame(text=text, user_id="user", timestamp=time_now_iso8601()), direction
                )
        elif kind == "speechComplete":
            turn_id, text = payload.get("turnId"), payload.get("transcript")
            if type(turn_id) is not int or not isinstance(text, str):
                raise ValueError("invalid completion")
            if turn_id in self._seen:
                return
            if turn_id not in self._turn_order:
                raise ValueError("completion without speech start")
            self._completed[turn_id] = text.strip()
            while self._turn_order and self._turn_order[0] in self._completed:
                completed_id = self._turn_order.pop(0)
                final = self._completed.pop(completed_id)
                self._seen.add(completed_id)
                if final:
                    await self.push_frame(
                        TranscriptionFrame(
                            text=final, user_id="user", timestamp=time_now_iso8601(), finalized=True, result=None
                        ),
                        direction,
                    )
        # speechEnd is a boundary, not final text. Unknown events are additive.

    async def _receive(self, direction: FrameDirection) -> None:
        try:
            assert self._ws is not None
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(message.data), direction)
                    if self._failed:
                        break
                elif message.type == aiohttp.WSMsgType.ERROR:
                    break
            if (
                not self._closed
                and not self._failed
                and (self._ws.close_code != 1000 or not self._ending or self._turn_order)
            ):
                status = {1008: 400, 1011: 500, 1013: 429}.get(self._ws.close_code)
                message = (
                    str(provider_transport_error("meta_stt", "realtime", status=status))
                    if status
                    else "stream closed before all turns completed."
                )
                await self._error(message, direction)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._error("invalid response or interrupted connection.", direction)
        finally:
            self._terminal.set()

    async def _close(self, *, finalize: bool, direction: FrameDirection) -> None:
        if self._closed:
            return
        self._ending = True
        try:
            if finalize and not self._failed and self._ws is not None and not self._ws.closed:
                await self._ws.send_json({"type": "endStream"})
                try:
                    await asyncio.wait_for(self._terminal.wait(), self._final_timeout)
                except TimeoutError:
                    await self._error("timed out waiting for final transcription.", direction)
        finally:
            self._closed = True
            await self._tasks.close(timeout_seconds=2, cancel=True)
            if self._ws is not None:
                await self._ws.close()

    async def cleanup(self):
        await self._close(finalize=False, direction=FrameDirection.DOWNSTREAM)
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        try:
            if isinstance(frame, StartFrame):
                await self._connect(direction)
            elif isinstance(frame, AudioRawFrame) and not self._ending and not self._failed:
                await self._connect(direction)
                if frame.sample_rate != self._rate or frame.num_channels != 1 or len(frame.audio) % 2:
                    raise ValueError("invalid audio format")
                if self._audio_bytes + len(frame.audio) > self._rate * 2 * META_STT_REALTIME_MAX_SECONDS:
                    await self._error("60-minute session limit reached; start a new recording.", direction)
                elif not self._failed and self._ws is not None:
                    # Pace the prebuffer as well as normal frames; Meta rejects burst uploads.
                    if self._audio_started is None:
                        self._audio_started = time.monotonic()
                    delay = self._audio_started + self._audio_bytes / (self._rate * 2) - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await self._ws.send_bytes(frame.audio)
                    self._audio_bytes += len(frame.audio)
            elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
                await self._close(finalize=not isinstance(frame, CancelFrame), direction=direction)
        except asyncio.CancelledError:
            await self._close(finalize=False, direction=direction)
            raise
        except Exception:
            await self._error("audio send or shutdown failed.", direction)
            await self._close(finalize=False, direction=direction)
        await self.push_frame(frame, direction)

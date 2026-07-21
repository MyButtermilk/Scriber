"""Modulate Velma-2 multilingual speech-to-text adapters.

Scriber intentionally uses only the transcription surface of Modulate's API:

* batch responses are reduced to their top-level final transcript and duration;
* streaming requests disable partial results and emit only finalized text;
* diarization and every enrichment/signal are disabled, so utterance metadata,
  emotion, accent, deepfake, and PII/PHI data never enter Scriber state.

The streaming API requires the credential in the WebSocket query string.  No
URL containing that query string is logged, and every provider error is passed
through the local credential redactor before it reaches logs or an ErrorFrame.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from typing import Any, BinaryIO, Callable
from urllib.parse import urlencode

import aiohttp
from aiohttp import WSMsgType
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.runtime.audio_spool import (
    append_pcm_frame,
    close_pcm_spool,
    create_pcm_spool,
    pcm_stream_to_wav,
)
from src.runtime.env_values import env_float
from src.runtime.http_response import read_response_text_limited


MODULATE_BATCH_MODEL = "velma-2-stt-batch"
MODULATE_STREAMING_MODEL = "velma-2-stt-streaming"
# Keep these hosts aligned with Modulate's public API reference.  The account
# dashboard lives on platform.modulate.ai, but provider traffic is served by
# modulate-developer-apis.com.
MODULATE_BATCH_URL = (
    f"https://modulate-developer-apis.com/api/{MODULATE_BATCH_MODEL}"
)
MODULATE_STREAMING_URL = (
    f"wss://modulate-developer-apis.com/api/{MODULATE_STREAMING_MODEL}"
)
MODULATE_BATCH_MAX_BYTES = 100 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_PUBLIC_ERROR_CHARS = 500
_QUERY_SECRET_RE = re.compile(r"(?i)(api_key=)[^&\s]+")


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def modulate_language_code(language: Language | str | None) -> str:
    """Return an optional ISO-639 primary language hint for Modulate."""
    if not language:
        return ""
    raw = str(language.value if isinstance(language, Language) else language).strip()
    if not raw or raw.lower() == "auto":
        return ""
    return raw.replace("_", "-").split("-", 1)[0].lower()


def redact_modulate_error(value: object, api_key: str) -> str:
    """Bound and redact provider errors before logging or surfacing them."""
    text = " ".join(str(value or "Modulate request failed").split())
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    return text[:_MAX_PUBLIC_ERROR_CHARS]


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if not on_progress:
        return
    try:
        on_progress(message)
    except Exception:
        pass


def _final_transcript_payload(payload: Any) -> dict[str, Any]:
    """Keep only non-enriched, top-level final transcript fields.

    Modulate's multilingual response includes an ``utterances`` collection even
    when all enrichments are disabled.  The product requirement is final text
    only, so that collection and any future signal fields are discarded at the
    provider boundary instead of being persisted accidentally.
    """
    if not isinstance(payload, dict):
        raise RuntimeError("Modulate returned an invalid transcription response.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Modulate returned no final transcript field.")
    result: dict[str, Any] = {"text": text}
    duration = payload.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        result["duration_ms"] = int(duration)
    return result


def modulate_transcript_payload_to_text(payload: Any) -> str:
    """Read only Modulate's top-level final transcript string."""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    return text.strip() if isinstance(text, str) else ""


async def transcribe_with_modulate_multilingual(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None = None,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
    endpoint: str = MODULATE_BATCH_URL,
) -> dict[str, Any]:
    """Transcribe one complete file using Modulate multilingual batch STT.

    Every optional enrichment is explicitly false.  ``speaker_diarization`` is
    also false because Scriber does not retain Modulate utterance-level output.
    """
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("Modulate API Key is missing.")

    data = aiohttp.FormData()
    data.add_field(
        "upload_file",
        audio_source,
        filename=filename,
        content_type=content_type,
    )
    for field in (
        "speaker_diarization",
        "emotion_signal",
        "accent_signal",
        "deepfake_signal",
        "pii_phi_tagging",
    ):
        data.add_field(field, "false")
    language_code = modulate_language_code(language)
    if language_code:
        data.add_field("language", language_code)

    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")
    try:
        async with session.post(
            endpoint,
            data=data,
            headers={"X-API-Key": key},
            timeout=aiohttp.ClientTimeout(total=max(1.0, float(timeout_secs))),
        ) as response:
            raw = await read_response_text_limited(response, _MAX_RESPONSE_BYTES)
            if response.status >= 400:
                detail = redact_modulate_error(raw, key)
                raise RuntimeError(
                    f"Modulate batch transcription failed ({response.status}): {detail}"
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            "Modulate batch transcription failed"
        ):
            raise
        raise RuntimeError(
            f"Modulate batch transcription failed: {redact_modulate_error(exc, key)}"
        ) from exc

    if not raw:
        raise RuntimeError("Modulate returned an empty transcription response.")
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError("Modulate returned an invalid transcription response.") from exc
    return _final_transcript_payload(parsed)


class ModulateAsyncProcessor(FrameProcessor):
    """Buffer one live recording and submit one final Modulate batch request."""

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None = None,
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._api_key = str(api_key or "").strip()
        self._language = language
        self._session = session
        self._on_progress = on_progress
        self._buffer = create_pcm_spool(reserve_wav_header=True)
        self._buffer_size = 0
        self._sample_rate = 16_000
        self._channels = 1
        self._oversized = False

    def _reset_buffer(self) -> None:
        close_pcm_spool(getattr(self, "_buffer", None))
        self._buffer = create_pcm_spool(reserve_wav_header=True)
        self._buffer_size = 0
        self._oversized = False

    def __del__(self) -> None:
        close_pcm_spool(getattr(self, "_buffer", None))

    async def _transcribe_wav(self, wav_source: BinaryIO) -> str:
        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_modulate_multilingual(
                session=session,
                api_key=self._api_key,
                audio_source=wav_source,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                on_progress=self._on_progress,
            )

        if self._session:
            payload = await _call(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return modulate_transcript_payload_to_text(payload)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            if frame.audio and not self._oversized:
                if getattr(frame, "sample_rate", None):
                    self._sample_rate = int(frame.sample_rate or self._sample_rate)
                if getattr(frame, "num_channels", None):
                    self._channels = max(1, int(frame.num_channels or self._channels))
                # Leave room for the WAV header.  The provider's documented
                # upload maximum is 100 MB.
                if self._buffer_size + len(frame.audio) + 44 > MODULATE_BATCH_MAX_BYTES:
                    self._oversized = True
                    await self.push_frame(
                        ErrorFrame(
                            error="modulate async error: recording exceeds the 100MB batch upload limit"
                        ),
                        direction,
                    )
                else:
                    self._buffer_size = await append_pcm_frame(
                        self._buffer,
                        self._buffer_size,
                        frame.audio,
                    )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            try:
                if getattr(self, "_skip_terminal_transcription", False):
                    logger.info(
                        "Modulate async: skipping terminal transcription for silent recording"
                    )
                elif self._oversized:
                    logger.warning("Modulate async recording exceeded the provider upload limit")
                elif self._buffer_size:
                    _report_progress(self._on_progress, "Transcribing...")
                    wav_source = await asyncio.to_thread(
                        pcm_stream_to_wav,
                        self._buffer,
                        self._sample_rate,
                        self._channels,
                        reserved_wav_header=True,
                        pcm_size=self._buffer_size,
                    )
                    try:
                        text = (await self._transcribe_wav(wav_source)).strip()
                    finally:
                        wav_source.close()
                    if text:
                        await self.push_frame(
                            TranscriptionFrame(
                                text=text,
                                user_id="user",
                                timestamp=time_now_iso8601(),
                                result=None,
                                finalized=True,
                            ),
                            direction,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                safe_error = redact_modulate_error(exc, self._api_key)
                logger.error(f"Modulate async transcription failed: {safe_error}")
                await self.push_frame(
                    ErrorFrame(error=f"modulate async error: {safe_error}"), direction
                )
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class ModulateRealtimeSTTService(FrameProcessor):
    """Raw-PCM multilingual Modulate WebSocket client with final text only."""

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None = None,
        aiohttp_session: aiohttp.ClientSession | None = None,
        sample_rate: int = 16_000,
        channels: int = 1,
        endpoint: str = MODULATE_STREAMING_URL,
    ) -> None:
        super().__init__()
        self._api_key = str(api_key or "").strip()
        self._language = modulate_language_code(language)
        self._session = aiohttp_session
        self._owned_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task | None = None
        self._sample_rate = max(1, int(sample_rate or 16_000))
        self._channels = max(1, int(channels or 1))
        self._endpoint = str(endpoint or MODULATE_STREAMING_URL)
        self._eos_sent = False
        self._done_received = False
        self._connect_failed = False
        self._terminal_error_emitted = False
        self._terminal_event = asyncio.Event()
        self._audio_bytes_sent = 0
        self._connected_at_monotonic: float | None = None
        self._eos_sent_at_monotonic: float | None = None
        self._last_provider_message_at_monotonic: float | None = None
        self._provider_message_count = 0
        self._final_utterance_count = 0
        self._close_requested = False
        self._local_close_requested = False
        self._stream_closed = False
        self._error_publish_task: asyncio.Task | None = None
        self._close_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._final_timeout_secs = env_float(
            "SCRIBER_MODULATE_STREAM_FINAL_TIMEOUT_SECONDS",
            30.0,
            minimum=2.0,
            maximum=120.0,
        )

    @staticmethod
    def _elapsed_ms(started_at: float | None, *, now: float) -> float | None:
        if started_at is None:
            return None
        return round(max(0.0, now - started_at) * 1000.0, 3)

    def _lifecycle_meta(
        self,
        *,
        status: str,
        close_code: int | None = None,
        error_type: str = "",
    ) -> dict[str, Any]:
        """Return bounded, content-free diagnostics for one WebSocket stream."""

        now = time.monotonic()
        meta: dict[str, Any] = {
            "status": str(status),
            "audioBytesSent": max(0, int(self._audio_bytes_sent)),
            "providerMessageCount": max(0, int(self._provider_message_count)),
            "finalUtteranceCount": max(0, int(self._final_utterance_count)),
        }
        connection_age_ms = self._elapsed_ms(
            self._connected_at_monotonic,
            now=now,
        )
        if connection_age_ms is not None:
            meta["connectionAgeMs"] = connection_age_ms
        finalize_wait_ms = self._elapsed_ms(
            self._eos_sent_at_monotonic,
            now=now,
        )
        if finalize_wait_ms is not None:
            meta["finalizeWaitMs"] = finalize_wait_ms
        last_message_ago_ms = self._elapsed_ms(
            self._last_provider_message_at_monotonic,
            now=now,
        )
        if last_message_ago_ms is not None:
            meta["lastProviderMessageAgoMs"] = last_message_ago_ms
        if isinstance(close_code, int) and 1000 <= close_code <= 4999:
            meta["provider_error_code"] = str(close_code)
        if error_type:
            # Keep only the exception class. Exception messages can contain an
            # authenticated URL and therefore must never enter diagnostics.
            normalized_error_type = str(error_type).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", normalized_error_type):
                meta["errorType"] = normalized_error_type
            else:
                meta["errorType"] = "WebSocketError"
        return meta

    @staticmethod
    def _websocket_exception_type(
        ws: aiohttp.ClientWebSocketResponse,
    ) -> str:
        exception_getter = getattr(ws, "exception", None)
        if not callable(exception_getter):
            return ""
        try:
            exception = exception_getter()
        except Exception as exc:
            return type(exc).__name__
        return type(exception).__name__ if exception is not None else ""

    def _ws_url(self) -> str:
        params = {
            "api_key": self._api_key,
            "audio_format": "s16le",
            "sample_rate": str(self._sample_rate),
            "num_channels": str(self._channels),
            "speaker_diarization": "false",
            "emotion_signal": "false",
            "accent_signal": "false",
            "deepfake_signal": "false",
            "pii_phi_tagging": "false",
            "partial_results": "false",
        }
        if self._language:
            params["language"] = self._language
        return f"{self._endpoint}?{urlencode(params)}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session:
            return self._session
        if not self._owned_session or self._owned_session.closed:
            self._owned_session = aiohttp.ClientSession()
        return self._owned_session

    async def _publish_error_frame(
        self,
        safe_error: str,
        direction: FrameDirection,
    ) -> None:
        try:
            await self.push_frame(
                ErrorFrame(error=f"modulate realtime error: {safe_error}"), direction
            )
        finally:
            # This event means either a provider ``done`` was received or the
            # terminal ErrorFrame finished traversing this processor.  It must
            # never be set merely because the receiver task exited.
            self._terminal_event.set()

    async def _emit_error(self, value: object, direction: FrameDirection) -> bool:
        if self._terminal_error_emitted:
            return False
        self._connect_failed = True
        self._terminal_error_emitted = True
        safe_error = redact_modulate_error(value, self._api_key)
        logger.error(f"Modulate realtime transcription failed: {safe_error}")
        publish_task = asyncio.create_task(
            self._publish_error_frame(safe_error, direction),
            name="modulate_realtime_terminal_error",
        )
        self._error_publish_task = publish_task
        try:
            # Shield publication from cancellation of the receive loop.  The
            # close path also joins this task before reclaiming the websocket.
            await asyncio.shield(publish_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Modulate terminal error delivery warning: "
                f"{redact_modulate_error(exc, self._api_key)}"
            )
        return True

    async def _prepare_for_start(self) -> None:
        """Reset a fully closed stream before a first or post-Stop start."""

        async with self._close_lock:
            async with self._connect_lock:
                if self._ws and not self._ws.closed:
                    # A duplicate StartFrame for an already active stream does
                    # not create a second provider connection.
                    return
                self._eos_sent = False
                self._done_received = False
                self._connect_failed = False
                self._terminal_error_emitted = False
                self._audio_bytes_sent = 0
                self._connected_at_monotonic = None
                self._eos_sent_at_monotonic = None
                self._last_provider_message_at_monotonic = None
                self._provider_message_count = 0
                self._final_utterance_count = 0
                self._close_requested = False
                self._local_close_requested = False
                self._stream_closed = False
                self._error_publish_task = None
                self._terminal_event.clear()

    async def _ensure_connected(self, direction: FrameDirection) -> bool:
        if (
            self._connect_failed
            or self._stream_closed
            or self._close_requested
            or self._local_close_requested
        ):
            return False
        async with self._connect_lock:
            if (
                self._connect_failed
                or self._stream_closed
                or self._close_requested
                or self._local_close_requested
            ):
                return False
            if self._ws and not self._ws.closed:
                return True
            if self._receive_task is not None:
                # A connection that has already started is never silently
                # replaced.  Reconnecting would lose unfinalized audio and let
                # the old receiver mutate the new connection's terminal state.
                if (
                    not self._terminal_error_emitted
                    and not self._done_received
                    and not self._local_close_requested
                ):
                    await self._emit_error(
                        "websocket closed before the final done message", direction
                    )
                return False
            if not self._api_key:
                self._connect_failed = True
                await self._emit_error("Modulate API Key is missing.", direction)
                return False
            websocket: aiohttp.ClientWebSocketResponse | None = None
            try:
                session = await self._get_session()
                # The credential is required in the URL by Modulate.  Never log
                # or otherwise expose this URL.
                websocket = await session.ws_connect(
                    self._ws_url(),
                    # Modulate's own examples do not enable a client heartbeat.
                    # aiohttp gives heartbeat=20 only ten seconds for the PONG;
                    # a provider that is still finalizing can miss that deadline
                    # and aiohttp then turns the pending request into 1006.
                    # The explicit final-response timeout below bounds shutdown.
                    heartbeat=None,
                    timeout=30,
                    max_msg_size=_MAX_RESPONSE_BYTES,
                )
                if self._close_requested or self._local_close_requested:
                    await websocket.close()
                    return False
                self._ws = websocket
                self._connected_at_monotonic = time.monotonic()
                self._receive_task = asyncio.create_task(
                    self._receive_responses(direction),
                    name="modulate_realtime_receive",
                )
                logger.bind(
                    component="pipeline",
                    event="modulate.realtime.connected",
                    workflow="live_mic",
                    stage="provider_connect",
                    provider="modulate",
                    outcome="success",
                    meta=self._lifecycle_meta(status="connected"),
                ).info("Modulate multilingual realtime websocket connected")
                return True
            except asyncio.CancelledError:
                if websocket and not websocket.closed:
                    await websocket.close()
                raise
            except Exception as exc:
                self._connect_failed = True
                await self._emit_error(exc, direction)
                return False

    async def _handle_response(self, raw: str, direction: FrameDirection) -> bool:
        self._provider_message_count += 1
        self._last_provider_message_at_monotonic = time.monotonic()
        try:
            payload = json.loads(raw)
        except Exception:
            await self._emit_error("invalid JSON response", direction)
            return True
        if not isinstance(payload, dict):
            await self._emit_error("invalid response object", direction)
            return True

        message_type = str(payload.get("type") or "").strip().lower()
        if message_type == "utterance":
            self._final_utterance_count += 1
            utterance = payload.get("utterance")
            text = (
                str(utterance.get("text") or "").strip()
                if isinstance(utterance, dict)
                else ""
            )
            if text:
                # Deliberately omit the provider utterance object.  Scriber
                # receives only finalized transcript text.
                await self.push_frame(
                    TranscriptionFrame(
                        text=text,
                        user_id="user",
                        timestamp=time_now_iso8601(),
                        result=None,
                        finalized=True,
                    ),
                    direction,
                )
            return False
        if message_type == "partial_utterance":
            # Defensive fail-closed behavior: the request sets
            # partial_results=false, and an unexpected preview is never emitted.
            logger.warning("Modulate returned an unexpected partial result; ignored")
            return False
        if message_type == "done":
            self._done_received = True
            meta = self._lifecycle_meta(status="done_received")
            duration_ms = payload.get("duration_ms")
            if (
                isinstance(duration_ms, (int, float))
                and not isinstance(duration_ms, bool)
                and math.isfinite(float(duration_ms))
                and duration_ms >= 0
            ):
                meta["providerDurationMs"] = round(float(duration_ms), 3)
            logger.bind(
                component="pipeline",
                event="modulate.realtime.done_received",
                workflow="live_mic",
                stage="provider_finalize",
                provider="modulate",
                outcome="success",
                meta=meta,
            ).debug("Modulate realtime final done message received")
            self._terminal_event.set()
            return True
        if message_type == "error":
            await self._emit_error(payload.get("error") or "provider error", direction)
            self._terminal_event.set()
            return True
        # Ignore future metadata message types instead of persisting them.
        return False

    async def _receive_responses(self, direction: FrameDirection) -> None:
        ws = self._ws
        if not ws:
            self._terminal_event.set()
            return
        try:
            close_code: int | None = None
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    if await self._handle_response(message.data, direction):
                        break
                elif message.type == WSMsgType.BINARY:
                    await self._emit_error("unexpected binary response", direction)
                    break
                elif message.type == WSMsgType.ERROR:
                    raise RuntimeError(str(ws.exception() or "websocket error"))
                elif message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSED,
                ):
                    if isinstance(message.data, int) and 1000 <= message.data <= 4999:
                        close_code = int(message.data)
                    break
            if (
                not self._done_received
                and not self._terminal_error_emitted
                and not self._local_close_requested
            ):
                if close_code is None:
                    candidate = getattr(ws, "close_code", None)
                    if isinstance(candidate, int) and 1000 <= candidate <= 4999:
                        close_code = int(candidate)
                error_type = self._websocket_exception_type(ws)
                logger.bind(
                    component="pipeline",
                    event="modulate.realtime.closed_before_done",
                    workflow="live_mic",
                    stage="provider_finalize",
                    provider="modulate",
                    outcome="failure",
                    meta=self._lifecycle_meta(
                        status="closed_before_done",
                        close_code=close_code,
                        error_type=error_type,
                    ),
                ).debug("Modulate realtime websocket closed before final done")
                detail = "websocket closed before the final done message"
                if close_code == 1011:
                    # RFC 6455 defines 1011 as a server-side unexpected
                    # condition.  Name it accurately so the shared provider
                    # classifier does not blame the user's network.
                    detail = "internal server error before the final done message"
                    detail = f"{detail} (close code {close_code})"
                elif close_code is not None:
                    detail = f"{detail} (close code {close_code})"
                await self._emit_error(detail, direction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_error(exc, direction)
        finally:
            self._terminal_event.set()

    async def _close_stream(
        self,
        direction: FrameDirection,
        *,
        wait_for_final: bool,
    ) -> None:
        async with self._close_lock:
            if self._stream_closed:
                return

            ws = self._ws
            task = self._receive_task
            try:
                should_finalize = (
                    wait_for_final
                    and self._audio_bytes_sent > 0
                    and not self._done_received
                    and not self._terminal_error_emitted
                    and ws is not None
                    and not ws.closed
                )
                if should_finalize and not self._eos_sent:
                    try:
                        await ws.send_str("")
                        self._eos_sent = True
                        self._eos_sent_at_monotonic = time.monotonic()
                        logger.bind(
                            component="pipeline",
                            event="modulate.realtime.eos_sent",
                            workflow="live_mic",
                            stage="provider_finalize",
                            provider="modulate",
                            outcome="started",
                            meta=self._lifecycle_meta(status="eos_sent"),
                        ).debug("Modulate realtime end-of-stream signal sent")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await self._emit_error(exc, direction)

                # An upstream capture failure can deliver EndFrame after the
                # websocket connected but before a single audio frame arrived.
                # Modulate cannot produce a transcript for that empty stream;
                # waiting for its 30-second final timeout blocks Pipecat's
                # EndFrame lane and makes an emergency CancelFrame queue behind
                # it.  Close immediately and let the original capture error
                # remain authoritative.
                if wait_for_final and self._audio_bytes_sent == 0:
                    logger.debug(
                        "Modulate realtime stream closed without final wait "
                        "because no audio reached the provider"
                    )
                should_wait = (
                    should_finalize
                    and self._eos_sent
                    and not self._terminal_error_emitted
                    and task is not None
                    and not task.done()
                )
                if should_wait:
                    try:
                        await asyncio.wait_for(
                            self._terminal_event.wait(), timeout=self._final_timeout_secs
                        )
                    except asyncio.TimeoutError:
                        logger.bind(
                            component="pipeline",
                            event="modulate.realtime.final_timeout",
                            workflow="live_mic",
                            stage="provider_finalize",
                            provider="modulate",
                            outcome="failure",
                            meta=self._lifecycle_meta(status="final_timeout"),
                        ).warning(
                            "Modulate realtime final response timed out"
                        )
                        await self._emit_error(
                            "timed out waiting for the final transcript", direction
                        )
            finally:
                # From this point on an expected local close must not be turned
                # back into a second remote-close ErrorFrame by the receiver.
                self._local_close_requested = True
                try:
                    if task and task is not asyncio.current_task() and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if ws and not ws.closed:
                        try:
                            await ws.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.debug(
                                "Modulate websocket cleanup warning: "
                                f"{redact_modulate_error(exc, self._api_key)}"
                            )
                    if self._owned_session and not self._owned_session.closed:
                        try:
                            await self._owned_session.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.debug(
                                "Modulate session cleanup warning: "
                                f"{redact_modulate_error(exc, self._api_key)}"
                            )
                finally:
                    self._receive_task = None
                    self._ws = None
                    self._stream_closed = True
                    self._terminal_event.set()
                    if self._done_received:
                        status = "closed_after_done"
                        outcome = "success"
                    elif self._terminal_error_emitted:
                        status = "closed_after_error"
                        outcome = "failure"
                    elif wait_for_final and self._audio_bytes_sent == 0:
                        status = "closed_empty"
                        outcome = "success"
                    else:
                        status = "closed_without_final_wait"
                        outcome = "cancelled"
                    logger.bind(
                        component="pipeline",
                        event="modulate.realtime.closed",
                        workflow="live_mic",
                        stage="provider_cleanup",
                        provider="modulate",
                        outcome=outcome,
                        meta=self._lifecycle_meta(status=status),
                    ).debug("Modulate realtime websocket cleanup complete")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            # Downstream processors must observe StartFrame before any
            # connection ErrorFrame.  Otherwise Pipecat rejects the provider
            # error as a lifecycle violation and Scriber's emergency cleanup
            # never gets a chance to stop capture.
            await self.push_frame(frame, direction)
            await self._ensure_connected(direction)
            return

        if isinstance(frame, AudioRawFrame):
            if frame.audio and await self._ensure_connected(direction):
                try:
                    await self._ws.send_bytes(frame.audio)  # type: ignore[union-attr]
                    self._audio_bytes_sent += len(frame.audio)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._emit_error(exc, direction)
                    return
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            await self._close_stream(
                direction,
                wait_for_final=not isinstance(frame, CancelFrame),
            )
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

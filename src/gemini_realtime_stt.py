"""Pipecat adapter for Gemini 3.5 Transcribe Live.

The dedicated Transcribe Live API is not the conversational Gemini Live LLM
service exposed by Pipecat. This processor keeps Scriber's standard Pipecat
frame contract while speaking the provider's raw WebSocket protocol directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from contextlib import suppress
from typing import Any

import aiohttp
from aiohttp import WSMsgType
from loguru import logger
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
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.cloud_async_stt import gemini_language_codes
from src.runtime.env_values import env_float

GEMINI_TRANSCRIBE_LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_TRANSCRIBE_LIVE_MODEL = "gemini-3.5-transcribe-live"
_VOCAB_SPLIT_RE = re.compile(r"[,\n;]+")
_QUERY_KEY_RE = re.compile(r"(?i)([?&]key=)[^&\s]+")
_MAX_PUBLIC_ERROR_CHARS = 500
_LIVE_AUDIO_CHUNK_BYTES = 16_000 * 2 // 10  # 100 ms of mono PCM16.
_GOOGLE_LIVE_ERROR_CODES = {
    "DEADLINE_EXCEEDED": "TimeoutError",
    "INVALID_ARGUMENT": "invalid_request_error",
    "PERMISSION_DENIED": "authentication_error",
    "RESOURCE_EXHAUSTED": "quota_exceeded",
    "UNAUTHENTICATED": "authentication_error",
    "UNAVAILABLE": "ServiceUnavailable",
}


def gemini_custom_vocabulary(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _VOCAB_SPLIT_RE.split(str(value or "")):
        term = " ".join(raw.strip().split())
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 100:
            break
    return terms


def redact_gemini_live_error(value: object, api_key: str) -> str:
    text = " ".join(str(value or "Gemini live transcription failed").split())
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return _QUERY_KEY_RE.sub(r"\1[REDACTED]", text)[:_MAX_PUBLIC_ERROR_CHARS]


def sanitize_gemini_live_provider_error(value: object) -> str:
    """Keep provider failures classifiable without retaining response text."""
    if not isinstance(value, dict):
        return "provider_error"
    status = str(value.get("status") or "").strip().upper()
    mapped = _GOOGLE_LIVE_ERROR_CODES.get(status)
    if mapped:
        return mapped
    message = str(value.get("message") or "").casefold()
    if "quota" in message or "resource exhausted" in message:
        return "quota_exceeded"
    return "provider_error"


class GeminiTranscribeLiveSTTService(FrameProcessor):
    """Stream 16-kHz mono PCM and emit Pipecat interim/final transcript frames."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = GEMINI_TRANSCRIBE_LIVE_MODEL,
        language: Language | str | None = "auto",
        custom_vocab: str = "",
        aiohttp_session: aiohttp.ClientSession | None = None,
        sample_rate: int = 16_000,
        soft_rotate_seconds: float | None = None,
        hard_rotate_seconds: float | None = None,
        final_timeout_seconds: float | None = None,
        final_quiet_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self._api_key = str(api_key or "").strip()
        self._model = str(model or GEMINI_TRANSCRIBE_LIVE_MODEL).strip()
        self._language = language
        self._custom_vocab = custom_vocab
        self._sample_rate = int(sample_rate or 16_000)
        self._session = aiohttp_session
        self._owned_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._setup_ready = asyncio.Event()
        self._transcription_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._direction = FrameDirection.DOWNSTREAM
        self._connected_at = 0.0
        self._final_generation = 0
        self._has_audio_in_session = False
        self._interim_since_final = False
        self._pcm_buffer = bytearray()
        self._rotate_after_final = False
        self._closing = False
        self._stopped = False
        self._terminal_failure = False
        self._setup_failed = False
        self._error_emitted = False
        self._soft_rotate_seconds = float(
            soft_rotate_seconds
            if soft_rotate_seconds is not None
            else env_float(
                "SCRIBER_GEMINI_LIVE_SOFT_ROTATE_SECONDS",
                480.0,
                minimum=60.0,
                maximum=570.0,
            )
        )
        self._hard_rotate_seconds = float(
            hard_rotate_seconds
            if hard_rotate_seconds is not None
            else env_float(
                "SCRIBER_GEMINI_LIVE_HARD_ROTATE_SECONDS",
                570.0,
                minimum=90.0,
                maximum=590.0,
            )
        )
        self._final_timeout_seconds = float(
            final_timeout_seconds
            if final_timeout_seconds is not None
            else env_float(
                "SCRIBER_GEMINI_LIVE_FINAL_TIMEOUT_SECONDS",
                15.0,
                minimum=1.0,
                maximum=30.0,
            )
        )
        self._final_quiet_seconds = float(
            final_quiet_seconds
            if final_quiet_seconds is not None
            else env_float(
                "SCRIBER_GEMINI_LIVE_FINAL_QUIET_SECONDS",
                0.75,
                minimum=0.1,
                maximum=3.0,
            )
        )
        if self._hard_rotate_seconds <= self._soft_rotate_seconds:
            self._soft_rotate_seconds = max(1.0, self._hard_rotate_seconds - 1.0)

    def _setup_payload(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {
            "languageCodes": gemini_language_codes(self._language),
            "mode": "SMART",
        }
        terms = gemini_custom_vocabulary(self._custom_vocab)
        if terms:
            transcription["customVocabulary"] = terms
        return {
            "setup": {
                "model": f"models/{self._model}",
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": transcription,
            }
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session:
            return self._session
        if not self._owned_session or self._owned_session.closed:
            self._owned_session = aiohttp.ClientSession()
        return self._owned_session

    async def _emit_error(self, operation: str, exc: object) -> None:
        if self._error_emitted:
            return
        self._error_emitted = True
        detail = redact_gemini_live_error(exc, self._api_key)
        logger.error(
            "Gemini Transcribe Live {} failed (error_type={})",
            operation,
            type(exc).__name__,
        )
        await self.push_frame(
            ErrorFrame(
                error=f"Gemini live transcription {operation} failed: {detail}",
                fatal=True,
                processor=self,
            ),
            self._direction,
        )

    async def _fail(
        self,
        operation: str,
        exc: object,
        *,
        setup_failed: bool = False,
        close_connection: bool = True,
    ) -> None:
        self._terminal_failure = True
        if setup_failed:
            self._setup_failed = True
            self._setup_ready.set()
        await self._emit_error(operation, exc)
        if close_connection:
            await self._close_connection()

    async def _connect(self) -> bool:
        if self._stopped or self._terminal_failure:
            return False
        if self._ws and not self._ws.closed:
            return True
        if not self._api_key:
            await self._fail("connection", "API key is missing", close_connection=False)
            return False
        try:
            session = await self._get_session()
            self._error_emitted = False
            self._setup_ready.clear()
            self._setup_failed = False
            self._closing = False
            self._ws = await session.ws_connect(
                GEMINI_TRANSCRIBE_LIVE_URL,
                params={"key": self._api_key},
                heartbeat=20,
                timeout=30,
                max_msg_size=4 * 1024 * 1024,
            )
            self._receive_task = asyncio.create_task(
                self._receive_responses(),
                name="gemini_transcribe_live_receive",
            )
            await self._ws.send_str(json.dumps(self._setup_payload(), separators=(",", ":")))
            await asyncio.wait_for(self._setup_ready.wait(), timeout=10.0)
            if self._setup_failed or not self._ws or self._ws.closed:
                await self._close_connection()
                return False
            self._connected_at = time.monotonic()
            self._has_audio_in_session = False
            self._interim_since_final = False
            self._pcm_buffer.clear()
            self._rotate_after_final = False
            logger.info("Gemini Transcribe Live websocket connected")
            return True
        except asyncio.CancelledError:
            await self._close_connection()
            raise
        except Exception as exc:
            await self._close_connection()
            await self._fail("connection", exc, close_connection=False)
            return False

    async def _receive_responses(self) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._handle_response(message.data)
                elif message.type == WSMsgType.BINARY:
                    await self._handle_response(message.data.decode("utf-8", errors="replace"))
                elif message.type == WSMsgType.ERROR:
                    raise RuntimeError(str(ws.exception() or "websocket error"))
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                await self._fail("receive", exc, setup_failed=True)
        else:
            # aiohttp ends async iteration with StopAsyncIteration for a normal
            # remote CLOSE, so reaching this branch while not closing locally
            # is always an unexpected provider disconnect.
            if not self._closing:
                await self._fail(
                    "receive",
                    "provider closed the websocket",
                    setup_failed=True,
                )

    async def _handle_response(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Gemini Transcribe Live returned a non-JSON message")
            return
        if not isinstance(payload, dict):
            return
        if payload.get("setupComplete") is not None or payload.get("setup_complete") is not None:
            self._setup_ready.set()
        if payload.get("error"):
            await self._fail(
                "provider",
                sanitize_gemini_live_provider_error(payload.get("error")),
                setup_failed=True,
            )
            return
        if isinstance(payload.get("goAway") or payload.get("go_away"), dict):
            # The provider can announce an earlier shutdown than the documented
            # session ceiling. Rotate at the next frame boundary.
            self._rotate_after_final = True

        content = payload.get("serverContent") or payload.get("server_content")
        if not isinstance(content, dict):
            return
        interim = content.get("interimInputTranscription") or content.get("interim_input_transcription")
        interim_text = str(interim.get("text") or "").strip() if isinstance(interim, dict) else ""
        if interim_text:
            self._interim_since_final = True
            self._transcription_event.set()
            await self.push_frame(
                InterimTranscriptionFrame(
                    text=interim_text,
                    user_id="user",
                    timestamp=time_now_iso8601(),
                    result=None,
                ),
                self._direction,
            )

        final = content.get("inputTranscription") or content.get("input_transcription")
        final_text = str(final.get("text") or "").strip() if isinstance(final, dict) else ""
        if isinstance(final, dict):
            self._final_generation += 1
            self._interim_since_final = False
            if self._connected_at and time.monotonic() - self._connected_at >= self._soft_rotate_seconds:
                self._rotate_after_final = True
            self._transcription_event.set()
        if final_text:
            await self.push_frame(
                TranscriptionFrame(
                    text=final_text,
                    user_id="user",
                    timestamp=time_now_iso8601(),
                    result=None,
                ),
                self._direction,
            )

    async def _send_pcm_chunk(self, audio: bytes) -> bool:
        ws = self._ws
        if not audio or not ws or ws.closed:
            return False
        message = {
            "realtimeInput": {
                "audio": {
                    "data": base64.b64encode(audio).decode("ascii"),
                    "mimeType": "audio/pcm;rate=16000",
                }
            }
        }
        try:
            await ws.send_str(json.dumps(message, separators=(",", ":")))
            self._has_audio_in_session = True
            return True
        except Exception as exc:
            await self._fail("send", exc)
            return False

    async def _flush_pcm_buffer(self) -> bool:
        if not self._pcm_buffer:
            return True
        audio = bytes(self._pcm_buffer)
        self._pcm_buffer.clear()
        return await self._send_pcm_chunk(audio)

    async def _wait_for_final_drain(self, generation: int) -> bool:
        deadline = time.monotonic() + self._final_timeout_seconds
        while self._final_generation <= generation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Timed out waiting for Gemini Transcribe Live final transcript")
                return False
            try:
                await asyncio.wait_for(self._transcription_event.wait(), timeout=remaining)
            except TimeoutError:
                logger.warning("Timed out waiting for Gemini Transcribe Live final transcript")
                return False
            self._transcription_event.clear()

        # A final for the previous provider turn can race with audioStreamEnd.
        # Keep a short quiet window and extend it whenever another final lands.
        observed = self._final_generation
        while True:
            self._transcription_event.clear()
            if self._final_generation > observed:
                observed = self._final_generation
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(
                    self._transcription_event.wait(),
                    timeout=min(self._final_quiet_seconds, remaining),
                )
            except TimeoutError:
                return True
            observed = self._final_generation

    async def _wait_for_possible_late_final(self, generation: int) -> int | None:
        """Return a drain baseline if provider speech appears during the quiet window."""
        observed = generation
        hard_deadline = time.monotonic() + self._final_timeout_seconds
        quiet_deadline = min(hard_deadline, time.monotonic() + self._final_quiet_seconds)
        while True:
            self._transcription_event.clear()
            if self._final_generation > observed:
                observed = self._final_generation
                quiet_deadline = min(
                    hard_deadline,
                    time.monotonic() + self._final_quiet_seconds,
                )
            if self._interim_since_final:
                return self._final_generation
            remaining = quiet_deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._transcription_event.wait(), timeout=remaining)
            except TimeoutError:
                return None

    async def _finish_current_stream(self, *, wait_for_final: bool) -> bool:
        ws = self._ws
        if not ws or ws.closed:
            return not self._terminal_failure
        if not await self._flush_pcm_buffer():
            return False
        generation = self._final_generation
        try:
            await ws.send_str(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
        except Exception as exc:
            if not self._closing:
                await self._fail("finalization", exc)
            return False
        # Raw PCM may be trailing silence, so sending bytes does not prove that
        # Gemini owes us another transcript. Only a provider interim is evidence
        # of an unfinished speech turn; otherwise audioStreamEnd is best-effort
        # and the short quiet window below catches a late final without turning
        # a normal silent stop into a 15-second terminal failure.
        pending_generation = self._final_generation if wait_for_final and self._interim_since_final else None
        if wait_for_final and self._has_audio_in_session and pending_generation is None:
            # A provider interim can race with audioStreamEnd. Promote that
            # newly observed speech to the strict drain path instead of closing
            # at the end of the best-effort quiet window.
            pending_generation = await self._wait_for_possible_late_final(generation)
        while pending_generation is not None:
            if not await self._wait_for_final_drain(pending_generation):
                await self._fail(
                    "finalization",
                    "timed out waiting for the final transcript",
                    close_connection=False,
                )
                return False
            # Transcription messages are ordered independently. If a new
            # interim landed during the prior final's quiet window, require its
            # corresponding final before closing too.
            pending_generation = self._final_generation if self._interim_since_final else None
        return True

    async def _close_connection(self) -> None:
        self._closing = True
        ws, task = self._ws, self._receive_task
        self._ws = None
        self._receive_task = None
        if ws and not ws.closed:
            with suppress(Exception):
                await ws.close()
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._connected_at = 0.0
        self._has_audio_in_session = False
        self._interim_since_final = False
        self._pcm_buffer.clear()

    async def _rotate(self) -> bool:
        logger.info("Rotating Gemini Transcribe Live websocket before provider session limit")
        finalized = await self._finish_current_stream(wait_for_final=True)
        await self._close_connection()
        if not finalized:
            return False
        self._terminal_failure = False
        return await self._connect()

    async def _close(self, *, wait_for_final: bool) -> None:
        if wait_for_final:
            await self._finish_current_stream(wait_for_final=True)
        else:
            self._pcm_buffer.clear()
        await self._close_connection()
        if self._owned_session and not self._owned_session.closed:
            await self._owned_session.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._direction = direction

        if isinstance(frame, StartFrame):
            # StartFrame and AudioRawFrame use independent Pipecat lanes. Hold
            # the same lock as audio so no chunk can overtake setupComplete.
            async with self._send_lock:
                self._stopped = False
                await self.push_frame(frame, direction)
                await self._connect()
            return

        if isinstance(frame, AudioRawFrame):
            async with self._send_lock:
                age = time.monotonic() - self._connected_at if self._connected_at else 0.0
                if self._rotate_after_final or age >= self._hard_rotate_seconds:
                    await self._rotate()
                if frame.audio and await self._connect():
                    if int(frame.sample_rate or self._sample_rate) != 16_000 or int(frame.num_channels or 1) != 1:
                        await self._fail("audio", "expected 16-kHz mono PCM16 audio")
                    else:
                        self._pcm_buffer.extend(frame.audio)
                        while len(self._pcm_buffer) >= _LIVE_AUDIO_CHUNK_BYTES:
                            chunk = bytes(self._pcm_buffer[:_LIVE_AUDIO_CHUNK_BYTES])
                            del self._pcm_buffer[:_LIVE_AUDIO_CHUNK_BYTES]
                            if not await self._send_pcm_chunk(chunk):
                                break
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            async with self._send_lock:
                self._stopped = True
                await self._close(wait_for_final=not isinstance(frame, CancelFrame))
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

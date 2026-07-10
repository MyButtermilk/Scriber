"""Smallest AI Pulse STT services for realtime and async transcription."""

from __future__ import annotations

import asyncio
import io
import json
import re
import tempfile
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
    InterimTranscriptionFrame,
    StartFrame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.runtime.audio_spool import pcm_stream_to_wav
from src.runtime.http_response import read_response_text_limited


_SMALLEST_PULSE_HTTP_URL = "https://api.smallest.ai/waves/v1/pulse/get_text"
_SMALLEST_PULSE_WS_URL = "wss://api.smallest.ai/waves/v1/pulse/get_text"
_SMALLEST_DEFAULT_LANGUAGE = "multi-eu"
_VOCAB_SPLIT_RE = re.compile(r"[,\n;]+")
_MAX_KEYWORDS = 100


def smallest_language_code(language: Language | str | None) -> str:
    if not language:
        return _SMALLEST_DEFAULT_LANGUAGE
    raw = str(language.value if isinstance(language, Language) else language).strip().lower()
    if not raw or raw == "auto":
        return _SMALLEST_DEFAULT_LANGUAGE
    if raw.startswith("multi"):
        return raw
    return raw.replace("_", "-").split("-", 1)[0]


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def _keywords_from_vocab(custom_vocab: str) -> str:
    if not custom_vocab:
        return ""

    out: list[str] = []
    seen: set[str] = set()
    for raw in _VOCAB_SPLIT_RE.split(str(custom_vocab)):
        term = " ".join(raw.strip().split())
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= _MAX_KEYWORDS:
            break
    return ",".join(out)


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if not on_progress:
        return
    try:
        on_progress(message)
    except Exception:
        pass


def format_smallest_utterances_to_scriber_text(utterances: list[dict[str, Any]]) -> str:
    if not utterances:
        return ""

    speaker_map: dict[str, int] = {}
    next_index = 1
    lines: list[str] = []

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = str(utterance.get("text") or utterance.get("transcript") or "").strip()
        if not text:
            continue
        speaker_key = str(utterance.get("speaker") or "").strip()
        if not speaker_key:
            lines.append(text)
            continue
        speaker_num = speaker_map.get(speaker_key)
        if speaker_num is None:
            speaker_num = next_index
            speaker_map[speaker_key] = speaker_num
            next_index += 1
        lines.append(f"[Speaker {speaker_num}]: {text}")

    return "\n\n".join(lines).strip()


def smallest_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    utterances = payload.get("utterances")
    utterance_list = utterances if isinstance(utterances, list) else []
    if prefer_speaker_labels and utterance_list:
        formatted = format_smallest_utterances_to_scriber_text(
            [u for u in utterance_list if isinstance(u, dict)]
        )
        if formatted:
            return formatted

    for key in ("transcription", "transcript", "text"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text

    if utterance_list:
        return format_smallest_utterances_to_scriber_text(
            [u for u in utterance_list if isinstance(u, dict)]
        )
    return ""


async def transcribe_with_smallest_pre_recorded(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    language: Language | str | None,
    word_timestamps: bool = False,
    diarize: bool = False,
    gender_detection: bool = False,
    emotion_detection: bool = False,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    """Transcribe complete audio with Smallest AI Pulse pre-recorded REST API."""
    params = {
        "language": smallest_language_code(language),
        "word_timestamps": _bool_param(word_timestamps),
        "diarize": _bool_param(diarize),
        "gender_detection": _bool_param(gender_detection),
        "emotion_detection": _bool_param(emotion_detection),
        "format": "true",
        "punctuate": "true",
        "capitalize": "true",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/octet-stream",
    }

    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")
    async with session.post(
        _SMALLEST_PULSE_HTTP_URL,
        params=params,
        data=audio_source,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as resp:
        raw = await read_response_text_limited(resp, 64 * 1024 * 1024)
        if resp.status >= 400:
            raise RuntimeError(f"Smallest AI transcription failed ({resp.status}): {raw[:500]}")

    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"transcription": raw}
    if not isinstance(payload, dict):
        return {}
    status = str(payload.get("status") or "").strip().lower()
    if status and status != "success":
        raise RuntimeError(f"Smallest AI transcription failed: {payload}")
    return payload


class SmallestAsyncProcessor(FrameProcessor):
    """Buffered STT via Smallest AI Pulse pre-recorded API."""

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str = "auto",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._language = language or "auto"
        self._session = session
        self._on_progress = on_progress
        self._diarize = bool(diarize)
        self._buffer = self._create_buffer()
        self._buffer_size = 0
        self._sample_rate = 16000
        self._channels = 1

    def _create_buffer(self):
        return tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)

    def _reset_buffer(self) -> None:
        try:
            self._buffer.close()
        except Exception:
            pass
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    async def _transcribe_wav(self, wav_source: BinaryIO) -> str:
        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_smallest_pre_recorded(
                session=session,
                api_key=self._api_key,
                audio_source=wav_source,
                language=self._language,
                word_timestamps=self._diarize,
                diarize=self._diarize,
                on_progress=self._on_progress,
                timeout_secs=900.0,
            )

        if self._session:
            payload = await _call(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)

        return smallest_transcript_payload_to_text(
            payload,
            prefer_speaker_labels=self._diarize,
        )

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_source = await asyncio.to_thread(
            pcm_stream_to_wav,
            io.BytesIO(audio_bytes),
            self._sample_rate,
            self._channels,
        )
        try:
            return await self._transcribe_wav(wav_source)
        finally:
            wav_source.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            if frame.audio:
                if getattr(frame, "sample_rate", None):
                    self._sample_rate = int(frame.sample_rate or self._sample_rate)
                if getattr(frame, "num_channels", None):
                    self._channels = max(1, int(frame.num_channels or self._channels))
                self._buffer.write(frame.audio)
                self._buffer_size += len(frame.audio)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            try:
                if getattr(self, "_skip_terminal_transcription", False):
                    logger.info("Smallest async: skipping terminal transcription for silent recording")
                    self._reset_buffer()
                    await self.push_frame(frame, direction)
                    return
                if self._buffer_size:
                    _report_progress(self._on_progress, "Transcribing...")
                    wav_source = await asyncio.to_thread(
                        pcm_stream_to_wav,
                        self._buffer,
                        self._sample_rate,
                        self._channels,
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
                            ),
                            direction,
                        )
            except Exception as exc:
                logger.error(f"Smallest AI async transcription failed: {exc}")
                await self.push_frame(ErrorFrame(error=f"smallest async error: {exc}"), direction)
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class SmallestRealtimeSTTService(FrameProcessor):
    """Realtime STT via Smallest AI Pulse WebSocket API."""

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str = "auto",
        custom_vocab: str = "",
        aiohttp_session: aiohttp.ClientSession | None = None,
        sample_rate: int = 16000,
        encoding: str = "linear16",
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._language = language or "auto"
        self._custom_vocab = custom_vocab or ""
        self._session = aiohttp_session
        self._owned_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task | None = None
        self._sample_rate = int(sample_rate or 16000)
        self._encoding = encoding
        self._close_sent = False
        self._connect_failed = False

    def _ws_url(self) -> str:
        params = {
            "language": smallest_language_code(self._language),
            "encoding": self._encoding,
            "sample_rate": str(self._sample_rate),
            "word_timestamps": "false",
            "sentence_timestamps": "false",
            "format": "true",
            "punctuate": "true",
            "capitalize": "true",
        }
        keywords = _keywords_from_vocab(self._custom_vocab)
        if keywords:
            params["keywords"] = keywords
        return f"{_SMALLEST_PULSE_WS_URL}?{urlencode(params)}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session:
            return self._session
        if not self._owned_session or self._owned_session.closed:
            self._owned_session = aiohttp.ClientSession()
        return self._owned_session

    async def _ensure_connected(self, direction: FrameDirection) -> bool:
        if self._ws and not self._ws.closed:
            return True
        if self._connect_failed:
            return False

        try:
            session = await self._get_session()
            self._ws = await session.ws_connect(
                self._ws_url(),
                headers={"Authorization": f"Bearer {self._api_key}"},
                heartbeat=20,
                timeout=30,
            )
            self._close_sent = False
            self._receive_task = asyncio.create_task(
                self._receive_responses(direction),
                name="smallest_realtime_receive",
            )
            logger.info("Smallest AI realtime websocket connected")
            return True
        except Exception as exc:
            self._connect_failed = True
            logger.error(f"Smallest AI realtime websocket connection failed: {exc}")
            await self.push_frame(
                ErrorFrame(error=f"smallest realtime connection error: {exc}"),
                direction,
            )
            return False

    async def _receive_responses(self, direction: FrameDirection) -> None:
        ws = self._ws
        if not ws:
            return
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if await self._handle_response(msg.data, direction):
                        break
                elif msg.type == WSMsgType.BINARY:
                    if await self._handle_response(msg.data.decode("utf-8", errors="replace"), direction):
                        break
                elif msg.type == WSMsgType.ERROR:
                    raise RuntimeError(str(ws.exception() or "websocket error"))
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Smallest AI realtime receive failed: {exc}")
            await self.push_frame(ErrorFrame(error=f"smallest realtime error: {exc}"), direction)

    async def _handle_response(self, raw: str, direction: FrameDirection) -> bool:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug(f"Smallest AI realtime returned non-JSON message: {raw[:200]}")
            return False
        if not isinstance(payload, dict):
            return False

        status = str(payload.get("status") or "").strip().lower()
        if status and status != "success":
            await self.push_frame(
                ErrorFrame(error=f"smallest realtime error: {payload}"),
                direction,
            )
            return False

        text = str(payload.get("transcript") or payload.get("transcription") or "").strip()
        if text:
            frame_cls = TranscriptionFrame if payload.get("is_final") or payload.get("is_last") else InterimTranscriptionFrame
            await self.push_frame(
                frame_cls(
                    text=text,
                    user_id="user",
                    timestamp=time_now_iso8601(),
                    result=payload,
                ),
                direction,
            )

        if payload.get("is_last"):
            self._close_sent = True
            return True
        return False

    async def _close_stream(self, direction: FrameDirection, *, wait_for_last: bool) -> None:
        ws = self._ws
        if ws and not ws.closed and not self._close_sent:
            try:
                await ws.send_str(json.dumps({"type": "close_stream"}))
                self._close_sent = True
            except Exception as exc:
                logger.debug(f"Smallest AI close_stream warning: {exc}")

        task = self._receive_task
        if wait_for_last and task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for Smallest AI final realtime transcript")
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug(f"Smallest AI receive task warning: {exc}")

        if ws and not ws.closed:
            await ws.close()
        if self._owned_session and not self._owned_session.closed:
            await self._owned_session.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._ensure_connected(direction)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, AudioRawFrame):
            if getattr(frame, "sample_rate", None) and not self._ws:
                self._sample_rate = int(frame.sample_rate or self._sample_rate)
            if frame.audio and await self._ensure_connected(direction):
                try:
                    await self._ws.send_bytes(frame.audio)  # type: ignore[union-attr]
                except Exception as exc:
                    logger.error(f"Smallest AI realtime send failed: {exc}")
                    await self.push_frame(ErrorFrame(error=f"smallest realtime send error: {exc}"), direction)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            await self._close_stream(direction, wait_for_last=not isinstance(frame, CancelFrame))
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

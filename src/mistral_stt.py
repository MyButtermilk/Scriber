"""
Mistral STT services for realtime and async transcription.
"""
from __future__ import annotations

import json
import re
import contextlib
import io
import tempfile
import wave
from typing import Any, AsyncGenerator, BinaryIO, Callable, Optional

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


_MISTRAL_TRANSCRIPTIONS_URL = "https://api.mistral.ai/v1/audio/transcriptions"


def _language_to_code(language: Optional[Language] | str | None) -> str:
    if not language:
        return "auto"
    if isinstance(language, Language):
        return str(language).split("-")[0]
    return str(language).strip() or "auto"


_CONTEXT_SPLIT_RE = re.compile(r"[,\n;]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_context_bias_terms(context_bias: str | list[str] | None) -> list[str]:
    """Normalize context bias terms for Mistral API requirements.

    Mistral currently expects each `context_bias` item to match `^[^,\\s]+$`.
    We therefore split comma-separated phrases into single-word tokens and
    strip punctuation at token boundaries.
    """
    if not context_bias:
        return []

    raw_terms: list[str] = []
    if isinstance(context_bias, list):
        for item in context_bias:
            if item:
                raw_terms.append(str(item))
    else:
        raw_terms.extend(_CONTEXT_SPLIT_RE.split(str(context_bias)))

    tokens: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        for part in _WHITESPACE_RE.split(term.strip()):
            token = part.strip(" \t\r\n,.;:!?()[]{}\"'")
            if not token:
                continue
            # Guardrail for Mistral schema: each item must not contain comma/whitespace.
            if any(ch.isspace() for ch in token) or "," in token:
                continue
            key = token.casefold()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token)
    return tokens


def _custom_vocab_to_context_bias(custom_vocab: str) -> list[str]:
    return _normalize_context_bias_terms(custom_vocab)


def _pcm_to_wav(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with contextlib.closing(wave.open(buf, "wb")) as wf:
        wf.setnchannels(max(1, int(channels or 1)))
        wf.setsampwidth(2)  # int16 PCM
        wf.setframerate(max(1, int(sample_rate or 16000)))
        wf.writeframes(audio_bytes)
    return buf.getvalue()


def _extract_text(payload: dict[str, Any]) -> str:
    text = (payload.get("text") or "").strip()
    if text:
        return text
    segments = payload.get("segments") or []
    if isinstance(segments, list):
        return " ".join(
            str(seg.get("text", "")).strip()
            for seg in segments
            if isinstance(seg, dict) and seg.get("text")
        ).strip()
    return ""


def format_mistral_segments_with_speakers(segments: list[dict[str, Any]]) -> str:
    """Format diarized segments from Mistral into readable speaker blocks."""
    if not segments:
        return ""

    has_speakers = any(
        isinstance(seg, dict) and (seg.get("speaker_id") or seg.get("speaker"))
        for seg in segments
    )
    if not has_speakers:
        return " ".join(
            str(seg.get("text", "")).strip() for seg in segments if isinstance(seg, dict)
        ).strip()

    blocks: list[tuple[str, list[str]]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        speaker = str(seg.get("speaker_id") or seg.get("speaker") or "unknown").strip()
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        if not blocks or blocks[-1][0] != speaker:
            blocks.append((speaker, [text]))
        else:
            blocks[-1][1].append(text)

    lines = [f"[Speaker {speaker}]: {' '.join(parts)}" for speaker, parts in blocks if parts]
    return "\n\n".join(lines).strip()


async def transcribe_with_mistral(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    file_content: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: str | None = None,
    context_bias: str | list[str] | None = "",
    diarize: bool = False,
    timestamp_granularities: Optional[list[str]] = None,
    timeout_secs: int = 180,
    _allow_language_retry: bool = True,
) -> dict[str, Any]:
    effective_language = language
    # Mistral currently does not support combining language + timestamp_granularities.
    # Avoid an avoidable retry by dropping language when timestamps are requested.
    if effective_language and timestamp_granularities:
        effective_language = None

    data = aiohttp.FormData()
    data.add_field("file", file_content, filename=filename, content_type=content_type)
    data.add_field("model", model)
    if effective_language:
        data.add_field("language", effective_language)
    for token in _normalize_context_bias_terms(context_bias):
        data.add_field("context_bias", token)
    if diarize:
        data.add_field("diarize", "true")
    for granularity in timestamp_granularities or []:
        data.add_field("timestamp_granularities", granularity)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
    }

    async with session.post(
        _MISTRAL_TRANSCRIPTIONS_URL,
        data=data,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as resp:
        raw = await resp.text()
        if resp.status >= 400:
            lower = raw.lower()
            if (
                _allow_language_retry
                and effective_language
                and timestamp_granularities
                and "timestamp_granularities" in lower
                and "language" in lower
            ):
                if not isinstance(file_content, (bytes, bytearray)):
                    if not hasattr(file_content, "seek"):
                        raise RuntimeError(
                            "Mistral transcription retry requires a seekable file object."
                        )
                    try:
                        file_content.seek(0)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Mistral transcription retry could not rewind file stream: {exc}"
                        ) from exc
                logger.info("Retrying Mistral transcription without language (timestamp_granularities incompatibility)")
                return await transcribe_with_mistral(
                    session=session,
                    api_key=api_key,
                    model=model,
                    file_content=file_content,
                    filename=filename,
                    content_type=content_type,
                    language=None,
                    context_bias=context_bias,
                    diarize=diarize,
                    timestamp_granularities=timestamp_granularities,
                    timeout_secs=timeout_secs,
                    _allow_language_retry=False,
                )
            raise RuntimeError(f"Mistral transcription failed ({resp.status}): {raw[:500]}")

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.debug("Mistral returned non-JSON transcription payload; using text fallback")
        return {"text": raw}
    return parsed if isinstance(parsed, dict) else {}


class MistralRealtimeSTTService(SegmentedSTTService):
    """Segmented STT using Mistral Voxtral realtime transcription model."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        language: Optional[Language] | str = "auto",
        custom_vocab: str = "",
        aiohttp_session: aiohttp.ClientSession | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._api_key = api_key
        self._model = model
        self._language = _language_to_code(language)
        self._context_bias = _custom_vocab_to_context_bias(custom_vocab)
        self._session = aiohttp_session

        logger.info(f"MistralRealtimeSTTService initialized (model={self._model})")

    async def set_language(self, language: Language):
        self._language = _language_to_code(language)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return
        try:
            wav_bytes = _pcm_to_wav(
                audio_bytes=audio,
                sample_rate=self.sample_rate or 16000,
                channels=1,
            )
            language = self._language if self._language != "auto" else None

            async def _transcribe(session: aiohttp.ClientSession) -> dict[str, Any]:
                return await transcribe_with_mistral(
                    session=session,
                    api_key=self._api_key,
                    model=self._model,
                    file_content=wav_bytes,
                    filename="audio.wav",
                    content_type="audio/wav",
                    language=language,
                    context_bias=self._context_bias,
                    diarize=False,
                )

            if self._session:
                payload = await _transcribe(self._session)
            else:
                async with aiohttp.ClientSession() as session:
                    payload = await _transcribe(session)

            text = _extract_text(payload)
            if text:
                yield TranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    result=None,
                )
        except Exception as exc:
            logger.error(f"Mistral realtime STT error: {exc}")
            yield ErrorFrame(error=f"mistral realtime error: {exc}")


class MistralAsyncProcessor(FrameProcessor):
    """Buffered async STT using Mistral Voxtral Mini Transcribe V2 model."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        language: Optional[Language] | str = "auto",
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self._api_key = api_key
        self._model = model
        self._language = _language_to_code(language)
        self._context_bias = _custom_vocab_to_context_bias(custom_vocab)
        self._session = session
        self._on_progress = on_progress
        self._buffer = self._create_buffer()
        self._buffer_size = 0
        self._sample_rate = 16000
        self._channels = 1

        logger.info(f"MistralAsyncProcessor initialized (model={self._model})")

    def _create_buffer(self):
        """Use spooled temp file so long recordings don't keep all audio in RAM."""
        return tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)

    def _reset_buffer(self) -> None:
        try:
            self._buffer.close()
        except Exception:
            pass
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    def _report_progress(self, msg: str) -> None:
        if not self._on_progress:
            return
        try:
            self._on_progress(msg)
        except Exception:
            pass

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(
            audio_bytes=audio_bytes,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )
        language = self._language if self._language != "auto" else None

        async def _transcribe(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_mistral(
                session=session,
                api_key=self._api_key,
                model=self._model,
                file_content=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=language,
                context_bias=self._context_bias,
                diarize=False,
                timeout_secs=240,
            )

        if self._session:
            payload = await _transcribe(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _transcribe(session)

        return _extract_text(payload)

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
                if self._buffer_size:
                    self._report_progress("Transcribing...")
                    self._buffer.seek(0)
                    audio_bytes = self._buffer.read()
                    text = (await self._transcribe_bytes(audio_bytes)).strip()
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
                logger.error(f"Mistral async transcription failed: {exc}")
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

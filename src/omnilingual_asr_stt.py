"""Local bridge for Meta Omnilingual ASR reference inference.

Meta distributes weights and a Python inference pipeline, not a hosted STT
API. Its public pipeline returns completed transcripts, so live audio is
buffered and inference runs outside Scriber's asyncio loop after recording.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import AudioRawFrame, CancelFrame, EndFrame, ErrorFrame, Frame, StopFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


def omnilingual_asr_available() -> bool:
    try:
        from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline  # noqa: F401
    except ImportError:
        return False
    return True


def omnilingual_language_code(language: Language | str | None) -> str | None:
    raw = str(language.value if isinstance(language, Language) else language or "").strip().lower()
    if raw in {"", "auto"}:
        return None
    base = raw.replace("_", "-").split("-", 1)[0]
    return {
        "de": "deu_Latn",
        "en": "eng_Latn",
        "fr": "fra_Latn",
        "es": "spa_Latn",
        "it": "ita_Latn",
        "pt": "por_Latn",
        "nl": "nld_Latn",
        "pl": "pol_Latn",
        "ru": "rus_Cyrl",
        "uk": "ukr_Cyrl",
        "ar": "arb_Arab",
        "hi": "hin_Deva",
        "ja": "jpn_Jpan",
        "ko": "kor_Hang",
        "zh": "cmn_Hans",
    }.get(base)


@lru_cache(maxsize=2)
def _load_pipeline(model_card: str) -> Any:
    from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline

    logger.info("Loading Meta Omnilingual ASR model {}", model_card)
    return ASRInferencePipeline(model_card=model_card)


def _transcribe_waveform(audio: bytes, *, sample_rate: int, model_card: str, language: str | None) -> str:
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
    result = _load_pipeline(model_card).transcribe(
        [{"waveform": samples, "sample_rate": sample_rate}], lang=[language] if language else None, batch_size=1
    )
    return str(result[0] if result else "").strip()


def transcribe_omnilingual_file(path: Path, *, model_card: str, language: str | None) -> str:
    result = _load_pipeline(model_card).transcribe([path], lang=[language] if language else None, batch_size=1)
    return str(result[0] if result else "").strip()


class OmnilingualASRBufferedSTTService(STTService):
    """Pipecat adapter for Meta's final-result-only local interface."""

    def __init__(
        self,
        *,
        model_card: str,
        language: Language | str | None,
        sample_rate: int,
        channels: int,
        max_buffer_secs: int = 900,
        **kwargs: Any,
    ) -> None:
        if not omnilingual_asr_available():
            raise ImportError("Meta Omnilingual ASR runtime is not installed in this Scriber build.")
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._model_card = model_card
        self._language = omnilingual_language_code(language)
        self._max_buffer_bytes = max(5, int(max_buffer_secs)) * sample_rate * max(1, int(channels or 1)) * 2
        self._buffer = bytearray()

    async def start(self, frame: Frame) -> None:
        await super().start(frame)
        self._buffer.clear()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await AIService.process_frame(self, frame, direction)
        if isinstance(frame, AudioRawFrame):
            if not self._muted and frame.audio:
                if len(self._buffer) + len(frame.audio) > self._max_buffer_bytes:
                    raise RuntimeError("Meta Omnilingual ASR live recording exceeds 15-minute local buffer limit.")
                self._buffer.extend(frame.audio)
            if self._audio_passthrough:
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, (StopFrame, EndFrame, CancelFrame)) and self._buffer:
            audio = bytes(self._buffer)
            self._buffer.clear()
            try:
                text = await asyncio.to_thread(
                    _transcribe_waveform,
                    audio,
                    sample_rate=self.sample_rate,
                    model_card=self._model_card,
                    language=self._language,
                )
                if text:
                    await self.push_frame(
                        TranscriptionFrame(text=text, user_id="", timestamp=time_now_iso8601(), result=None)
                    )
            except Exception as exc:
                logger.exception("Meta Omnilingual ASR inference failed")
                await self.push_frame(ErrorFrame(error=f"omnilingual_asr error: {exc}"))
        await self.push_frame(frame, direction)

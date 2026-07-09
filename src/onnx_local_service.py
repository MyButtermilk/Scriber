"""
OnnxLocalSTTService: Pipecat-compatible STT service using local ONNX models.
"""
from __future__ import annotations

from typing import AsyncGenerator, Optional

from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    STTMuteFrame,
    STTUpdateSettingsFrame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService
from pipecat.services.settings import STTSettings, is_given
from pipecat.services.stt_service import SegmentedSTTService, STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.onnx_stt import is_onnx_available, transcribe_audio_bytes


def _language_to_code(language: Optional[Language] | str | None) -> str:
    if not language:
        return "auto"
    if isinstance(language, Language):
        return str(language).split("-")[0]
    return str(language).strip() or "auto"


class OnnxLocalSTTService(SegmentedSTTService):
    """Local ONNX STT service using onnx-asr models."""

    def __init__(
        self,
        *,
        model_name: str,
        language: Optional[str] = "auto",
        quantization: str = "int8",
        **kwargs,
    ):
        if not is_onnx_available():
            raise ImportError(
                "onnx-asr library not installed. Install with: pip install onnx-asr[cpu,hub]"
            )

        resolved_language = _language_to_code(language)
        super().__init__(
            settings=STTSettings(
                model=model_name,
                language=None if resolved_language == "auto" else resolved_language,
            ),
            **kwargs,
        )
        self._model_name = model_name
        self._language = resolved_language
        self._quantization = quantization
        self._local_settings = {
            "model": self._model_name,
            "language": self._language,
            "quantization": self._quantization,
        }

        logger.info(
            f"OnnxLocalSTTService initialized (model={self._model_name}, quantization={self._quantization})"
        )

    async def set_language(self, language: Language):
        self._language = _language_to_code(language)
        self._local_settings["language"] = self._language

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            text = await transcribe_audio_bytes(
                audio,
                sample_rate=self.sample_rate,
                model_name=self._model_name,
                language=self._language,
                quantization=self._quantization,
                use_vad=False,  # Segmentation already handled by Pipecat VAD
            )
            text = (text or "").strip()
            if text:
                yield TranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    result=None,
                )
        except Exception as exc:
            logger.error(f"OnnxLocalSTTService error: {exc}")
            yield ErrorFrame(error=f"onnx_local error: {exc}")


class OnnxLocalBufferedSTTService(STTService):
    """Buffer audio and transcribe on stop for manual start/stop recordings."""

    def __init__(
        self,
        *,
        model_name: str,
        language: Optional[str] = "auto",
        quantization: str = "int8",
        sample_rate: int = 16000,
        channels: int = 1,
        max_buffer_secs: int = 300,
        **kwargs,
    ):
        if not is_onnx_available():
            raise ImportError(
                "onnx-asr library not installed. Install with: pip install onnx-asr[cpu,hub]"
            )

        resolved_language = _language_to_code(language)
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(
                model=model_name,
                language=None if resolved_language == "auto" else resolved_language,
            ),
            **kwargs,
        )
        self._model_name = model_name
        self._language = resolved_language
        self._quantization = quantization
        self._local_settings = {
            "model": self._model_name,
            "language": self._language,
            "quantization": self._quantization,
        }
        self._buffer = bytearray()
        self._channels = max(1, int(channels or 1))
        self._max_buffer_secs = max(5, int(max_buffer_secs))
        self._max_buffer_bytes = 0
        self._min_flush_secs = 0.2

        logger.info(
            f"OnnxLocalBufferedSTTService initialized (model={self._model_name}, quantization={self._quantization})"
        )

    async def start(self, frame):
        await super().start(frame)
        self._max_buffer_bytes = int(self.sample_rate * self._channels * 2 * self._max_buffer_secs)
        self._buffer.clear()

    async def set_language(self, language: Language):
        self._language = _language_to_code(language)
        self._local_settings["language"] = self._language

    async def process_audio_frame(self, frame: AudioRawFrame, direction: FrameDirection):
        if self._muted:
            return

        if hasattr(frame, "user_id"):
            self._user_id = frame.user_id
        else:
            self._user_id = ""

        if not frame.audio:
            return

        if getattr(frame, "num_channels", None):
            self._channels = max(1, int(frame.num_channels or self._channels))

        self._buffer.extend(frame.audio)
        if self._max_buffer_bytes and len(self._buffer) > self._max_buffer_bytes:
            excess = len(self._buffer) - self._max_buffer_bytes
            if excess > 0:
                del self._buffer[:excess]

    async def _flush_buffer(self):
        if self._muted:
            self._buffer.clear()
            return
        if not self._buffer:
            return
        min_bytes = int(self.sample_rate * self._channels * 2 * self._min_flush_secs)
        if len(self._buffer) < min_bytes:
            logger.debug("OnnxLocal buffered audio too short to transcribe")
            self._buffer.clear()
            return

        audio_bytes = bytes(self._buffer)
        self._buffer.clear()
        await self.process_generator(self.run_stt(audio_bytes))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Keep terminal frames under this service's control so buffered audio is
        # transcribed before EndFrame reaches downstream processors. AIService
        # owns the Pipecat 1.5 lifecycle; STTService would also forward frames.
        await AIService.process_frame(self, frame, direction)

        if isinstance(frame, AudioRawFrame):
            await self.process_audio_frame(frame, direction)
            if self._audio_passthrough:
                await self.push_frame(frame, direction)
            return

        if isinstance(frame, STTUpdateSettingsFrame):
            if frame.service is not None and frame.service is not self:
                await self.push_frame(frame, direction)
                return
            if frame.delta is not None:
                await self._update_settings(frame.delta)
                if is_given(frame.delta.language):
                    self._language = _language_to_code(frame.delta.language)
                    self._local_settings["language"] = self._language
                if is_given(frame.delta.model) and frame.delta.model:
                    self._model_name = str(frame.delta.model)
                    self._local_settings["model"] = self._model_name
            return
        if isinstance(frame, STTMuteFrame):
            self._muted = frame.mute
            return

        if isinstance(frame, (StopFrame, EndFrame, CancelFrame)):
            await self._flush_buffer()

        await self.push_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            text = await transcribe_audio_bytes(
                audio,
                sample_rate=self.sample_rate,
                model_name=self._model_name,
                language=self._language,
                quantization=self._quantization,
                use_vad=False,
            )
            text = (text or "").strip()
            if text:
                yield TranscriptionFrame(
                    text=text,
                    user_id=self._user_id,
                    timestamp=time_now_iso8601(),
                    result=None,
                )
        except Exception as exc:
            logger.error(f"OnnxLocalBufferedSTTService error: {exc}")
            yield ErrorFrame(error=f"onnx_local error: {exc}")

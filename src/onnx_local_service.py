"""
OnnxLocalSTTService: Pipecat-compatible STT service using local ONNX models.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StopFrame,
    STTMuteFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService
from pipecat.services.settings import STTSettings, is_given
from pipecat.services.stt_service import SegmentedSTTService, STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.onnx_stt import is_onnx_available, start_model_preload, transcribe_audio_bytes

# A completed chunk is cut in the quietest window of its tail so no word is split.
_CHUNK_SPLIT_WINDOW_SECONDS = 0.1
_CHUNK_SPLIT_SEARCH_MAX_SECONDS = 10.0


def _quietest_split_offset(buffer: bytes | bytearray, *, sample_rate: int, channels: int, search_seconds: float) -> int:
    """Return a frame-aligned byte offset inside the quietest window of the buffer tail."""
    frame_bytes = 2 * max(1, channels)
    window_frames = max(1, int(sample_rate * _CHUNK_SPLIT_WINDOW_SECONDS))
    window_bytes = window_frames * frame_bytes
    usable = len(buffer) - len(buffer) % frame_bytes
    windows = min(usable, int(sample_rate * search_seconds) * frame_bytes) // window_bytes
    if windows < 1:
        return usable
    start = usable - windows * window_bytes
    samples = np.frombuffer(buffer, dtype=np.int16, count=windows * window_bytes // 2, offset=start)
    energy = np.square(samples.astype(np.float32)).reshape(windows, -1).mean(axis=1)
    # Of equally quiet windows take the latest, so the chunk stays as long as allowed.
    quietest = windows - 1 - int(np.argmin(energy[::-1]))
    return start + quietest * window_bytes + (window_frames // 2) * frame_bytes


def _language_to_code(language: Language | None | str | None) -> str:
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
        language: str | None = "auto",
        quantization: str = "int8",
        **kwargs,
    ):
        if not is_onnx_available():
            raise ImportError("onnx-asr library not installed. Install with: pip install onnx-asr[cpu,hub]")

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

        logger.info(f"OnnxLocalSTTService initialized (model={self._model_name}, quantization={self._quantization})")

    async def set_language(self, language: Language):
        self._language = _language_to_code(language)
        self._local_settings["language"] = self._language

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame]:
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
    """Buffer audio and transcribe on stop for manual start/stop recordings.

    Recordings longer than ``max_buffer_secs`` are transcribed in completed
    chunks, so no recorded speech is dropped.
    """

    def __init__(
        self,
        *,
        model_name: str,
        language: str | None = "auto",
        quantization: str = "int8",
        sample_rate: int = 16000,
        channels: int = 1,
        max_buffer_secs: int = 300,
        **kwargs,
    ):
        if not is_onnx_available():
            raise ImportError("onnx-asr library not installed. Install with: pip install onnx-asr[cpu,hub]")

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
        self._skip_terminal_transcription = False
        self._model_preload = None

        logger.info(
            f"OnnxLocalBufferedSTTService initialized (model={self._model_name}, quantization={self._quantization})"
        )

    async def start(self, frame):
        await super().start(frame)
        self._max_buffer_bytes = int(self.sample_rate * self._channels * 2 * self._max_buffer_secs)
        self._buffer.clear()
        self._skip_terminal_transcription = False
        # Load the model while the user speaks instead of after stop.
        try:
            self._model_preload = start_model_preload(self._model_name, self._quantization)
        except RuntimeError as exc:
            logger.debug("ONNX model preload unavailable (error_type={})", type(exc).__name__)

    def _reset_buffer(self) -> None:
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
            # File transports feed hours of decoded PCM faster than real time,
            # and long dictations exceed one chunk too. Transcribe a completed
            # chunk and keep the rest; dropping the oldest PCM lost speech.
            await self._flush_completed_chunk()

    async def _flush_completed_chunk(self) -> None:
        cut = _quietest_split_offset(
            self._buffer,
            sample_rate=self.sample_rate,
            channels=self._channels,
            search_seconds=min(_CHUNK_SPLIT_SEARCH_MAX_SECONDS, self._max_buffer_secs / 3),
        )
        audio_bytes = bytes(self._buffer[:cut])
        del self._buffer[:cut]
        await self._transcribe_chunk(audio_bytes)

    async def _flush_buffer(self):
        audio_bytes = bytes(self._buffer)
        self._buffer.clear()
        await self._transcribe_chunk(audio_bytes)

    async def _transcribe_chunk(self, audio_bytes: bytes) -> None:
        if self._muted or not audio_bytes:
            return
        min_bytes = int(self.sample_rate * self._channels * 2 * self._min_flush_secs)
        if len(audio_bytes) < min_bytes:
            logger.debug("OnnxLocal buffered audio too short to transcribe")
            return
        await self.process_generator(self.run_stt(audio_bytes))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Keep terminal frames under this service's control so buffered audio is
        # transcribed before EndFrame reaches downstream processors. AIService
        # owns the Pipecat lifecycle; STTService would also forward frames.
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

        if isinstance(frame, CancelFrame) or (
            isinstance(frame, (StopFrame, EndFrame)) and self._skip_terminal_transcription
        ):
            # Cancelled and aborted sessions discard their PCM without running
            # inference, like the other buffered providers.
            self._buffer.clear()
        elif isinstance(frame, (StopFrame, EndFrame)):
            await self._flush_buffer()

        await self.push_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame]:
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

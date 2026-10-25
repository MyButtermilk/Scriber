import types
from dataclasses import dataclass
from unittest.mock import AsyncMock

import numpy as np
import pytest
from pipecat.frames.frames import (
    CancelFrame,
    DataFrame,
    InputAudioRawFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings
from pipecat.tests.utils import run_test
from pipecat.transcriptions.language import Language

from src import onnx_local_service
from src.pipeline import ScriberPipeline

_RATE = 1000
_FRAME_SECONDS = 0.1


def _speech(seconds: float) -> bytes:
    samples = round(seconds * _RATE)
    return (np.sin(np.arange(samples) * 0.3) * 8000).astype(np.int16).tobytes()


def _silence(seconds: float) -> bytes:
    return bytes(2 * round(seconds * _RATE))


def _audio_frames(audio: bytes) -> list[InputAudioRawFrame]:
    step = 2 * int(_RATE * _FRAME_SECONDS)
    return [
        InputAudioRawFrame(audio=audio[offset : offset + step], sample_rate=_RATE, num_channels=1)
        for offset in range(0, len(audio), step)
    ]


@pytest.fixture
def recognized(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)
    preloads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        onnx_local_service,
        "start_model_preload",
        lambda model_name, quantization: preloads.append((model_name, quantization)),
    )
    chunks: list[bytes] = []

    async def transcribe(audio_bytes, **_kwargs):
        chunks.append(audio_bytes)
        return f"chunk {len(chunks)}"

    monkeypatch.setattr(onnx_local_service, "transcribe_audio_bytes", transcribe)
    return chunks, preloads


def _live_service(max_buffer_secs: int = 300):
    return onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="nemo-canary-1b-v2",
        language="de",
        sample_rate=_RATE,
        max_buffer_secs=max_buffer_secs,
    )


def test_onnx_services_initialize_complete_pipecat_1_5_settings(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)

    segmented = onnx_local_service.OnnxLocalSTTService(
        model_name="test-model",
        language="auto",
    )
    buffered = onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="test-model",
        language="de",
    )

    assert segmented._settings.model == "test-model"
    assert segmented._settings.language is None
    assert buffered._settings.model == "test-model"
    assert buffered._settings.language == "de"


@pytest.mark.asyncio
async def test_buffered_onnx_service_consumes_pipecat_1_5_settings_delta(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)
    service = onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="initial-model",
        language="auto",
    )
    service._update_settings = AsyncMock(return_value={})
    service.push_frame = AsyncMock()
    delta = STTSettings(model="replacement-model", language=Language.DE)

    await service.process_frame(
        STTUpdateSettingsFrame(delta=delta, service=service),
        FrameDirection.DOWNSTREAM,
    )

    service._update_settings.assert_awaited_once_with(delta)
    service.push_frame.assert_not_awaited()
    assert service._model_name == "replacement-model"
    assert service._language == "de"
    assert service._local_settings == {
        "model": "replacement-model",
        "language": "de",
        "quantization": "int8",
    }


@pytest.mark.asyncio
async def test_buffered_onnx_file_service_flushes_instead_of_dropping_full_chunks(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)
    service = onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="parakeet-primeline",
        language="de",
        quantization="int8",
        sample_rate=10,
        channels=1,
        max_buffer_secs=5,
    )
    service._max_buffer_bytes = 100
    flushed = []

    async def capture_flush():
        flushed.append(bytes(service._buffer))
        service._buffer.clear()

    service._flush_completed_chunk = capture_flush

    await service.process_audio_frame(
        InputAudioRawFrame(audio=b"a" * 101, sample_rate=10, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    assert flushed == [b"a" * 101]
    assert service._buffer == bytearray()


@pytest.mark.asyncio
async def test_long_live_dictation_keeps_every_word_in_completed_chunks(recognized):
    chunks, preloads = recognized
    # Live chunks are five minutes long; this dictation lasts 12 minutes.
    audio = _speech(293) + _silence(0.3) + _speech(294.7) + _silence(0.3) + _speech(131.4)

    down, _up = await run_test(_live_service(), frames_to_send=_audio_frames(audio))

    # The previous buffer discarded everything beyond five minutes but the
    # last five; every recorded sample now reaches the model exactly once.
    assert b"".join(chunks) == audio
    assert len(chunks) == 3
    # Completed chunks end inside the pauses, so no word is split.
    first_cut = len(chunks[0]) / 2 / _RATE
    second_cut = first_cut + len(chunks[1]) / 2 / _RATE
    assert 293.0 <= first_cut <= 293.3
    assert 588.0 <= second_cut <= 588.3
    assert [frame.text for frame in down if isinstance(frame, TranscriptionFrame)] == [
        "chunk 1",
        "chunk 2",
        "chunk 3",
    ]
    # The model is loaded while the user speaks, not after stop.
    assert preloads == [("nemo-canary-1b-v2", "int8")]


def test_quietest_split_prefers_the_latest_equal_pause():
    audio = _speech(1) + _silence(0.2) + _speech(1) + _silence(0.2) + _speech(0.5)

    cut = onnx_local_service._quietest_split_offset(audio, sample_rate=_RATE, channels=1, search_seconds=3)

    assert 2.2 <= cut / 2 / _RATE <= 2.4
    assert cut % 2 == 0


@pytest.mark.asyncio
async def test_cancelled_live_dictation_discards_audio_without_inference(recognized):
    chunks, _preloads = recognized

    down, _up = await run_test(
        _live_service(),
        frames_to_send=[*_audio_frames(_speech(3)), CancelFrame()],
        send_end_frame=False,
    )

    assert chunks == []
    assert not any(isinstance(frame, TranscriptionFrame) for frame in down)


@dataclass
class _AbortRequested(DataFrame):
    pass


class _AbortMarker(FrameProcessor):
    def __init__(self, scriber: ScriberPipeline):
        super().__init__()
        self._scriber = scriber

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, _AbortRequested):
            # The pipeline marks buffered providers before an abort or a silent cancel.
            self._scriber._mark_provider_terminal_transcription_skip()
            return
        await self.push_frame(frame, direction)


@pytest.mark.asyncio
async def test_aborted_live_dictation_skips_terminal_inference(recognized):
    chunks, _preloads = recognized
    service = _live_service()
    scriber = ScriberPipeline(service_name="onnx_local")
    scriber.pipeline = types.SimpleNamespace(processors=[service])

    down, _up = await run_test(
        Pipeline([_AbortMarker(scriber), service]),
        frames_to_send=[*_audio_frames(_speech(3)), _AbortRequested()],
    )

    assert chunks == []
    assert not any(isinstance(frame, TranscriptionFrame) for frame in down)

import wave
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, InputAudioRawFrame, StopFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from src.assemblyai_async_stt import AssemblyAIUniversal35ProAsyncProcessor
from src.cloud_async_stt import SpeechmaticsAsyncProcessor
from src.mistral_stt import MistralAsyncProcessor
from src.modulate_stt import ModulateAsyncProcessor
from src.pipeline import SonioxAsyncProcessor
from src.smallest_stt import SmallestAsyncProcessor


@pytest.mark.parametrize(
    "processor_type,options,transcribe_attribute",
    [
        (AssemblyAIUniversal35ProAsyncProcessor, {}, "_transcribe_wav"),
        (SpeechmaticsAsyncProcessor, {"language": "de"}, "_transcribe_wav"),
        (MistralAsyncProcessor, {"model": "voxtral-mini-2602"}, "_transcribe_wav"),
        (ModulateAsyncProcessor, {}, "_transcribe_wav"),
        (SmallestAsyncProcessor, {}, "_transcribe_wav"),
        (SonioxAsyncProcessor, {}, "_transcribe_async"),
    ],
    ids=["assemblyai", "shared-cloud", "mistral", "modulate", "smallest", "soniox"],
)
@pytest.mark.parametrize("terminal_type", [CancelFrame, EndFrame, StopFrame])
@pytest.mark.asyncio
async def test_buffered_upload_requires_normal_stop(processor_type, options, transcribe_attribute, terminal_type):
    progress = Mock()
    processor = processor_type(api_key="test-key", session=object(), on_progress=progress, **options)
    processor.push_frame = AsyncMock()
    original_buffer = processor._buffer
    pcm = b"\x01\x02" * 160
    uploaded = []

    async def transcribe(wav_source=None, *, audio_stream=None, audio_size=None):
        if audio_stream is not None:
            uploaded.append(audio_stream.read(audio_size))
        else:
            with wave.open(wav_source, "rb") as reader:
                uploaded.append(reader.readframes(reader.getnframes()))
        return "Final transcript."

    setattr(processor, transcribe_attribute, transcribe)
    terminal = terminal_type()
    direction = FrameDirection.DOWNSTREAM
    try:
        await processor.process_frame(InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1), direction)
        await processor.process_frame(terminal, direction)
        # A repeated stop must not recover or resubmit the discarded recording.
        await processor.process_frame(EndFrame(), direction)
        frames = [call.args[0] for call in processor.push_frame.call_args_list]
        assert original_buffer.closed and processor._buffer_size == 0
        assert sum(frame is terminal for frame in frames) == 1
        assert not any(isinstance(frame, ErrorFrame) for frame in frames)
        finals = [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)]
        if terminal_type is CancelFrame:
            assert uploaded == [], "Cancel must discard captured audio before any provider upload"
            assert finals == []
            progress.assert_not_called()
        else:
            assert uploaded == [pcm]
            assert finals == ["Final transcript."]
    finally:
        processor._buffer.close()


@pytest.mark.asyncio
async def test_shared_cloud_cancel_releases_capture_wav_without_opening_or_uploading():
    processor = SpeechmaticsAsyncProcessor(api_key="test-key", language="de", session=object())
    processor.push_frame = AsyncMock()
    processor._transcribe_wav = AsyncMock(return_value="Canceled transcript.")
    artifact = Mock()
    artifact.open_async = AsyncMock(return_value=Mock(spec=["close"]))
    artifact.release_async = AsyncMock()
    assert processor.adopt_capture_wav_artifact(artifact)
    direction = FrameDirection.DOWNSTREAM
    try:
        await processor.process_frame(
            InputAudioRawFrame(audio=b"\x01\x02" * 160, sample_rate=16000, num_channels=1), direction
        )
        await processor.process_frame(CancelFrame(), direction)
        artifact.open_async.assert_not_awaited()
        artifact.release_async.assert_awaited_once()
        processor._transcribe_wav.assert_not_awaited()
        assert not processor.adopt_capture_wav_artifact(artifact)
    finally:
        processor._buffer.close()

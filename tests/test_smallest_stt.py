import wave
from unittest.mock import AsyncMock, patch

import pytest

from pipecat.frames.frames import AudioRawFrame, EndFrame
from pipecat.processors.frame_processor import FrameDirection

from src.smallest_stt import (
    SmallestAsyncProcessor,
    format_smallest_utterances_to_scriber_text,
    smallest_language_code,
    smallest_transcript_payload_to_text,
)


def test_smallest_language_code_maps_auto_to_multieu():
    assert smallest_language_code("auto") == "multi-eu"
    assert smallest_language_code("") == "multi-eu"
    assert smallest_language_code("de-DE") == "de"


def test_smallest_transcript_payload_prefers_speaker_utterances():
    payload = {
        "transcription": "plain",
        "utterances": [
            {"speaker": "speaker_0", "text": "Hello"},
            {"speaker": "speaker_1", "text": "Hi"},
            {"speaker": "speaker_0", "text": "Again"},
        ],
    }

    assert smallest_transcript_payload_to_text(payload, prefer_speaker_labels=True) == (
        "[Speaker 1]: Hello\n\n[Speaker 2]: Hi\n\n[Speaker 1]: Again"
    )
    assert smallest_transcript_payload_to_text(payload, prefer_speaker_labels=False) == "plain"


def test_format_smallest_utterances_without_speakers():
    assert format_smallest_utterances_to_scriber_text([{"text": "Hello"}, {"transcript": "world"}]) == (
        "Hello\n\nworld"
    )


def test_format_smallest_utterances_preserves_numeric_speaker_zero():
    assert format_smallest_utterances_to_scriber_text([
        {"speaker": 0, "text": "First"},
        {"speaker": 1, "text": "Second"},
    ]) == "[Speaker 1]: First\n\n[Speaker 2]: Second"


@pytest.mark.asyncio
async def test_smallest_buffer_finalizes_reserved_wav_in_place():
    processor = SmallestAsyncProcessor(
        api_key="test-key",
        session=object(),  # type: ignore[arg-type]
    )
    original_buffer = processor._buffer
    pcm = b"\x01\x02" * (16_000 * 5)
    observed: dict[str, object] = {}

    async def inspect_wav(wav_source):
        observed["sameBuffer"] = wav_source is original_buffer
        with wave.open(wav_source, "rb") as reader:
            observed["sampleRate"] = reader.getframerate()
            observed["channels"] = reader.getnchannels()
            observed["frames"] = reader.getnframes()
            observed["pcm"] = reader.readframes(reader.getnframes())
        return "done"

    processor._transcribe_wav = inspect_wav  # type: ignore[method-assign]
    processor.push_frame = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "src.smallest_stt.FrameProcessor.process_frame",
        new=AsyncMock(),
    ):
        await processor.process_frame(
            AudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert observed == {
        "sameBuffer": True,
        "sampleRate": 16_000,
        "channels": 1,
        "frames": 16_000 * 5,
        "pcm": pcm,
    }
    assert original_buffer.closed
    assert processor._buffer is not original_buffer
    assert processor._buffer.tell() == 44
    processor._buffer.close()

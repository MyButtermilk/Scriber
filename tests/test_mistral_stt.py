import wave
from unittest.mock import AsyncMock, patch

import pytest

from pipecat.frames.frames import AudioRawFrame, EndFrame
from pipecat.processors.frame_processor import FrameDirection

from src.mistral_stt import (
    MistralAsyncProcessor,
    MistralRealtimeSTTService,
    _normalize_context_bias_terms,
    format_mistral_segments_with_speakers,
)


def test_mistral_realtime_initializes_complete_pipecat_1_5_settings():
    service = MistralRealtimeSTTService(
        api_key="test-key",
        model="voxtral-mini-transcribe-2507",
        language="auto",
    )

    assert service._settings.model == "voxtral-mini-transcribe-2507"
    assert service._settings.language is None


def test_context_bias_terms_strip_spaces_and_split_phrases():
    terms = _normalize_context_bias_terms("Scriber, Soniox, Bayerische Motoren Werke KGaA")
    assert "Scriber" in terms
    assert "Soniox" in terms
    assert "Bayerische" in terms
    assert "Motoren" in terms
    assert "Werke" in terms
    assert "KGaA" in terms


def test_context_bias_terms_deduplicate_case_insensitive():
    terms = _normalize_context_bias_terms("Scriber, scriber, SCRIBER")
    assert terms == ["Scriber"]


def test_format_mistral_segments_with_speakers_groups_contiguous_segments():
    text = format_mistral_segments_with_speakers(
        [
            {"speaker_id": "SPEAKER_00", "text": "Hallo"},
            {"speaker_id": "SPEAKER_00", "text": "weiter"},
            {"speaker_id": "SPEAKER_01", "text": "Guten Tag"},
        ]
    )

    assert text == (
        "[Speaker 1]: Hallo weiter\n\n"
        "[Speaker 2]: Guten Tag"
    )


def test_format_mistral_segments_preserves_numeric_speaker_zero():
    assert format_mistral_segments_with_speakers([
        {"speaker": 0, "text": "First"},
        {"speaker": 1, "text": "Second"},
    ]) == "[Speaker 1]: First\n\n[Speaker 2]: Second"


@pytest.mark.asyncio
async def test_mistral_buffer_finalizes_reserved_wav_in_place():
    processor = MistralAsyncProcessor(
        api_key="test-key",
        model="voxtral-mini-2602",
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
        "src.mistral_stt.FrameProcessor.process_frame",
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


@pytest.mark.asyncio
async def test_mistral_async_processor_enables_diarization(monkeypatch):
    captured: dict = {}

    async def _fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {
            "text": "plain fallback",
            "segments": [
                {"speaker_id": "SPEAKER_00", "text": "Hallo"},
                {"speaker_id": "SPEAKER_01", "text": "Antwort"},
            ],
        }

    monkeypatch.setattr("src.mistral_stt.transcribe_with_mistral", _fake_transcribe)

    processor = MistralAsyncProcessor(
        api_key="test-key",
        model="voxtral-mini-2602",
        language="de",
        custom_vocab="Scriber",
        session=object(),
        diarize=True,
    )

    text = await processor._transcribe_bytes(b"\0" * 320)

    assert captured["diarize"] is True
    assert captured["timestamp_granularities"] == ["segment"]
    assert captured["language"] == "de"
    assert text == "[Speaker 1]: Hallo\n\n[Speaker 2]: Antwort"

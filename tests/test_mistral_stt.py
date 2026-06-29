import pytest

from src.mistral_stt import (
    MistralAsyncProcessor,
    _normalize_context_bias_terms,
    format_mistral_segments_with_speakers,
)


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
        "[Speaker SPEAKER_00]: Hallo weiter\n\n"
        "[Speaker SPEAKER_01]: Guten Tag"
    )


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
    assert text == "[Speaker SPEAKER_00]: Hallo\n\n[Speaker SPEAKER_01]: Antwort"

from src.smallest_stt import (
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

from src.cloud_async_stt import (
    deepgram_transcript_payload_to_text,
    openai_transcript_payload_to_text,
    speechmatics_transcript_payload_to_text,
)


def test_deepgram_payload_formats_speaker_words():
    payload = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "hello there hi back",
                            "words": [
                                {"speaker": 0, "punctuated_word": "Hello"},
                                {"speaker": 0, "punctuated_word": "there."},
                                {"speaker": 1, "punctuated_word": "Hi"},
                                {"speaker": 1, "punctuated_word": "back."},
                            ],
                        }
                    ]
                }
            ]
        }
    }

    assert deepgram_transcript_payload_to_text(payload, prefer_speaker_labels=True) == (
        "[Speaker 1]: Hello there.\n\n[Speaker 2]: Hi back."
    )


def test_openai_payload_uses_text_fallback():
    assert (
        openai_transcript_payload_to_text({"text": "plain transcript"}, prefer_speaker_labels=True)
        == "plain transcript"
    )


def test_speechmatics_payload_builds_text_from_results():
    payload = {
        "results": [
            {"type": "word", "alternatives": [{"content": "Hello"}]},
            {"type": "word", "alternatives": [{"content": "world"}]},
            {"type": "punctuation", "alternatives": [{"content": "."}]},
        ]
    }

    assert speechmatics_transcript_payload_to_text(payload, prefer_speaker_labels=False) == "Hello world."


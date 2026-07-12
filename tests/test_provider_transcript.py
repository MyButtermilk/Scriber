from src.provider_transcript import normalize_provider_segments, normalize_provider_words


def test_soniox_tokens_preserve_exact_timing_and_speaker_turns():
    payload = {"tokens": [
        {"text": " Hello", "start_ms": 120, "end_ms": 300, "speaker": "1", "confidence": 0.9},
        {"text": " world.", "start_ms": 310, "end_ms": 600, "speaker": "1", "confidence": 0.8},
        {"text": " Yes.", "start_ms": 700, "end_ms": 900, "speaker": "2", "confidence": 1.0},
    ]}

    result = normalize_provider_segments("soniox_async", payload, "system", 5_000)

    assert [(item["startMs"], item["endMs"]) for item in result] == [(5_120, 5_600), (5_700, 5_900)]
    assert [item["speakerLabel"] for item in result] == ["Speaker 1", "Speaker 2"]
    assert result[0]["text"] == "Hello world."
    assert result[0]["providerSegmentId"].startswith("provider-exact-")
    assert result[0]["alignmentQuality"] == "exact_word"


def test_assemblyai_utterances_are_already_canonical_turns():
    payload = {"utterances": [{
        "speaker": "A", "text": "A decision", "start": 250, "end": 1200, "confidence": 0.95
    }]}

    result = normalize_provider_segments("assemblyai", payload, "system", 10_000)

    assert result[0] | {"confidence": 0.95} == result[0]
    assert result[0]["speakerLabel"] == "Speaker 1"
    assert (result[0]["startMs"], result[0]["endMs"], result[0]["text"]) == (
        10_250, 11_200, "A decision"
    )
    assert result[0]["alignmentQuality"] == "provider_segment"


def test_deepgram_words_use_seconds_and_insert_word_spacing():
    payload = {"results": {"channels": [{"alternatives": [{"words": [
        {"word": "hello", "start": 1.0, "end": 1.2, "speaker": 0},
        {"punctuated_word": "world.", "start": 1.25, "end": 1.6, "speaker": 0},
    ]}]}]}}

    result = normalize_provider_segments("deepgram_async", payload, "system")

    assert result[0]["text"] == "hello world."
    assert result[0]["speakerLabel"] == "Speaker 1"
    assert (result[0]["startMs"], result[0]["endMs"]) == (1_000, 1_600)
    assert result[0]["alignmentQuality"] == "exact_word"


def test_mistral_provider_segments_are_not_misrepresented_as_word_exact():
    result = normalize_provider_segments(
        "mistral",
        {"segments": [{"text": "One provider interval", "start": 1.5, "end": 2.5}]},
        "system",
    )

    assert result[0]["alignmentQuality"] == "provider_segment"


def test_missing_provider_timing_returns_no_fake_exact_segments():
    assert normalize_provider_segments("openai_async", {"text": "hello"}, "microphone") == []


def test_smallest_words_use_seconds_and_preserve_speaker_zero():
    payload = {"words": [
        {"word": "Hello", "start": 0.25, "end": 0.5, "speaker": "speaker_0", "confidence": 0.9},
        {"word": "there.", "start": 0.55, "end": 0.9, "speaker": "speaker_0", "confidence": 0.8},
        {"word": "Hi.", "start": 1.0, "end": 1.2, "speaker": "speaker_1", "confidence": 1.0},
    ]}

    result = normalize_provider_segments("smallest_async", payload, "system", 2_000)

    assert [(item["startMs"], item["endMs"]) for item in result] == [
        (2_250, 2_900),
        (3_000, 3_200),
    ]
    assert [item["speakerLabel"] for item in result] == ["Speaker 1", "Speaker 2"]
    assert all(item["alignmentQuality"] == "exact_word" for item in result)


def test_gladia_utterances_preserve_provider_intervals():
    payload = {"result": {"transcription": {"utterances": [
        {"speaker": 0, "text": "First turn", "start": 0.73341, "end": 2.364},
        {"speaker": 1, "text": "Second turn", "start": 2.5, "end": 3.125},
    ]}}}

    result = normalize_provider_segments("gladia_async", payload, "system")

    assert [(item["startMs"], item["endMs"]) for item in result] == [
        (733, 2_364),
        (2_500, 3_125),
    ]
    assert [item["speakerLabel"] for item in result] == ["Speaker 1", "Speaker 2"]
    assert all(item["alignmentQuality"] == "provider_segment" for item in result)


def test_speechmatics_json_v2_words_keep_seconds_speakers_and_punctuation():
    payload = {"results": [
        {
            "type": "word", "start_time": 0.36, "end_time": 0.51,
            "alternatives": [{"content": "Hello", "speaker": "S1", "confidence": 0.93}],
        },
        {
            "type": "punctuation", "start_time": 0.51, "end_time": 0.51,
            "alternatives": [{"content": ",", "speaker": "S1", "confidence": 1.0}],
        },
        {
            "type": "word", "start_time": 0.56, "end_time": 0.8,
            "alternatives": [{"content": "there", "speaker": "S1", "confidence": 0.91}],
        },
        {
            "type": "word", "start_time": 1.0, "end_time": 1.2,
            "alternatives": [{"content": "Hi", "speaker": "S2", "confidence": 0.95}],
        },
    ]}

    result = normalize_provider_segments("speechmatics_async", payload, "system", 1_000)

    assert result[0]["text"] == "Hello, there"
    assert (result[0]["startMs"], result[0]["endMs"]) == (1_360, 1_800)
    assert result[1]["speakerLabel"] == "Speaker 2"
    assert result[1]["alignmentQuality"] == "exact_word"


def test_azure_mai_phrases_preserve_real_provider_intervals_without_claiming_word_precision():
    payload = {"phrases": [
        {
            "text": "Phrase one", "offsetMilliseconds": 760,
            "durationMilliseconds": 1_320, "confidence": 0.75,
        }
    ]}

    result = normalize_provider_segments("azure_mai", payload, "system", 5_000)

    assert (result[0]["startMs"], result[0]["endMs"]) == (5_760, 7_080)
    assert result[0]["alignmentQuality"] == "provider_segment"


def test_openai_diarized_json_segments_are_provider_timed_not_word_exact():
    payload = {"segments": [
        {"id": "seg_002", "speaker": "A", "start": 5.2, "end": 12.8, "text": "Need help."}
    ]}

    result = normalize_provider_segments("openai_async", payload, "system")

    assert (result[0]["startMs"], result[0]["endMs"]) == (5_200, 12_800)
    assert result[0]["speakerLabel"] == "Speaker 1"
    assert result[0]["alignmentQuality"] == "provider_segment"


def test_local_alignment_words_support_all_normalized_batch_shapes():
    smallest = normalize_provider_words(
        "smallest", {"words": [{"word": "one", "start": 0.1, "end": 0.2}]}, 1_000
    )
    speechmatics = normalize_provider_words(
        "speechmatics_async",
        {"results": [{
            "type": "word", "start_time": 0.1, "end_time": 0.2,
            "alternatives": [{"content": "one", "speaker": 0}],
        }]},
        1_000,
    )
    gladia = normalize_provider_words(
        "gladia", {"result": {"transcription": {"utterances": [
            {"text": "one", "start": 0.1, "end": 0.2, "speaker": 0}
        ]}}}, 1_000
    )

    assert [(item["startMs"], item["endMs"], item["alignmentQuality"]) for item in smallest] == [
        (1_100, 1_200, "exact_word")
    ]
    assert speechmatics[0]["speaker"] == "0"
    assert (speechmatics[0]["startMs"], speechmatics[0]["endMs"]) == (1_100, 1_200)
    assert gladia[0]["alignmentQuality"] == "provider_segment"

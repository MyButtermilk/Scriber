"""Boundary correctness without provider calls, clock access, or audio fixtures."""

import copy
import math

import pytest

from src.provider_transcript import normalize_provider_segments, normalize_provider_words
from src.transcription_merge import (
    PartTranscript,
    TranscriptWord,
    merge_part_transcripts,
    part_transcript_from_payload,
)


def word(text, start, end=None, speaker=None, confidence=None):
    return TranscriptWord(text, start, start + 200 if end is None else end, speaker, confidence)


def part(index, words=(), *, text=None, start=None, end=None):
    core_start = index * 10_000
    core_end = core_start + 10_000
    return PartTranscript(
        index,
        max(0, core_start - 4_000) if start is None else start,
        core_end + 4_000 if end is None else end,
        core_start,
        core_end,
        " ".join(item.text for item in words) if text is None else text,
        tuple(words),
    )


def texts(result):
    return [item.text for item in result.words]


def codes(result):
    return [warning.code for warning in result.boundary_warnings]


def test_same_words_in_overlap_have_one_copy_on_original_timeline():
    left = part(0, [word("Guten", 1_000), word("Morgen,", 8_000), word("Anna.", 11_000)])
    right = part(1, [word("Morgen,", 8_025), word("Anna.", 11_010), word("Willkommen!", 17_000)])

    result = merge_part_transcripts([left, right])

    assert result.text == "Guten Morgen, Anna. Willkommen!"
    assert len(result.words) == 4
    assert [(item.start_ms, item.end_ms) for item in result.words] == [
        (1_000, 1_200),
        (8_000, 8_200),
        (11_010, 11_210),
        (17_000, 17_200),
    ]
    assert result.word_timing_complete
    assert not result.boundary_warnings


def test_same_sentence_at_different_times_is_not_deduplicated():
    left = part(0, [word("Ja", 7_000), word("bitte.", 7_500), word("Ja", 9_000), word("bitte.", 9_500)])
    right = part(1, [word("Ja", 9_000), word("bitte.", 9_500), word("Ja", 12_000), word("bitte.", 12_500)])

    result = merge_part_transcripts([left, right])

    assert result.text == "Ja bitte. Ja bitte. Ja bitte."
    assert len(result.words) == 6


def test_adjacent_repeated_words_with_disjoint_times_survive():
    result = merge_part_transcripts([part(0, [word("ja", 9_000)]), part(1, [word("ja", 9_200)])])

    assert result.text == "ja ja"
    assert len(result.words) == 2


def test_duplicate_words_within_one_request_are_never_removed():
    result = merge_part_transcripts([part(0, [word("ja", 9_000), word("ja", 9_000)])])

    assert result.text == "ja ja"


def test_overbroad_word_timing_does_not_prove_a_repeated_word_is_the_same_event():
    result = merge_part_transcripts([part(0, [word("ja", 8_000, 10_000)]), part(1, [word("ja", 9_000, 9_200)])])

    assert result.text == "ja ja"
    assert codes(result) == ["overlap_conflicting_words"]


def test_time_constrained_alignment_preserves_repetition_count():
    left = part(0, [word("ja", 9_000), word("ja", 9_220), word("ja", 9_440)])
    right = part(1, [word("ja", 9_225), word("ja", 9_445), word("ja", 9_660)])

    result = merge_part_transcripts([left, right])

    assert result.text == "ja ja ja ja"
    assert len(result.words) == 4


@pytest.mark.parametrize(
    "before,after",
    [("15", "50"), ("Maße", "Masse"), ("Anna", "Anne"), ("-5", "5"), ("1.500", "1500"), (".5", "5"), (",5", "5")],
)
def test_names_numbers_and_german_distinctions_preserve_conflicting_recognition(before, after):
    result = merge_part_transcripts([part(0, [word(before, 9_000)]), part(1, [word(after, 9_010)])])

    assert texts(result) == [before, after]
    assert codes(result) == ["overlap_conflicting_words"]
    assert result.boundary_warnings[0].start_ms == 6_000
    assert result.boundary_warnings[0].end_ms == 14_000


def test_unmatched_alternative_next_to_matched_word_is_still_warned():
    result = merge_part_transcripts(
        [
            part(0, [word("eins", 9_000)]),
            part(1, [word("eins", 9_000), word("zwei", 9_010)]),
        ]
    )

    assert texts(result) == ["eins", "zwei"]
    assert codes(result) == ["overlap_conflicting_words"]


def test_recovery_of_explicitly_clipped_german_compound_uses_interior_word():
    result = merge_part_transcripts(
        [
            part(0, [word("Gesellschafts-", 13_700, 13_990)]),
            part(1, [word("Gesellschaftsvertrag", 13_700, 14_200)]),
        ]
    )

    assert result.text == "Gesellschaftsvertrag"
    assert [(item.start_ms, item.end_ms) for item in result.words] == [(13_700, 14_200)]
    assert not result.boundary_warnings


@pytest.mark.parametrize("fragment", ["Gesellschaft", "Gesellschafts-"])
def test_unproven_fragment_or_interior_hyphen_is_preserved(fragment):
    result = merge_part_transcripts(
        [
            part(0, [word(fragment, 9_000)]),
            part(1, [word("Gesellschaftsvertrag", 9_000)]),
        ]
    )

    assert texts(result) == [fragment, "Gesellschaftsvertrag"]
    assert codes(result) == ["overlap_conflicting_words"]


def test_different_tokenization_of_compound_is_not_guessed():
    result = merge_part_transcripts(
        [
            part(0, [word("Haus", 9_000), word("tür", 9_200)]),
            part(1, [word("Haustür", 9_000, 9_400)]),
        ]
    )

    assert "Haus" in texts(result) and "Haustür" in texts(result) and "tür" in texts(result)
    assert codes(result) == ["overlap_conflicting_words"]


def test_upload_edge_reliability_beats_confidence_and_preserves_punctuation():
    result = merge_part_transcripts(
        [
            part(0, [word("Vertrag", 13_700, 13_950, confidence=0.99)]),
            part(1, [word("Vertrag.", 13_720, 13_970, confidence=0.5)]),
        ]
    )

    assert texts(result) == ["Vertrag."]


def test_sentence_case_and_unicode_composition_match_without_rewriting():
    result = merge_part_transcripts(
        [
            part(0, [word("MÜLLER!", 9_000)]),
            part(1, [word("Mu\u0308ller.", 9_000)]),
        ]
    )

    assert len(result.words) == 1
    assert result.words[0].text == "MÜLLER!"


def test_silence_does_not_create_a_word_or_warning():
    result = merge_part_transcripts(
        [
            part(0, [word("Vorher.", 1_000)]),
            part(1),
            part(2, [word("Nachher.", 27_000)]),
        ]
    )

    assert result.text == "Vorher. Nachher."
    assert len(result.words) == 2
    assert not result.boundary_warnings


def test_timestamp_free_overlap_preserves_full_repeated_text_without_fake_words():
    result = merge_part_transcripts(
        [
            part(0, text="Das ist richtig. Ja, ja."),
            part(1, text="Ja, ja. Das ist richtig."),
        ]
    )

    assert result.text == "Das ist richtig. Ja, ja.\n\nJa, ja. Das ist richtig."
    assert result.words == ()
    assert not result.word_timing_complete
    assert codes(result) == ["overlap_missing_word_timestamps"]
    assert "overlap" not in result.text
    payload = result.to_dict()
    assert payload["_scriberMerged"]["alignmentQuality"] == "estimated"
    assert payload["_scriberMerged"]["words"] == []
    assert payload["text"] == result.text


def test_incomplete_word_coverage_cannot_delete_unaligned_spoken_text():
    result = merge_part_transcripts(
        [
            part(0, [word("Hallo", 9_000)], text="Hallo Herr Müller"),
            part(1, [word("Müller", 9_500), word("spricht.", 11_000)]),
        ]
    )

    assert result.text == "Hallo Herr Müller\n\nMüller spricht."
    assert not result.word_timing_complete
    assert codes(result) == ["overlap_missing_word_timestamps"]


def test_out_of_order_provider_words_preserve_original_full_text_as_fallback():
    result = merge_part_transcripts([part(0, [word("später", 9_000), word("früher", 8_000)])])

    assert result.text == "später früher"
    assert result.words == ()
    assert not result.word_timing_complete


def test_timed_runs_still_merge_on_either_side_of_untimed_part():
    result = merge_part_transcripts(
        [
            part(0, [word("Erstens", 9_000)]),
            part(1, [word("Erstens", 9_000), word("zweitens", 19_000)]),
            part(2, text="zweitens drittens"),
            part(3, [word("viertens", 39_000)]),
            part(4, [word("viertens", 39_000), word("fertig.", 45_000)]),
        ]
    )

    assert result.text == "Erstens zweitens\n\nzweitens drittens\n\nviertens fertig."
    assert texts(result) == ["Erstens", "zweitens", "viertens", "fertig."]
    assert not result.word_timing_complete
    assert codes(result) == ["overlap_missing_word_timestamps", "overlap_missing_word_timestamps"]


def test_nonoverlapping_untimed_parts_need_no_duplicate_warning():
    result = merge_part_transcripts([part(0, text="Hallo", end=10_000), part(1, text="Hallo", start=10_000)])

    assert result.text == "Hallo\n\nHallo"
    assert not result.boundary_warnings


def test_request_local_speaker_zero_is_not_assumed_to_be_global():
    result = merge_part_transcripts(
        [
            part(0, [word("Alice", 8_000, speaker="0")]),
            part(1, [word("Bob", 12_000, speaker="0")]),
        ]
    )

    assert [item.speaker for item in result.words] == ["part-0:0", "part-1:0"]


def test_two_matching_words_prove_speaker_continuity_even_if_ids_change():
    result = merge_part_transcripts(
        [
            part(0, [word("Guten", 9_000, speaker="0"), word("Tag.", 9_500, speaker="0")]),
            part(
                1,
                [
                    word("Guten", 9_000, speaker="7"),
                    word("Tag.", 9_500, speaker="7"),
                    word("Weiter.", 17_000, speaker="7"),
                ],
            ),
        ]
    )

    assert [item.speaker for item in result.words] == ["part-0:0"] * 3


def test_a_single_matching_word_does_not_prove_speaker_identity():
    result = merge_part_transcripts(
        [
            part(0, [word("Ja.", 9_000, speaker="0")]),
            part(1, [word("Ja.", 9_000, speaker="0"), word("Weiter.", 17_000, speaker="0")]),
        ]
    )

    assert [item.speaker for item in result.words] == ["part-0:0", "part-1:0"]


def test_speaker_switch_does_not_merge_different_local_speakers():
    result = merge_part_transcripts(
        [
            part(0, [word("Guten", 9_000, speaker="0"), word("Tag.", 9_500, speaker="0")]),
            part(
                1,
                [
                    word("Guten", 9_000, speaker="1"),
                    word("Tag.", 9_500, speaker="1"),
                    word("Antwort.", 10_000, speaker="0"),
                ],
            ),
        ]
    )

    assert [item.speaker for item in result.words] == ["part-0:0", "part-0:0", "part-1:0"]


def test_ambiguous_many_to_one_speaker_evidence_does_not_link_identities():
    left = [word("eins", 8_000, speaker="0"), word("zwei", 8_500, speaker="0"), word("drei", 9_000, speaker="1")]
    right = [
        word("eins", 8_000, speaker="0"),
        word("zwei", 8_500, speaker="0"),
        word("drei", 9_000, speaker="0"),
        word("vier", 17_000, speaker="0"),
    ]
    result = merge_part_transcripts([part(0, left), part(1, right)])

    assert result.words[-1].speaker == "part-1:0"


def test_speaker_identity_can_continue_across_three_parts():
    result = merge_part_transcripts(
        [
            part(0, [word("a", 9_000, speaker="0"), word("b", 9_500, speaker="0")]),
            part(
                1,
                [
                    word("a", 9_000, speaker="5"),
                    word("b", 9_500, speaker="5"),
                    word("c", 19_000, speaker="5"),
                    word("d", 19_500, speaker="5"),
                ],
            ),
            part(2, [word("c", 19_000, speaker="2"), word("d", 19_500, speaker="2"), word("e", 27_000, speaker="2")]),
        ]
    )

    assert [item.speaker for item in result.words] == ["part-0:0"] * 5


def test_empty_sequence_and_silent_part_are_valid_without_fabricated_text():
    for parts in ([], [part(0)]):
        result = merge_part_transcripts(parts)
        assert result.text == ""
        assert result.words == ()
        assert not result.boundary_warnings


@pytest.mark.parametrize(
    "parts",
    [
        [part(0), part(2)],
        [part(1), part(0)],
        [part(0), part(0)],
        [part(0), PartTranscript(1, 6_000, 24_000, 10_001, 20_000, "")],
    ],
)
def test_missing_duplicate_or_noncontiguous_parts_fail_closed(parts):
    with pytest.raises(ValueError):
        merge_part_transcripts(parts)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_ms": -1},
        {"start_ms": 3.5},
        {"end_ms": 1},
        {"end_ms": True},
        {"confidence": math.nan},
        {"confidence": math.inf},
        {"confidence": -1},
    ],
)
def test_invalid_word_evidence_is_rejected(kwargs):
    values = {"text": "hello", "start_ms": 2, "end_ms": 100}
    values.update(kwargs)
    with pytest.raises(ValueError):
        TranscriptWord(**values)


def test_word_outside_part_cannot_escape_original_timeline():
    with pytest.raises(ValueError, match="outside"):
        part(1, [word("Hallo", 5_999)])


def test_merge_is_deterministic_does_not_mutate_input_and_preserves_valid_real_intervals():
    parts = [part(0, [word("Hallo", 9_000, 9_250)]), part(1, [word("Hallo", 9_050, 9_270), word("Welt", 17_000)])]
    before = copy.deepcopy(parts)

    result = merge_part_transcripts(parts)

    assert merge_part_transcripts(parts) == result
    assert parts == before
    original_intervals = {(item.start_ms, item.end_ms) for item in before[0].words + before[1].words}
    assert all((item.start_ms, item.end_ms) in original_intervals for item in result.words)
    assert [item.start_ms for item in result.words] == sorted(item.start_ms for item in result.words)


def normalize(provider, payload, **kwargs):
    return part_transcript_from_payload(
        provider,
        index=1,
        start_ms=6_000,
        end_ms=24_000,
        core_start_ms=10_000,
        core_end_ms=20_000,
        payload=payload,
        **kwargs,
    )


def test_openrouter_seconds_are_rebased_exactly_once_and_speaker_zero_survives():
    result = normalize(
        "openrouter_stt",
        {
            "text": "Guten Tag.",
            "words": [
                {"word": "Guten", "start": 0.125, "end": 0.325, "speaker": 0},
                {"word": "Tag.", "start": 0.350, "end": 0.700, "speaker": 0},
            ],
        },
    )

    assert [(item.start_ms, item.end_ms, item.speaker) for item in result.words] == [
        (6_125, 6_325, "0"),
        (6_350, 6_700, "0"),
    ]


def test_azure_word_offsets_are_milliseconds_and_inherit_phrase_speaker():
    result = normalize(
        "azure_mai",
        {
            "combinedPhrases": [{"text": "Guten Tag."}],
            "phrases": [
                {
                    "text": "Guten Tag.",
                    "speaker": 0,
                    "offsetMilliseconds": 125,
                    "durationMilliseconds": 575,
                    "words": [
                        {"text": "Guten", "offsetMilliseconds": 125, "durationMilliseconds": 200},
                        {"text": "Tag.", "offsetMilliseconds": 350, "durationMilliseconds": 350},
                    ],
                },
            ],
        },
    )

    assert [(item.start_ms, item.end_ms, item.speaker) for item in result.words] == [
        (6_125, 6_325, "0"),
        (6_350, 6_700, "0"),
    ]


def test_azure_phrase_only_data_is_never_presented_as_word_alignment():
    result = normalize(
        "azure_mai",
        {
            "phrases": [
                {"text": "Guten Tag.", "speaker": 0, "offsetMilliseconds": 125, "durationMilliseconds": 575},
            ]
        },
    )

    assert result.text == "Guten Tag."
    assert result.words == ()
    assert not merge_part_transcripts([result]).word_timing_complete


@pytest.mark.parametrize(
    "bad_word",
    [
        {"word": "Tag", "start": math.nan, "end": 1.5},
        {"word": "Tag", "start": 1.0, "end": math.inf},
        {"word": "Tag", "start": True, "end": 1.5},
        {"word": "Tag", "start": -1, "end": 1.5},
        {"word": "Tag", "start": 18, "end": 19},
        {"word": "Tag", "start": 2, "end": 1.5},
        {"word": "Tag"},
    ],
)
def test_invalid_partial_provider_timing_keeps_complete_text(bad_word):
    result = normalize(
        "openrouter_stt",
        {
            "text": "Guten Tag",
            "words": [
                {"word": "Guten", "start": 0.5, "end": 0.8},
                bad_word,
            ],
        },
    )

    assert result.text == "Guten Tag"
    assert result.words == ()


def test_provider_words_missing_a_name_or_number_cannot_claim_complete_coverage():
    result = normalize(
        "openrouter_stt",
        {
            "text": "Anna zahlt 1500 Euro",
            "words": [
                {"word": "Anna", "start": 0.5, "end": 0.8},
                {"word": "zahlt", "start": 0.9, "end": 1.2},
                {"word": "Euro", "start": 1.8, "end": 2.0},
            ],
        },
    )

    assert result.text == "Anna zahlt 1500 Euro"
    assert not result.words


def test_compound_name_tokenization_disagreement_uses_complete_text_fallback():
    result = normalize(
        "openrouter_stt",
        {
            "text": "Anna Maria",
            "words": [
                {"word": "Annamaria", "start": 0.5, "end": 0.8},
            ],
        },
    )

    assert result.text == "Anna Maria"
    assert not result.words


def test_normalization_supports_explicit_authoritative_text_and_no_input_mutation():
    payload = {"words": [{"word": "Hallo", "start": 1, "end": 2}]}
    before = copy.deepcopy(payload)
    result = normalize("openrouter_stt", payload, text="Hallo!")

    assert result.text == "Hallo!"
    assert result.words[0].text == "Hallo"
    assert payload == before


def test_unsupported_provider_is_explicitly_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        normalize("invented", {})


def test_word_only_provider_response_retains_recognition_even_with_partial_timing():
    result = normalize(
        "openrouter_stt",
        {
            "words": [
                {"word": "Hallo", "start": 1, "end": 2},
                {"word": "Anna"},
            ]
        },
    )

    assert result.text == "Hallo Anna"
    assert not result.words


def test_azure_fallback_does_not_claim_native_speaker_evidence():
    result = normalize(
        "azure_mai",
        {
            "_scriberDiarizationFallback": "diarization_unavailable",
            "phrases": [
                {
                    "speaker": 0,
                    "text": "Hallo",
                    "words": [
                        {"text": "Hallo", "offsetMilliseconds": 10, "durationMilliseconds": 200},
                    ],
                }
            ],
        },
    )

    assert result.words[0].speaker is None


def test_many_boundaries_do_not_accumulate_duplicate_speech():
    parts = []
    for index in range(12):
        words = [word(f"wort-{tick}", tick * 1_000) for tick in range(max(0, index * 10 - 4), index * 10 + 14)]
        parts.append(part(index, words))

    result = merge_part_transcripts(parts)

    assert len(result.words) == 124
    assert len({item.text for item in result.words}) == 124
    assert not result.boundary_warnings


def test_sparse_short_words_with_disjoint_jitter_remain_visible_and_warned():
    result = merge_part_transcripts(
        [
            part(0, [word("Anna", 8_000, 8_100), word("sagt", 9_000, 9_100), word("ja", 10_000, 10_100)]),
            part(1, [word("Anna", 8_200, 8_300), word("sagt", 9_200, 9_300), word("ja", 10_200, 10_300)]),
        ]
    )

    assert result.text == "Anna Anna sagt sagt ja ja"
    assert len(result.words) == 6
    assert codes(result) == ["overlap_conflicting_words"]


@pytest.mark.parametrize(
    "text,timed_text",
    [
        ("-5", "5"),
        ("1.500", "1500"),
        ("1,5", "15"),
        (".5", "5"),
        ("Anna Maria", "Annamaria"),
        ("Maße", "Masse"),
        ("5%", "5"),
        ("5€", "5"),
    ],
)
def test_single_request_openrouter_coverage_cannot_erase_signs_numbers_or_names(text, timed_text):
    payload = {"text": text, "words": [{"word": timed_text, "start": 1, "end": 2}]}

    assert normalize_provider_words("openrouter_stt", payload) == []
    assert normalize_provider_segments("openrouter_stt", payload, "mix") == []


@pytest.mark.parametrize(
    "words",
    [
        [{"word": "one", "start": 10, "end": 11}, {"word": "two", "start": 2, "end": 3}],
        [{"word": "one", "start": -1, "end": 1}, {"word": "two", "start": 2, "end": 3}],
    ],
)
def test_single_request_openrouter_invalid_word_order_requires_full_text_fallback(words):
    payload = {"text": "one two", "words": words}

    assert normalize_provider_words("openrouter_stt", payload) == []
    assert normalize_provider_segments("openrouter_stt", payload, "mix") == []


@pytest.mark.parametrize(
    "raw_word",
    [
        {"text": "Anna", "startMs": -1, "endMs": 100},
        {"text": "Anna", "startMs": 1.1, "endMs": 100},
        {"text": "Anna", "startMs": True, "endMs": 100},
        {"text": "Anna", "startMs": 100, "endMs": 99},
        {"text": "Anna", "startMs": 1, "endMs": math.nan},
        {"text": 123, "startMs": 1, "endMs": 100},
    ],
)
def test_merged_internal_flag_does_not_override_invalid_native_word_evidence(raw_word):
    payload = {"text": "Anna", "_scriberMerged": {"wordTimingComplete": True, "words": [raw_word]}}

    assert normalize_provider_words("openrouter_stt", payload) == []
    assert normalize_provider_segments("openrouter_stt", payload, "mix") == []


def test_merged_internal_flag_cannot_erase_missing_text():
    payload = {
        "text": "Anna zahlt 5000",
        "_scriberMerged": {
            "wordTimingComplete": True,
            "words": [{"text": "Anna", "startMs": 1, "endMs": 100}],
        },
    }

    assert normalize_provider_words("openrouter_stt", payload) == []
    assert normalize_provider_segments("openrouter_stt", payload, "mix") == []


def test_merged_text_copy_must_agree_with_complete_word_evidence():
    payload = {
        "text": "Anna",
        "_scriberMerged": {
            "text": "Anna zahlt 5000",
            "wordTimingComplete": True,
            "words": [{"text": "Anna", "startMs": 1, "endMs": 100}],
        },
    }

    assert normalize_provider_words("azure_mai", payload) == []


def test_merged_word_order_must_advance_on_original_clock():
    payload = {
        "text": "one two",
        "_scriberMerged": {
            "wordTimingComplete": True,
            "words": [
                {"text": "one", "startMs": 1_000, "endMs": 1_100},
                {"text": "two", "startMs": 500, "endMs": 600},
            ],
        },
    }

    assert normalize_provider_words("azure_mai", payload) == []


def test_overlapping_alternative_cannot_shorten_canonical_interval_of_full_word():
    result = merge_part_transcripts(
        [
            part(0, [word("Gesellschaftsvertrag", 9_000, 10_000)]),
            part(1, [word("Gesellschaft", 9_100, 9_200)]),
        ]
    )

    segments = normalize_provider_segments("openrouter_stt", result.to_dict(), "mix")

    assert len(segments) == 1
    assert segments[0]["startMs"] == 9_000
    assert segments[0]["endMs"] == 10_000
    assert segments[0]["text"] == "Gesellschaftsvertrag Gesellschaft"


@pytest.mark.parametrize("field", ["text", "displayText", "display", "lexical"])
def test_azure_empty_combined_result_does_not_hide_actual_phrase_text(field):
    result = normalize(
        "azure_mai",
        {
            "text": "",
            "combinedPhrases": [{"text": ""}],
            "phrases": [{field: "Complete recognized speech"}],
        },
    )

    assert result.text == "Complete recognized speech"
    assert not result.words


@pytest.mark.parametrize("field", ["text", "displayText", "transcription", "transcript"])
def test_azure_known_top_level_text_fallback_aliases_are_preserved(field):
    result = normalize("azure_mai", {field: "Complete recognized speech"})

    assert result.text == "Complete recognized speech"
    assert not result.words

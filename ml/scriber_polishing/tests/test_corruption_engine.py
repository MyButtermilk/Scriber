import re

from scriber_polishing.corruption_engine import (
    CorruptionProfile,
    CorruptionSeverity,
    ValueMapping,
    compose_corruption_profiles,
    corrupt_text,
    corrupt_with_profile,
)


def test_identity_variant_leaves_an_already_correct_text_unchanged() -> None:
    result = corrupt_text("Die Frist endet am 12.03.2026.", severity=CorruptionSeverity.IDENTITY, seed=3)

    assert result.text == "Die Frist endet am 12.03.2026."
    assert result.operations == ()


def test_corruption_is_seeded_tracks_origins_and_preserves_numeric_protected_spans() -> None:
    source = "Die Zahlung von 1.250,00 € erfolgt am 12.03.2026 um 09:30 Uhr."
    result = corrupt_text(source, severity="heavy", seed=7)

    assert result == corrupt_text(source, severity="heavy", seed=7)
    assert result.error_origin in {"stt", "speaker", "mixed", "formatting_only"}
    assert result.operations
    assert "1.250,00 €" in result.text
    assert "12.03.2026" in result.text
    assert "09:30 Uhr" in result.text


def test_corruption_never_duplicates_a_protected_number_at_the_start_of_a_sentence() -> None:
    result = corrupt_text("1.250,00 € sind bis morgen fällig.", severity="heavy", seed=7)

    assert result.text.count("1.250,00 €") == 1


def test_heavy_and_challenge_variants_include_repetition_self_correction_and_spoken_formatting() -> None:
    source = "Betreff: Projektstatus. Bitte bestätigen Sie die Freigabe."
    heavy = corrupt_text(source, severity="heavy", seed=9)
    challenge = corrupt_text(source, severity="challenge", seed=9)

    assert any(operation.kind == "repetition" for operation in heavy.operations)
    assert any(operation.kind == "self_correction" for operation in heavy.operations)
    assert any(operation.kind == "spoken_formatting" for operation in challenge.operations)


def test_named_profiles_cover_spelling_grammar_abandoned_starts_and_formatting() -> None:
    source = "Bitte senden Sie die Unterlagen. Die Prüfung ist heute abgeschlossen."

    spelling = corrupt_with_profile(source, CorruptionProfile.SPELLING, seed=11)
    grammar = corrupt_with_profile(source, CorruptionProfile.GRAMMAR, seed=12)
    abandoned = corrupt_with_profile(source, CorruptionProfile.ABANDONED_START, seed=13)
    formatting = corrupt_with_profile(source, CorruptionProfile.SPOKEN_FORMATTING, seed=14)

    assert spelling.text != source
    assert [operation.kind for operation in spelling.operations] == ["spelling_error"]
    assert grammar.text != source
    assert [operation.kind for operation in grammar.operations] == ["grammar_error"]
    assert "also nein" in abandoned.text
    assert [operation.kind for operation in abandoned.operations] == ["abandoned_start"]
    assert "neuer Absatz" in formatting.text
    assert [operation.kind for operation in formatting.operations] == ["spoken_formatting"]


def test_layout_profiles_record_real_blank_line_and_merged_paragraph_errors() -> None:
    source = "Erster Absatz.\n\nZweiter Absatz."

    blank_lines = corrupt_with_profile(source, CorruptionProfile.BLANK_LINE_ERROR, seed=14)
    merged = corrupt_with_profile(source, CorruptionProfile.PUNCTUATION_CASE, seed=15)

    assert blank_lines.text == "Erster Absatz.\n\n\nZweiter Absatz."
    assert [operation.kind for operation in blank_lines.operations] == ["blank_line_error"]
    assert "\n" not in merged.text
    assert "merged_paragraphs" in [operation.kind for operation in merged.operations]
    assert merged.error_origin == "mixed"


def test_minimal_punctuation_profile_changes_exactly_one_unprotected_mark() -> None:
    source = "Bitte senden Sie, heute. Danke!"

    result = corrupt_with_profile(source, CorruptionProfile.MINIMAL_PUNCTUATION, seed=15)

    assert result.text == "Bitte senden Sie heute. Danke!"
    assert [operation.kind for operation in result.operations] == ["punctuation_error"]


def test_unit_and_legal_speech_profiles_record_reversible_surface_mappings() -> None:
    source = "Die Fläche beträgt 84,5 m² nach § 7 Abs. 4 Satz 2 EStG."

    units = corrupt_with_profile(source, CorruptionProfile.UNITS_SPOKEN, seed=21)
    legal = corrupt_with_profile(source, CorruptionProfile.LEGAL_SPOKEN, seed=22)

    assert "84,5 Quadratmeter" in units.text
    assert units.value_mappings[0].target == "84,5 m²"
    assert units.value_mappings[0].source == "84,5 Quadratmeter"
    assert "Paragraf 7 Absatz 4 Satz 2 Einkommensteuergesetz" in legal.text
    assert legal.value_mappings[0].target == "§ 7 Abs. 4 Satz 2 EStG"


def test_legal_speech_profile_supports_long_hierarchy_multiple_sections_and_articles() -> None:
    source = (
        "Es gelten § 7 Absatz 4 Satz 2 EStG, §§ 8c und 8d KStG "
        "und Artikel 3 Absatz 1 GG."
    )

    result = corrupt_with_profile(source, CorruptionProfile.LEGAL_SPOKEN, seed=22)

    assert "Paragraf 7 Absatz 4 Satz 2 Einkommensteuergesetz" in result.text
    assert "Paragrafen 8c und 8d Körperschaftsteuergesetz" in result.text
    assert "Artikel 3 Absatz 1 Grundgesetz" in result.text
    assert len(result.value_mappings) == 3


def test_unit_surface_mapping_matches_a_decimal_compound_unit_as_one_value() -> None:
    source = "Der Volumenstrom beträgt 0,28 m³/h."

    result = corrupt_with_profile(source, CorruptionProfile.UNITS_SPOKEN, seed=23)

    assert "0,28 Kubikmeter pro Stunde" in result.text
    assert result.value_mappings == (
        ValueMapping("unit_surface", "0,28 Kubikmeter pro Stunde", "0,28 m³/h"),
    )


def test_orthography_house_style_profile_records_a_reversible_variant() -> None:
    result = corrupt_with_profile(
        "Die Rückmeldung liegt zurzeit vor.",
        CorruptionProfile.ORTHOGRAPHY_HOUSE_STYLE,
        seed=24,
    )

    assert result.text == "Die Rückmeldung liegt zur Zeit vor."
    assert result.value_mappings == (
        ValueMapping("orthography_surface", "zur Zeit", "zurzeit"),
    )


def test_orthography_house_style_mappings_do_not_chain_through_another_target_surface() -> None:
    result = corrupt_with_profile(
        "Wir formulieren es so, dass alles klar bleibt.",
        CorruptionProfile.ORTHOGRAPHY_HOUSE_STYLE,
        seed=24,
    )

    assert "sodass" in result.text
    assert "so dass" not in result.text
    assert result.value_mappings == (
        ValueMapping("orthography_surface", "sodass", "so, dass"),
    )


def test_context_spelling_profile_creates_an_explicit_das_dass_error() -> None:
    result = corrupt_with_profile(
        "Wir bestätigen, dass die Freigabe vorliegt.",
        CorruptionProfile.CONTEXT_SPELLING,
        seed=25,
    )

    assert result.text == "Wir bestätigen, das die Freigabe vorliegt."
    assert [operation.kind for operation in result.operations] == ["context_spelling_error"]


def test_inapplicable_domain_surface_profiles_never_invent_content() -> None:
    source = "Bitte senden Sie die Unterlagen unverändert."

    units = corrupt_with_profile(source, CorruptionProfile.UNITS_SPOKEN, seed=21)
    legal = corrupt_with_profile(source, CorruptionProfile.LEGAL_SPOKEN, seed=22)

    assert units.text == source
    assert units.operations == ()
    assert units.value_mappings == ()
    assert legal.text == source
    assert legal.operations == ()
    assert legal.value_mappings == ()


def test_composes_reversible_domain_surfaces_with_seeded_noise() -> None:
    source = "Die Fläche beträgt 84,5 m² und die Prüfung ist abgeschlossen."

    result = compose_corruption_profiles(
        source,
        (CorruptionProfile.UNITS_SPOKEN, CorruptionProfile.HEAVY_COMBINED),
        seed=31,
    )

    assert result == compose_corruption_profiles(
        source,
        (CorruptionProfile.UNITS_SPOKEN, CorruptionProfile.HEAVY_COMBINED),
        seed=31,
    )
    assert "84,5 Quadratmeter" in result.text
    assert result.value_mappings[0].target == "84,5 m²"
    assert [operation.kind for operation in result.operations][:2] == [
        "unit_spoken",
        "punctuation_error",
    ]
    assert result.severity is CorruptionSeverity.CHALLENGE


def test_explicit_protection_preserves_every_occurrence_of_a_repeated_value() -> None:
    source = "Mara Feld prüft den Vorgang. Mara Feld gibt ihn frei."

    result = compose_corruption_profiles(
        source,
        (CorruptionProfile.HEAVY_COMBINED,),
        seed=32,
        protected_values=("Mara Feld",),
    )

    assert result.text.count("Mara Feld") == 2


def test_explicit_protection_preserves_overlapping_full_sentence_and_inner_values() -> None:
    sentence = "Die Rückmeldung ist bis 01.09.2026 erforderlich."

    result = compose_corruption_profiles(
        sentence,
        (CorruptionProfile.HEAVY_COMBINED,),
        seed=33,
        protected_values=("01.09.2026", sentence),
    )

    assert result.text.count(sentence) == 1


def test_short_protected_word_is_not_treated_as_a_substring_inside_other_words() -> None:
    result = compose_corruption_profiles(
        "Mara bestätigt, er prüft den Termin.",
        (CorruptionProfile.HEAVY_COMBINED,),
        seed=34,
        protected_values=("er",),
    )

    assert len(re.findall(r"(?<!\w)er(?!\w)", result.text)) == 1


def test_profile_seed_selects_reproducible_nonprotected_positions() -> None:
    source = "Alpha bleibt erhalten. Beta bleibt ebenfalls erhalten. Frist 12.03.2026."

    first = corrupt_with_profile(source, CorruptionProfile.FILLER_STUTTER, seed=100)
    repeated = corrupt_with_profile(source, CorruptionProfile.FILLER_STUTTER, seed=100)
    other = corrupt_with_profile(source, CorruptionProfile.FILLER_STUTTER, seed=101)

    assert first == repeated
    assert first.text != other.text
    assert first.text.count("12.03.2026") == 1
    assert other.text.count("12.03.2026") == 1

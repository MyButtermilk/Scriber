from __future__ import annotations

import pytest

from src.local_polishing.safety import SafetyError, validate_plain_text_content


def test_plain_text_safety_accepts_removal_of_an_exact_semantic_word_repetition() -> None:
    validate_plain_text_content(
        "Kabelwege dürfen den barrierefreien Zugang nicht nicht beeinträchtigen.",
        "Kabelwege dürfen den barrierefreien Zugang nicht beeinträchtigen.",
    )


def test_plain_text_safety_accepts_removal_of_an_exact_repeated_phrase() -> None:
    validate_plain_text_content(
        "Für die Freigabe ist ohne weitere, ohne weitere Verzögerung erforderlich.",
        "Für die Freigabe ist ohne weitere Verzögerung erforderlich.",
    )

    validate_plain_text_content(
        "Für die Freigabe ist eine klare, eine klare Zuordnung erforderlich.",
        "Für die Freigabe ist eine klare Zuordnung erforderlich.",
    )


def test_plain_text_safety_accepts_pause_delimited_na_ja_as_a_filler() -> None:
    validate_plain_text_content(
        "Für die Bearbeitung werden, na ja, folgende Angaben benötigt.",
        "Für die Bearbeitung werden folgende Angaben benötigt.",
    )


def test_plain_text_safety_ignores_number_rendering_in_operator_context() -> None:
    validate_plain_text_content(
        "Im Regelbetrieb werden einhundertzwei Anfragen erwartet.",
        "Im Regelbetrieb werden 102 Anfragen erwartet.",
    )


def test_plain_text_safety_accepts_removal_of_an_acoustic_filler_near_an_operator() -> None:
    validate_plain_text_content(
        "Ohne eine klare Abgrenzung der Leistungen ähm, können wir keine Zustimmung erteilen.",
        "Ohne eine klare Abgrenzung der Leistungen können wir keine Zustimmung erteilen.",
    )


def test_plain_text_safety_accepts_a_spoken_date_after_the_twentieth() -> None:
    validate_plain_text_content(
        "Bitte senden Sie die Unterlagen am dreiundzwanzigsten September zweitausendsechsundzwanzig um dreizehn Uhr fünfzehn.",
        "Bitte senden Sie die Unterlagen am 23.09.2026 um 13:15 Uhr.",
    )


def test_plain_text_safety_accepts_spoken_ordinals_as_a_numbered_list() -> None:
    validate_plain_text_content(
        "Bitte erledigen: Erstens Unterlagen prüfen. Zweitens Termin bestätigen. Drittens Rückmeldung senden.",
        "Bitte erledigen:\n\n1. Unterlagen prüfen\n2. Termin bestätigen\n3. Rückmeldung senden",
    )


def test_plain_text_safety_rejects_changed_numbered_list_order() -> None:
    with pytest.raises(SafetyError, match="changed_list_structure"):
        validate_plain_text_content(
            "Bitte erledigen: Erstens Unterlagen prüfen. Zweitens Termin bestätigen.",
            "Bitte erledigen:\n\n1. Unterlagen prüfen\n3. Termin bestätigen",
        )


def test_plain_text_safety_accepts_spoken_legal_reference_formatting() -> None:
    validate_plain_text_content(
        "Für die Einordnung gilt Paragraf vierunddreißig Absatz eins Satz eins BauGB.",
        "Für die Einordnung gilt § 34 Abs. 1 S. 1 BauGB.",
    )


def test_plain_text_safety_rejects_changed_spoken_legal_reference_value() -> None:
    with pytest.raises(SafetyError, match="changed_legal_reference"):
        validate_plain_text_content(
            "Für die Einordnung gilt Paragraf vierunddreißig Absatz eins Satz eins BauGB.",
            "Für die Einordnung gilt § 35 Abs. 1 S. 1 BauGB.",
        )


def test_plain_text_safety_accepts_a_spoken_legal_letter_reference() -> None:
    validate_plain_text_content(
        "Für die Einordnung gilt Artikel fünf Absatz eins Buchstabe c DSGVO.",
        "Für die Einordnung gilt Art. 5 Abs. 1 lit. c DSGVO.",
    )


def test_plain_text_safety_rejects_a_changed_legal_letter_reference() -> None:
    with pytest.raises(SafetyError, match="changed_legal_reference"):
        validate_plain_text_content(
            "Für die Einordnung gilt Artikel fünf Absatz eins Buchstabe c DSGVO.",
            "Für die Einordnung gilt Art. 5 Abs. 1 lit. d DSGVO.",
        )


def test_plain_text_safety_does_not_merge_spoken_numbers_across_a_sentence_boundary() -> None:
    validate_plain_text_content(
        "Das Aktenzeichen endet mit vierzehn. Ein Termin folgt.",
        "Das Aktenzeichen endet mit 14. Ein Termin folgt.",
    )


def test_plain_text_safety_accepts_a_spoken_slash_in_a_case_number() -> None:
    validate_plain_text_content(
        "Das Urteil trägt das Aktenzeichen VIII ZR einhundertfünfundachtzig Schrägstrich vierzehn.",
        "Das Urteil trägt das Aktenzeichen VIII ZR 185/14.",
    )


def test_plain_text_safety_does_not_fold_a_trailing_verb_into_a_case_number() -> None:
    validate_plain_text_content(
        "Bitte beziehen Sie das Urteil VIII ZR einhundertfünfundachtzig Schrägstrich vierzehn ein.",
        "Bitte beziehen Sie das Urteil VIII ZR 185/14 ein.",
    )


def test_plain_text_safety_accepts_multiple_spoken_fraction_digits() -> None:
    validate_plain_text_content(
        "Als Vergleichswert sind einundzwanzig Komma acht null Euro pro Quadratmeter anzusetzen.",
        "Als Vergleichswert sind 21,80 €/m² anzusetzen.",
    )


def test_plain_text_safety_accepts_an_implicit_zero_minute() -> None:
    validate_plain_text_content(
        "Der Termin beginnt um sechzehn Uhr.",
        "Der Termin beginnt um 16:00 Uhr.",
    )


def test_plain_text_safety_accepts_spelled_initialisms_and_a_spoken_court_roman_numeral() -> None:
    validate_plain_text_content(
        "Das Konzept muss die D S G V O und die T O M berücksichtigen. Das Urteil lautet BGH – acht Z R einhundertfünfundachtzig Schrägstrich vierzehn.",
        "Das Konzept muss die DSGVO und die TOM berücksichtigen. Das Urteil lautet BGH – VIII ZR 185/14.",
    )


def test_plain_text_safety_accepts_the_standard_ust_id_abbreviation() -> None:
    validate_plain_text_content(
        "Auf der Rechnung sind U S T I D und I B A N vollständig anzugeben.",
        "Auf der Rechnung sind USt-IdNr. und IBAN vollständig anzugeben.",
    )


def test_plain_text_safety_accepts_a_contextual_list_separator_as_a_bullet_boundary() -> None:
    validate_plain_text_content(
        "Benötigt werden folgende Angaben: Rechnung prüfen; Leistungsdatum prüfen sowie Steuersatz erläutern.",
        "Benötigt werden folgende Angaben:\n\n- Rechnung prüfen\n- Leistungsdatum prüfen\n- Steuersatz erläutern",
    )


def test_plain_text_safety_keeps_an_internal_sowie_while_binding_a_later_bullet_boundary() -> None:
    validate_plain_text_content(
        "Punkte: Median sowie neunzig. und neunundneunzig. Perzentil ausweisen; Datenschutz dokumentieren sowie Zuständigkeiten benennen.",
        "Punkte:\n\n- Median sowie 90. und 99. Perzentil ausweisen\n- Datenschutz dokumentieren\n- Zuständigkeiten benennen",
    )


def test_plain_text_safety_does_not_drop_sowie_outside_an_enumerated_bullet_boundary() -> None:
    with pytest.raises(SafetyError, match="content_loss"):
        validate_plain_text_content(
            "Bitte reichen Sie die Rechnung sowie die Belegliste ein.",
            "Bitte reichen Sie die Rechnung die Belegliste ein.",
        )


def test_plain_text_safety_accepts_parenthesized_energy_intensity_unit() -> None:
    validate_plain_text_content(
        "Der Kennwert beträgt siebenundvierzig Kilowattstunden pro Quadratmeter und Jahr.",
        "Der Kennwert beträgt 47 kWh/(m²·a).",
    )


@pytest.mark.parametrize(
    ("spoken", "compact"),
    (
        ("217 Megabit pro Sekunde", "217 Mbit/s"),
        ("113 Newtonmeter", "113 Nm"),
        ("89 Megawattstunden", "89 MWh"),
        ("424 Megabyte", "424 MB"),
        ("8 Gigabyte", "8 GB"),
        ("23 Liter pro Minute", "23 l/min"),
        ("34 Liter", "34 l"),
        ("5,7 Liter pro Quadratmeter", "5,7 l/m²"),
        ("15 Kilowatt", "15 kW"),
        ("12 Kilowatt Peak", "12 kWp"),
        ("243 Kilometer", "243 km"),
        ("18 Millimeter", "18 mm"),
        ("176 Tonnen", "176 t"),
    ),
)
def test_plain_text_safety_accepts_bound_compact_units(spoken: str, compact: str) -> None:
    validate_plain_text_content(
        f"Der dokumentierte Wert beträgt {spoken}.",
        f"Der dokumentierte Wert beträgt {compact}.",
    )


def test_plain_text_safety_rejects_a_standalone_month_mutation() -> None:
    with pytest.raises(SafetyError, match="content_"):
        validate_plain_text_content(
            "Der Termin findet im Mai statt.",
            "Der Termin findet im Juni statt.",
        )


def test_plain_text_safety_rejects_a_standalone_month_addition() -> None:
    with pytest.raises(SafetyError, match="content_"):
        validate_plain_text_content(
            "Wir treffen uns.",
            "Wir treffen uns Mai.",
        )


def test_plain_text_safety_rejects_an_unbound_duration_unit_mutation() -> None:
    with pytest.raises(SafetyError, match="content_"):
        validate_plain_text_content(
            "Die Frist beträgt eine Stunde.",
            "Die Frist beträgt eine Minute.",
        )


def test_plain_text_safety_does_not_treat_a_variable_as_a_unit_surface() -> None:
    with pytest.raises(SafetyError, match="content_"):
        validate_plain_text_content(
            "Die Variable M bleibt unverändert.",
            "Die Variable Meter bleibt unverändert.",
        )


def test_plain_text_safety_does_not_render_acht_in_the_acht_geben_idiom() -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(
            "Bitte geben Sie acht.",
            "Bitte geben Sie 8.",
        )


def test_plain_text_safety_preserves_significant_leading_zeroes() -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(
            "Das Aktenzeichen lautet 089/123456.",
            "Das Aktenzeichen lautet 89/123456.",
        )


def test_plain_text_safety_does_not_rebind_a_spoken_format_command_to_a_later_repetition() -> None:
    with pytest.raises(SafetyError, match="changed_format_command"):
        validate_plain_text_content(
            "Wir essen komma Kinder und wir essen Kinder.",
            "Wir essen Kinder und wir essen, Kinder.",
        )


def test_plain_text_safety_rejects_a_polarity_scope_shift_across_a_sentence_boundary() -> None:
    with pytest.raises(SafetyError, match="changed_polarity_position"):
        validate_plain_text_content(
            "Wir liefern ohne Garantie. Die Prüfung erfolgt.",
            "Wir liefern. Ohne Garantie die Prüfung erfolgt.",
        )


def test_plain_text_safety_rejects_changing_an_explicit_statement_into_a_question() -> None:
    with pytest.raises(SafetyError, match="changed_sentence_type"):
        validate_plain_text_content(
            "Sie zahlen.",
            "Sie zahlen?",
        )


def test_plain_text_safety_does_not_invent_a_significant_leading_zero() -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(
            "Das Aktenzeichen lautet 89/123456.",
            "Das Aktenzeichen lautet 089/123456.",
        )


def test_plain_text_safety_binds_significant_leading_zeroes_by_position() -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(
            "Die Codes lauten 089 und 89.",
            "Die Codes lauten 89 und 089.",
        )


def test_plain_text_safety_does_not_bind_a_unit_to_a_distant_unrelated_number() -> None:
    with pytest.raises(SafetyError, match="content_"):
        validate_plain_text_content(
            "Version 2 verwendet die Variable M.",
            "Version 2 verwendet die Variable Meter.",
        )


def test_plain_text_safety_does_not_bind_an_uppercase_variable_to_an_adjacent_number() -> None:
    with pytest.raises(SafetyError, match="changed_unit"):
        validate_plain_text_content(
            "Die Variable M (2) bleibt.",
            "Die Variable Meter (2) bleibt.",
        )


def test_plain_text_safety_still_renders_a_numeric_acht_after_a_distinct_idiom() -> None:
    validate_plain_text_content(
        "Geben Sie acht und laden Sie danach acht Pakete.",
        "Geben Sie acht und laden Sie danach 8 Pakete.",
    )


def test_plain_text_safety_still_renders_acht_before_a_counted_noun() -> None:
    validate_plain_text_content(
        "Wir geben acht Pakete ab.",
        "Wir geben 8 Pakete ab.",
    )


def test_plain_text_safety_rejects_an_interior_statement_to_question_change() -> None:
    with pytest.raises(SafetyError, match="changed_sentence_type"):
        validate_plain_text_content(
            "Sie zahlen. Wir kommen.",
            "Sie zahlen? Wir kommen.",
        )


def test_plain_text_safety_preserves_an_explicit_question_mark() -> None:
    with pytest.raises(SafetyError, match="changed_sentence_type"):
        validate_plain_text_content(
            "Sie kommen?",
            "Sie kommen",
        )


def test_plain_text_safety_rejects_an_added_question_mark_before_a_preserved_period() -> None:
    with pytest.raises(SafetyError, match="changed_sentence_type"):
        validate_plain_text_content(
            "Sie zahlen. Wir kommen.",
            "Sie zahlen?. Wir kommen.",
        )


@pytest.mark.parametrize(
    ("source", "candidate"),
    (
        ("Die Freigabe erfolgt.", "Die Freigabe erfolgt ✅."),
        ("Die Summe steht fest.", "Die Summe € steht fest."),
        ("Die Regel gilt.", "Die Regel § gilt."),
        ("Die Freigabe erfolgt.", "Die Freigabe # erfolgt."),
        ("Die Freigabe erfolgt.", "Die Freigabe @ erfolgt."),
        ("Die Freigabe erfolgt.", "Die Freigabe & erfolgt."),
        ("Die Freigabe erfolgt.", "Die Freigabe * erfolgt."),
        ("Die Freigabe erfolgt.", "Die Freigabe / erfolgt."),
        ("Die Freigabe erfolgt.", "Die Freigabe \\ erfolgt."),
    ),
)
def test_plain_text_safety_rejects_unbound_semantic_symbol_additions(source: str, candidate: str) -> None:
    with pytest.raises(SafetyError, match="content_addition"):
        validate_plain_text_content(source, candidate)


@pytest.mark.parametrize(
    ("source", "candidate"),
    (
        ("Der Wert beträgt +5 Grad Celsius.", "Der Wert beträgt 5 °C."),
        ("Der Wert beträgt 5 Grad Celsius.", "Der Wert beträgt +5 °C."),
    ),
)
def test_plain_text_safety_preserves_an_explicit_plus_on_its_numeric_anchor(source: str, candidate: str) -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(source, candidate)

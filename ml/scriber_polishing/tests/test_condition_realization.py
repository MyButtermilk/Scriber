from scriber_polishing.condition_realization import (
    condition_is_realized,
    integrate_appended_condition_commentary,
    remove_redundant_condition_commentary,
    remove_redundant_condition_restatement,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def test_recognizes_the_closed_german_condition_equivalence_families() -> None:
    examples = (
        (
            "Die Prüfung schlägt fehl.",
            "Falls die Prüfung fehlschlägt, darf Auenplan KG die Bestellung nicht auslösen.",
        ),
        (
            "Es liegt keine schriftliche Zustimmung vor.",
            "Ohne schriftliche Zustimmung bleibt System Lumen gesperrt.",
        ),
        (
            "Die Lieferung ist bestätigt.",
            "Auenplan KG soll den Betrag nur bei bestätigter Lieferung zahlen.",
        ),
        (
            "Auenplan KG stimmt zu.",
            "Wenn Auenplan KG zustimmt, könnte System Lumen am 16.09.2031 starten.",
        ),
        (
            "Es liegt keine Zustimmung vor.",
            "Ohne Zustimmung darf System Lumen nicht starten.",
        ),
    )

    assert all(condition_is_realized(condition, text) for condition, text in examples)


def test_recognizes_the_closed_mixed_language_condition_equivalence_families() -> None:
    assert condition_is_realized(
        "BIL-286 is not available by 2026-08-11",
        "Die Entscheidung bleibt offen, falls BIL-286 bis 2026-08-11 nicht vorliegt.",
    )
    assert condition_is_realized(
        "invoice reconciliation for $ 2,975.00 is complete",
        "Eine Freigabe ist nur möglich, wenn die „invoice reconciliation“ "
        "für $ 2,975.00 abgeschlossen ist.",
    )


def test_removes_only_reviewed_commentary_whose_condition_is_already_realized() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Falls die Prüfung fehlschlägt, darf Auenplan KG die Bestellung nicht auslösen. "
        "Die Regelung gilt unter folgender Bedingung: Die Prüfung schlägt fehl.[/P]\n"
        "[P]Ein unabhängiger Satz bleibt unverändert.[/P]\n"
        "[/DOC]"
    )

    repaired = remove_redundant_condition_commentary(
        document,
        ("Die Prüfung schlägt fehl.",),
    )

    assert render_plain_text(repaired) == (
        "Falls die Prüfung fehlschlägt, darf Auenplan KG die Bestellung nicht auslösen.\n\n"
        "Ein unabhängiger Satz bleibt unverändert."
    )


def test_condition_realization_fails_closed_for_an_unknown_paraphrase() -> None:
    assert not condition_is_realized(
        "Ein unbekanntes Ereignis tritt ein.",
        "Die Unterlagen bleiben offen.",
    )
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Unterlagen bleiben offen. "
        "Die Regelung gilt unter folgender Bedingung: Ein unbekanntes Ereignis tritt ein.[/P]\n"
        "[/DOC]"
    )

    try:
        remove_redundant_condition_commentary(
            document,
            ("Ein unbekanntes Ereignis tritt ein.",),
        )
    except ValueError as error:
        assert "not already realized" in str(error)
    else:
        raise AssertionError("an unrecognized condition equivalence must fail closed")


def test_integrates_declared_conditional_and_prepositional_phrases_into_fact_sentence() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Betrag wird abgegrenzt. "
        "Die Regelung gilt, sofern die Zuordnung unverändert bleibt.[/P]\n"
        "[P]Die Freigabe kann erfolgen. Die Regelung gilt nach vollständiger Prüfung.[/P]\n"
        "[/DOC]"
    )

    repaired = integrate_appended_condition_commentary(
        document,
        (
            "sofern die Zuordnung unverändert bleibt",
            "nach vollständiger Prüfung",
        ),
    )

    assert render_plain_text(repaired) == (
        "Der Betrag wird abgegrenzt, sofern die Zuordnung unverändert bleibt.\n\n"
        "Nach vollständiger Prüfung kann die Freigabe erfolgen."
    )


def test_condition_integration_consumes_repeated_declared_conditions_in_order() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Betrag bleibt gesperrt. Die Regelung gilt, wenn die Freigabe vorliegt.[/P]\n"
        "[P]Die Zahlung bleibt offen. Die Regelung gilt, wenn die Freigabe vorliegt.[/P]\n"
        "[/DOC]"
    )

    repaired = integrate_appended_condition_commentary(
        document,
        ("wenn die Freigabe vorliegt", "wenn die Freigabe vorliegt"),
    )

    assert "Die Regelung gilt" not in render_plain_text(repaired)


def test_condition_integration_ignores_fictionality_provenance_adjective() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Wert bleibt offen. Die Regelung gilt, solange der Prüfvermerk nicht vorliegt.[/P]\n"
        "[/DOC]"
    )

    repaired = integrate_appended_condition_commentary(
        document,
        ("solange der fiktive Prüfvermerk nicht vorliegt",),
    )

    assert render_plain_text(repaired) == (
        "Der Wert bleibt offen, solange der Prüfvermerk nicht vorliegt."
    )


def test_removes_only_the_closed_completed_check_restatement() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann. "
        "Dies gilt, wenn die Prüfung abgeschlossen ist.[/P]\n"
        "[/DOC]"
    )

    repaired = remove_redundant_condition_restatement(
        document,
        ("wenn die Prüfung abgeschlossen ist",),
    )

    assert render_plain_text(repaired) == (
        "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann."
    )

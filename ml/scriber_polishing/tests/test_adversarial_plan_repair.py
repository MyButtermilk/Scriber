from scriber_polishing.adversarial_plan_repair import (
    bind_feedback_list_to_obligation_due_date,
    localize_german_structure,
    mark_uncertain_list_date,
    naturalize_technical_compound,
    paragraphize_single_unchanged_item,
    preserve_feedback_position_role,
    promote_explicit_named_actors,
    restore_lexical_fictionality,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def test_preserves_declared_feedback_position_instead_of_weakening_it_to_a_topic() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": (
                        "Die fiktive Ansprechperson Levin Reuter empfiehlt eine "
                        "Rückmeldung zur Position Sollzinssatz bis zum 18.09.2031."
                    ),
                    "modality": "fact",
                }
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Ansprechperson Levin Reuter empfiehlt eine Rückmeldung zum Thema "
        "„Sollzinssatz“ bis zum 18.09.2031.[/P]\n"
        "[/DOC]"
    )

    repaired = preserve_feedback_position_role(plan, document)

    assert render_plain_text(repaired) == (
        "Die Ansprechperson Levin Reuter empfiehlt eine Rückmeldung zur Position "
        "„Sollzinssatz“ bis zum 18.09.2031."
    )


def test_binds_feedback_list_to_the_obligation_due_date_without_changing_other_dates() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": "Die Mitteilung des Finanzamts datiert vom 15.07.2026.",
                    "modality": "fact",
                },
                {
                    "text": (
                        "Milan Aue soll die Erläuterung zu Verlustvortrag bis zum "
                        "15.04.2026 übermitteln."
                    ),
                    "modality": "obligation",
                },
            ],
            "structure": {
                "list_kind": "ordered",
                "list_items": [
                    "Rechnungskopie",
                    "Rückmeldung bis 15.07.2026",
                    "Prüfpunkt Verlustvortrag",
                ],
            },
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Mitteilung des Finanzamts datiert vom 15.07.2026.[/P]\n"
        "[P]Milan Aue soll die Erläuterung zu Verlustvortrag bis zum "
        "15.04.2026 übermitteln.[/P]\n"
        "[OL]\n"
        "[LI1]Rechnungskopie[/LI1]\n"
        "[LI1]Rückmeldung bis 15.07.2026[/LI1]\n"
        "[LI1]Prüfpunkt Verlustvortrag[/LI1]\n"
        "[/OL]\n"
        "[/DOC]"
    )

    repaired_plan, repaired_document = bind_feedback_list_to_obligation_due_date(
        plan,
        document,
    )

    assert repaired_plan["semantic_plan"]["structure"]["list_items"] == [
        "Rechnungskopie",
        "Rückmeldung bis 15.04.2026",
        "Prüfpunkt Verlustvortrag",
    ]
    rendered = render_plain_text(repaired_document)
    assert "Die Mitteilung des Finanzamts datiert vom 15.07.2026." in rendered
    assert "bis zum 15.04.2026 übermitteln." in rendered
    assert "Rückmeldung bis 15.04.2026" in rendered
    assert "Rückmeldung bis 15.07.2026" not in rendered


def test_marks_an_uncertain_list_date_without_turning_it_into_a_deadline() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": "Kira Dalen kann den Termin 2026-08-11 noch nicht bestätigen.",
                    "modality": "uncertainty",
                }
            ],
            "structure": {
                "list_kind": "unordered",
                "list_items": [
                    "Reference FIN-731",
                    "Owner Kira Dalen",
                    "Deadline 2026-08-11",
                ],
            },
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Kira Dalen kann den Termin 2026-08-11 noch nicht bestätigen.[/P]\n"
        "[UL]\n"
        "[LI1]Reference FIN-731[/LI1]\n"
        "[LI1]Owner Kira Dalen[/LI1]\n"
        "[LI1]Deadline 2026-08-11[/LI1]\n"
        "[/UL]\n"
        "[/DOC]"
    )

    repaired_plan, repaired_document = mark_uncertain_list_date(plan, document)

    assert repaired_plan["semantic_plan"]["structure"]["list_items"][-1] == (
        "Termin 2026-08-11 noch nicht bestätigt"
    )
    rendered = render_plain_text(repaired_document)
    assert "Termin 2026-08-11 noch nicht bestätigt" in rendered
    assert "Deadline 2026-08-11" not in rendered


def test_naturalizes_technical_compound_while_preserving_the_exact_term() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Anforderung verlangt eine observability dashboard-Prüfung im Pfad "
        "workers/signal/src/consumer.rs.[/P]\n"
        "[/DOC]"
    )

    repaired = naturalize_technical_compound(document)

    assert render_plain_text(repaired) == (
        "Die Anforderung verlangt eine Prüfung des „observability dashboard“ im Pfad "
        "workers/signal/src/consumer.rs."
    )


def test_turns_the_single_unchanged_item_into_a_paragraph_and_updates_the_plan() -> None:
    plan = {
        "semantic_plan": {
            "facts": [],
            "structure": {
                "list_kind": "ordered",
                "list_items": ["Inhalt bleibt unverändert."],
            },
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Jonas Klee bestätigt keinen früheren Beginn.[/P]\n"
        "[OL]\n"
        "[LI1]Inhalt bleibt unverändert.[/LI1]\n"
        "[/OL]\n"
        "[/DOC]"
    )

    repaired_plan, repaired_document = paragraphize_single_unchanged_item(
        plan,
        document,
    )

    assert repaired_plan["semantic_plan"]["structure"]["list_kind"] == "none"
    assert repaired_plan["semantic_plan"]["structure"]["list_items"] == []
    assert render_plain_text(repaired_document) == (
        "Jonas Klee bestätigt keinen früheren Beginn.\n\n"
        "Inhalt bleibt unverändert."
    )


def test_can_promote_explicit_named_actors_without_gender_inference() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": (
                        "Bitte gebt Ihr Euer Prüfergebnis an Mara Linden, damit er Euch "
                        "den abgestimmten Stand zurücksendet."
                    ),
                    "protected_values": ["Mara Linden", "er", "Euch"],
                },
                {
                    "text": (
                        "Jonas Falk hält fest, dass sie den Wortlaut nicht inhaltlich "
                        "erweitert."
                    ),
                    "protected_values": ["Jonas Falk", "sie"],
                },
            ],
            "structure": {"list_kind": "none", "list_items": []},
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Bitte gebt Ihr Euer Prüfergebnis an Mara Linden, damit er Euch den "
        "abgestimmten Stand zurücksendet.[/P]\n"
        "[P]Jonas Falk hält fest, dass sie den Wortlaut nicht inhaltlich erweitert.[/P]\n"
        "[/DOC]"
    )

    repaired_plan, repaired_document = promote_explicit_named_actors(plan, document)

    rendered = render_plain_text(repaired_document)
    assert "Mara Linden, damit Mara Linden Euch" in rendered
    assert "Jonas Falk hält fest, dass Jonas Falk den Wortlaut" in rendered
    assert repaired_plan["semantic_plan"]["facts"][0]["protected_values"] == [
        "Mara Linden",
        "Euch",
    ]
    assert repaired_plan["semantic_plan"]["facts"][1]["protected_values"] == [
        "Jonas Falk"
    ]


def test_restores_only_lexical_fictionality_from_fact_text_in_natural_case() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": (
                        "Die fiktive Organisation Asterhaus Verwaltung GmbH bearbeitet "
                        "für die fiktive Liegenschaft Quartier Auenkai 7 in "
                        "Fiktivstadt-West den fiktiven Vorgang REE-71."
                    )
                },
                {
                    "text": (
                        "Die fiktive Ansprechperson Mara Voss muss die Maßnahme nur "
                        "umsetzen, wenn die fiktive Freigabe in der Projektakte "
                        "dokumentiert ist."
                    )
                },
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Asterhaus Verwaltung GmbH bearbeitet für die Liegenschaft Quartier "
        "Auenkai 7 in Fiktivstadt-West den Vorgang REE-71.[/P]\n"
        "[P]Die Ansprechperson Mara Voss muss die Maßnahme nur umsetzen, wenn die "
        "Freigabe in der Projektakte dokumentiert ist.[/P]\n"
        "[/DOC]"
    )

    repaired = restore_lexical_fictionality(plan, document)

    assert render_plain_text(repaired) == (
        "Die fiktive Organisation Asterhaus Verwaltung GmbH bearbeitet für die "
        "fiktive Liegenschaft Quartier Auenkai 7 in Fiktivstadt-West den fiktiven "
        "Vorgang REE-71.\n\n"
        "Die fiktive Ansprechperson Mara Voss muss die Maßnahme nur umsetzen, wenn "
        "die fiktive Freigabe in der Projektakte dokumentiert ist."
    )


def test_restores_fictionality_with_the_natural_target_noun_gender() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "text": (
                        "Die fiktive Organisation Lindenbogen Immobilien GmbH "
                        "bearbeitet für die fiktive Liegenschaft Gartenhof Mistral 8 "
                        "in Fiktivstadt-West den fiktiven Los BAU-44."
                    )
                }
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Lindenbogen Immobilien GmbH bearbeitet für die Liegenschaft Gartenhof "
        "Mistral 8 in Fiktivstadt-West das Los BAU-44.[/P]\n"
        "[/DOC]"
    )

    repaired = restore_lexical_fictionality(plan, document)

    assert "das fiktive Los BAU-44" in render_plain_text(repaired)
    assert "den fiktiven Los" not in render_plain_text(repaired)


def test_localizes_the_complete_declared_german_structure_and_keeps_values() -> None:
    plan = {
        "language": "de-DE",
        "semantic_plan": {
            "facts": [],
            "structure": {
                "paragraph_topics": [
                    "Delivery status and dependencies",
                    "Protected references",
                    "Next step",
                ],
                "list_kind": "nested",
                "list_items": [
                    "Reference FIN-731",
                    "Owner Kira Dalen",
                    "Deadline 2026-08-11",
                    "Sub-item: https://portal.example.invalid/release-notes",
                    "Sub-item: Tidal Gateway",
                ],
                "signature_lines": [
                    "Kira Dalen",
                    "Fictional business operations",
                ],
            },
        },
    }
    document = parse_sst(
        "[DOC]\n"
        "[SUBJECT]Delivery status and dependencies – Cobalt Sync[/SUBJECT]\n"
        "[H1]Protected references[/H1]\n"
        "[OL]\n"
        "[LI1]Reference FIN-731[/LI1]\n"
        "[LI1]Owner Kira Dalen[/LI1]\n"
        "[LI1]Deadline 2026-08-11[/LI1]\n"
        "[LI2]Sub-item: https://portal.example.invalid/release-notes[/LI2]\n"
        "[LI2]Sub-item: Tidal Gateway[/LI2]\n"
        "[/OL]\n"
        "[SIGNATURE]\n"
        "[LINE]Kira Dalen[/LINE]\n"
        "[LINE]Fictional business operations[/LINE]\n"
        "[/SIGNATURE]\n"
        "[/DOC]"
    )

    repaired_plan, repaired_document = localize_german_structure(plan, document)

    structure = repaired_plan["semantic_plan"]["structure"]
    assert structure["paragraph_topics"] == [
        "Lieferstatus und Abhängigkeiten",
        "Geschützte Referenzen",
        "Nächster Schritt",
    ]
    assert structure["list_items"] == [
        "Referenz FIN-731",
        "Verantwortlich: Kira Dalen",
        "Frist 2026-08-11",
        "Unterpunkt: https://portal.example.invalid/release-notes",
        "Unterpunkt: Tidal Gateway",
    ]
    rendered = render_plain_text(repaired_document)
    assert "Lieferstatus und Abhängigkeiten – Cobalt Sync" in rendered
    assert "Geschützte Referenzen" in rendered
    assert "https://portal.example.invalid/release-notes" in rendered
    assert "Tidal Gateway" in rendered
    assert "Fiktiver Geschäftsbetrieb" in rendered

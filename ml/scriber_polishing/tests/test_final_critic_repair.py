import pytest

from scriber_polishing.final_critic_repair import (
    ARTIFICIAL_CAUSAL_NOUN,
    ARTIFICIAL_MEETING_VALUE,
    ARTIFICIAL_REGISTRATION_LANGUAGE,
    ARTIFICIAL_TICKET_LANGUAGE,
    ENGLISH_RECORD_REFERENCE,
    GRAMMAR_CASE_AGREEMENT,
    HEADING_INITIAL_LOWERCASE,
    MALFORMED_MIXED_LANGUAGE_COMPOUND,
    MISSING_GENERAL_ARTICLE,
    MISSING_REE_LIST_ARTICLE,
    MISSING_REQUIRED_ARTICLE,
    MISSING_TAX_ARTICLE,
    PRESERVE_CODE_SWITCH_LANGUAGE,
    REDUNDANT_COMPLETION_CONDITION,
    REDUNDANT_RELEASE_CONDITION,
    REDUNDANT_REVIEW_PHRASE,
    UNSUPPORTED_UNCHANGED_BLOCK,
    apply_final_critic_repairs,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def test_naturalizes_english_record_reference_without_changing_protected_values() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": (
                        "Fictional Solstice Beacon Ltd. records reference NBS-184 "
                        "for Orbit Console v1.8.4 with fictional Juniper Meridian AG."
                    ),
                    "protected_values": [
                        "Solstice Beacon Ltd.",
                        "NBS-184",
                        "Orbit Console",
                        "v1.8.4",
                        "Juniper Meridian AG",
                    ],
                }
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Fictional Solstice Beacon Ltd. records reference NBS-184 for Orbit "
        "Console v1.8.4 together with fictional Juniper Meridian AG.[/P]\n"
        "[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({ENGLISH_RECORD_REFERENCE}),
    )

    expected = (
        "The fictional organization Solstice Beacon Ltd. documents reference NBS-184 "
        "for Orbit Console v1.8.4 together with the fictional organization "
        "Juniper Meridian AG."
    )
    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected
    assert result.plan["semantic_plan"]["facts"][0]["protected_values"] == [
        "Solstice Beacon Ltd.",
        "NBS-184",
        "Orbit Console",
        "v1.8.4",
        "Juniper Meridian AG",
    ]
    assert result.applied_occurrences == ((ENGLISH_RECORD_REFERENCE, 1),)


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        (
            "The meeting refers to https://example.invalid/notes and the value 3 business days.",
            "The meeting references https://example.invalid/notes and specifies a period of 3 business days.",
        ),
        (
            "Das Meeting verweist auf https://example.invalid/notes und den Wert 3 business days.",
            "Das Meeting verweist auf https://example.invalid/notes und nennt einen Zeitraum von 3 business days.",
        ),
    ],
)
def test_naturalizes_meeting_reference_and_value_without_dropping_either(
    old: str,
    expected: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f3",
                    "order": 3,
                    "text": old,
                    "protected_values": [
                        "https://example.invalid/notes",
                        "3 business days",
                    ],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({ARTIFICIAL_MEETING_VALUE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


def test_rephrases_the_redundant_release_condition_once() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "condition": "wenn die Freigabe vorliegt",
                    "text": (
                        "Der Termin am 01.09.2026 für Vorhaben Lumen-398 bleibt "
                        "vorbehaltlich der Freigabe bestehen."
                    ),
                    "protected_values": ["01.09.2026", "Vorhaben Lumen-398"],
                }
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Termin am 01.09.2026 für Vorhaben Lumen-398 bleibt vorbehaltlich "
        "der Freigabe bestehen, wenn die Freigabe vorliegt.[/P]\n"
        "[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({REDUNDANT_RELEASE_CONDITION}),
    )

    expected = (
        "Der Termin am 01.09.2026 für Vorhaben Lumen-398 wird verbindlich, "
        "wenn die Freigabe vorliegt."
    )
    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert result.plan["semantic_plan"]["facts"][0]["condition"] == (
        "wenn die Freigabe vorliegt"
    )
    assert render_plain_text(result.document) == expected


def test_removes_only_the_unsupported_unchanged_meta_block() -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {"fact_id": "f1", "order": 1, "text": "Fakt eins."},
                {"fact_id": "f2", "order": 2, "text": "Fakt zwei."},
            ]
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Fakt eins.[/P]\n"
        "[P]Inhalt bleibt unverändert.[/P]\n"
        "[P]Fakt zwei.[/P]\n"
        "[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({UNSUPPORTED_UNCHANGED_BLOCK}),
    )

    assert render_plain_text(result.document) == "Fakt eins.\n\nFakt zwei."
    assert result.plan == plan


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("die Budgetfreigabe", "Die Budgetfreigabe"),
        ("die priorisierte Umsetzung", "Die priorisierte Umsetzung"),
        ("die geordnete Übergabe", "Die geordnete Übergabe"),
    ],
)
def test_capitalizes_only_the_closed_unprotected_heading_scope(
    old: str,
    expected: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [{"fact_id": "f1", "order": 1, "text": "Der Fakt bleibt."}],
            "structure": {"paragraph_topics": ["Projektstatus", old]},
        }
    }
    document = parse_sst(
        f"[DOC]\n[H1]{old}[/H1]\n[P]Der Fakt bleibt.[/P]\n[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({HEADING_INITIAL_LOWERCASE}),
    )

    assert result.plan["semantic_plan"]["structure"]["paragraph_topics"] == [
        "Projektstatus",
        expected,
    ]
    assert render_plain_text(result.document) == f"{expected}\n\nDer Fakt bleibt."


@pytest.mark.parametrize(
    ("plan_text", "target_text", "expected_plan", "expected_target"),
    [
        (
            "Levin berichtet über den Fortschritt des schema migration.",
            "Levin berichtet über den Fortschritt des „schema migration“.",
            "Levin berichtet über den Fortschritt der schema migration.",
            "Levin berichtet über den Fortschritt der „schema migration“.",
        ),
        (
            "Vor der Freigabe wird das idempotency key gegen 250 ms geprüft.",
            "Vor der Freigabe wird den „idempotency key“ gegen 250 ms geprüft.",
            "Vor der Freigabe wird der idempotency key gegen 250 ms geprüft.",
            "Vor der Freigabe wird der „idempotency key“ gegen 250 ms geprüft.",
        ),
        (
            "Die Anforderung verlangt eine webhook signature-Prüfung im Pfad app/config.",
            "Die Anforderung verlangt eine Prüfung des „webhook signature“ im Pfad app/config.",
            "Die Anforderung verlangt eine Prüfung der webhook signature im Pfad app/config.",
            "Die Anforderung verlangt eine Prüfung der „webhook signature“ im Pfad app/config.",
        ),
    ],
)
def test_repairs_only_the_three_closed_case_agreement_patterns(
    plan_text: str,
    target_text: str,
    expected_plan: str,
    expected_target: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": plan_text,
                    "protected_values": ["schema migration"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{target_text}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({GRAMMAR_CASE_AGREEMENT}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected_plan
    assert render_plain_text(result.document) == expected_target


def test_adds_the_required_article_for_the_operating_temperature_limit() -> None:
    old = (
        "Die Wiederherstellung darf das Limit von 25 °C für Betriebstemperatur "
        "nicht überschreiten."
    )
    expected = (
        "Die Wiederherstellung darf das Limit von 25 °C für die Betriebstemperatur "
        "nicht überschreiten."
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f4",
                    "order": 4,
                    "text": old,
                    "protected_values": ["25 °C", "Betriebstemperatur"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({MISSING_REQUIRED_ARTICLE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


def test_rephrases_both_malformed_mixed_language_compounds_in_one_case() -> None:
    first = (
        "Die fiktive Tidal Prism AG liefert die API gateway-Rückmeldung bis "
        "2026-10-20."
    )
    second = "Die service-level agreement-Grenze von 2.5 TB ist verbindlich."
    expected_first = (
        "Die fiktive Tidal Prism AG liefert die Rückmeldung zum „API gateway“ bis "
        "2026-10-20."
    )
    expected_second = (
        "Die Grenze des „service-level agreement“ von 2.5 TB ist verbindlich."
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {"fact_id": "f2", "order": 2, "text": first},
                {"fact_id": "f3", "order": 3, "text": second},
            ]
        }
    }
    document = parse_sst(
        f"[DOC]\n[P]{first}[/P]\n[P]{second}[/P]\n[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({MALFORMED_MIXED_LANGUAGE_COMPOUND}),
    )

    assert [fact["text"] for fact in result.plan["semantic_plan"]["facts"]] == [
        expected_first,
        expected_second,
    ]
    assert render_plain_text(result.document) == (
        f"{expected_first}\n\n{expected_second}"
    )
    assert result.applied_occurrences == (
        (MALFORMED_MIXED_LANGUAGE_COMPOUND, 2),
    )


def test_collapses_the_duplicated_review_phrase_and_keeps_the_condition() -> None:
    fact = (
        "Die Rückmeldung kann nach Prüfung der Unterlagen erfolgen; eine "
        "Vorfestlegung wird nicht getroffen."
    )
    target = fact.replace(
        "Die Rückmeldung kann",
        "Nach vollständiger Prüfung kann die Rückmeldung",
        1,
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "condition": "nach vollständiger Prüfung",
                    "fact_id": "f6",
                    "order": 6,
                    "text": fact,
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{target}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({REDUNDANT_REVIEW_PHRASE}),
    )

    expected = (
        "Nach vollständiger Prüfung der Unterlagen kann die Rückmeldung "
        "erfolgen; eine Vorfestlegung wird nicht getroffen."
    )
    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert result.plan["semantic_plan"]["facts"][0]["condition"] == (
        "nach vollständiger Prüfung"
    )
    assert render_plain_text(result.document) == expected


def test_removes_only_the_condition_restatement_after_complete_review() -> None:
    first = (
        "Bitte stimmen Sie sich mit Jonas Falk ab; Jonas Falk hält die "
        "vereinbarte Reihenfolge fest."
    )
    second = (
        "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann."
    )
    unsupported = " Dies gilt, wenn die Prüfung abgeschlossen ist."
    plan = {
        "semantic_plan": {
            "facts": [
                {"fact_id": "f1", "order": 1, "text": first},
                {"fact_id": "f2", "order": 2, "text": second},
            ]
        }
    }
    document = parse_sst(
        f"[DOC]\n[P]{first} {second}{unsupported}[/P]\n[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({REDUNDANT_COMPLETION_CONDITION}),
    )

    assert result.plan == plan
    assert render_plain_text(result.document) == f"{first} {second}"


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        (
            "Registrations use only https://example.invalid/register.",
            "Registration is available only at https://example.invalid/register.",
        ),
        (
            "Anmeldungen verwenden ausschließlich https://example.invalid/register.",
            "Die Anmeldung ist ausschließlich unter https://example.invalid/register möglich.",
        ),
    ],
)
def test_rephrases_registration_urls_as_natural_instructions(
    old: str,
    expected: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f3",
                    "order": 3,
                    "text": old,
                    "protected_values": ["https://example.invalid/register"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({ARTIFICIAL_REGISTRATION_LANGUAGE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


def test_rephrases_ticket_assignment_and_version_protection_as_two_clear_clauses() -> None:
    old = (
        "Die Übergabe ordnet das Ticket NBS-184 Luca Sable zu und schützt v1.8.4."
    )
    expected = (
        "Die Übergabe ordnet das Ticket NBS-184 Luca Sable zu; die Version v1.8.4 "
        "bleibt geschützt."
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": old,
                    "protected_values": ["NBS-184", "Luca Sable", "v1.8.4"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({ARTIFICIAL_TICKET_LANGUAGE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


@pytest.mark.parametrize(
    "translated",
    [
        (
            "Der technische Begriff „single sign-on“ sowie die Referenz "
            "https://example.invalid/docs sind exakt beizubehalten."
        ),
        (
            "Bitte behalten Sie den technischen Begriff „single sign-on“ sowie "
            "die Referenz https://example.invalid/docs exakt bei."
        ),
    ],
)
def test_restores_the_declared_code_switch_instruction_language(
    translated: str,
) -> None:
    plan_text = (
        "Bitte keep the technical term single sign-on and reference "
        "https://example.invalid/docs exactly as written."
    )
    expected = (
        "Bitte keep the technical term single sign-on and reference "
        "https://example.invalid/docs exactly as written."
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f4",
                    "order": 4,
                    "text": plan_text,
                    "protected_values": [
                        "single sign-on",
                        "https://example.invalid/docs",
                    ],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{translated}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({PRESERVE_CODE_SWITCH_LANGUAGE}),
    )

    assert result.plan == plan
    assert render_plain_text(result.document) == expected


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        (
            "Mara Linden bestätigt für Vorgang OP-800001, dass der Wortlaut bleibt.",
            "Mara Linden bestätigt für den Vorgang OP-800001, dass der Wortlaut bleibt.",
        ),
        (
            "Mara Linden hält für Vorgang OP-800001 fest, dass die Form bleibt.",
            "Mara Linden hält für den Vorgang OP-800001 fest, dass die Form bleibt.",
        ),
        (
            "Mara Linden bestätigt für Referenz UN-900001, dass M-2 erhalten bleibt.",
            "Mara Linden bestätigt zur Referenz UN-900001, dass M-2 erhalten bleibt.",
        ),
        (
            "Bitte stimmen Sie sich mit Mara Linden zu Vorgang OP-800001 ab.",
            "Bitte stimmen Sie sich mit Mara Linden zum Vorgang OP-800001 ab.",
        ),
    ],
)
def test_adds_the_closed_general_noun_article(
    old: str,
    expected: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": old,
                    "protected_values": ["OP-800001"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({MISSING_GENERAL_ARTICLE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("Zuordnung zu Nachtrag NF-19", "Zuordnung zum Nachtrag NF-19"),
        ("Zuordnung zu Prüfvermerk EN-63", "Zuordnung zum Prüfvermerk EN-63"),
        ("Zuordnung zu Vorgang REE-71", "Zuordnung zum Vorgang REE-71"),
    ],
)
def test_adds_the_required_article_to_the_closed_ree_list_items(
    old: str,
    expected: str,
) -> None:
    plan = {
        "semantic_plan": {
            "facts": [{"fact_id": "f1", "order": 1, "text": "Der Fakt bleibt."}],
            "structure": {
                "list_kind": "unordered",
                "list_items": ["Fiktive Prüfung", old],
            },
        }
    }
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Fakt bleibt.[/P]\n"
        "[UL]\n"
        "[LI1]Fiktive Prüfung[/LI1]\n"
        f"[LI1]{old}[/LI1]\n"
        "[/UL]\n"
        "[/DOC]"
    )

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({MISSING_REE_LIST_ARTICLE}),
    )

    assert result.plan["semantic_plan"]["structure"]["list_items"] == [
        "Fiktive Prüfung",
        expected,
    ]
    assert expected in render_plain_text(result.document)


@pytest.mark.parametrize(
    ("noun", "contracted"),
    [
        ("Abschreibungsbasis", "zur Abschreibungsbasis"),
        ("Aktennotiz", "zur Aktennotiz"),
        ("Bemessungsgrundlage", "zur Bemessungsgrundlage"),
        ("Bescheidvergleich", "zum Bescheidvergleich"),
        ("Kontenabstimmung", "zur Kontenabstimmung"),
        ("Mietaufwand", "zum Mietaufwand"),
        ("Rückfrage", "zur Rückfrage"),
        ("Verlustvortrag", "zum Verlustvortrag"),
        ("Vorsteuerabzug", "zum Vorsteuerabzug"),
        ("Zahlungsstand", "zum Zahlungsstand"),
    ],
)
def test_adds_the_natural_article_to_the_closed_tax_explanation_terms(
    noun: str,
    contracted: str,
) -> None:
    old = f"Kira Falk soll die Erläuterung zu {noun} bis zum 03.02.2026 übermitteln."
    expected = (
        f"Kira Falk soll die Erläuterung {contracted} bis zum 03.02.2026 übermitteln."
    )
    plan = {
        "semantic_plan": {
            "facts": [{"fact_id": "f3", "order": 3, "text": old}]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({MISSING_TAX_ARTICLE}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected


def test_rephrases_the_artificial_causal_noun_without_asserting_a_cause() -> None:
    old = (
        "Die Nachverfolgung misst 768 MiB als Container-Limit; der Wert ist keine "
        "Ursachebehauptung."
    )
    expected = (
        "Die Nachverfolgung misst 768 MiB als Container-Limit; damit ist keine "
        "Aussage über eine Ursache verbunden."
    )
    plan = {
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f3",
                    "order": 3,
                    "text": old,
                    "protected_values": ["768 MiB"],
                }
            ]
        }
    }
    document = parse_sst(f"[DOC]\n[P]{old}[/P]\n[/DOC]")

    result = apply_final_critic_repairs(
        plan,
        document,
        frozenset({ARTIFICIAL_CAUSAL_NOUN}),
    )

    assert result.plan["semantic_plan"]["facts"][0]["text"] == expected
    assert render_plain_text(result.document) == expected

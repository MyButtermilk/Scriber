from scriber_polishing.critic_repair import repair_document
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_html, render_plain_text


def _plan(*topics: str) -> dict[str, object]:
    return {
        "semantic_plan": {
            "structure": {
                "paragraph_topics": list(topics),
            }
        }
    }


def test_repairs_government_article_and_rich_text_spacing_without_plain_text_nbsp() -> None:
    document = parse_sst("[DOC]\n[P]Die Mitteilung von Finanzamt Sonnenfeld nennt 57.240,30 € und 19 %.[/P]\n[/DOC]")

    repaired = repair_document(
        document,
        _plan("Steuerlicher Sachstand"),
        {"missing_government_office_article", "rich_text_nonbreaking_space_missing"},
    )

    assert render_plain_text(repaired) == ("Die Mitteilung vom Finanzamt Sonnenfeld nennt 57.240,30 € und 19 %.")
    assert render_html(repaired) == (
        "<p>Die Mitteilung vom Finanzamt Sonnenfeld nennt 57.240,30&nbsp;€ und 19&nbsp;%.</p>"
    )


def test_repairs_gendered_salutation_and_restores_natural_pronoun() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[SALUTATION]Liebe Jonas Falk,[/SALUTATION]\n"
        "[P]Mara Linden 37 teilte mit, dass Mara Linden 37 die Unterlagen nicht versendet.[/P]\n"
        "[/DOC]"
    )

    repaired = repair_document(
        document,
        _plan("Rückmeldung"),
        {"greeting_agreement_error", "awkward_pronoun_repetition"},
    )

    assert render_plain_text(repaired) == (
        "Lieber Jonas Falk,\n\nMara Linden 37 teilte mit, dass sie die Unterlagen nicht versendet."
    )


def test_repairs_redundant_heading_from_declared_topic_without_removing_structure() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[SUBJECT]Ambiguitätsvermerk[/SUBJECT]\n"
        "[H1]Ambiguitätsvermerk[/H1]\n"
        "[P]Der Wortlaut bleibt unverändert.[/P]\n"
        "[/DOC]"
    )

    repaired = repair_document(
        document,
        _plan("Ambige Zeichenfolge", "Kontextschutz"),
        {"accidental_repetition", "redundant_heading"},
    )

    assert render_plain_text(repaired) == (
        "Ambiguitätsvermerk\n\nAmbige Zeichenfolge\n\nDer Wortlaut bleibt unverändert."
    )


def test_repairs_protected_english_terms_by_changing_only_german_scaffolding() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Die Anforderung verlangt eine idempotency key-Prüfung.[/P]\n"
        "[P]Der „canary deployment“ darf den „feature flag“ nicht aktivieren.[/P]\n"
        "[/DOC]"
    )

    repaired = repair_document(
        document,
        _plan("Technische Prüfung"),
        {"technical_compound_error", "technical_term_agreement_error"},
    )

    assert render_plain_text(repaired) == (
        "Die Anforderung verlangt eine Prüfung des „idempotency key“.\n\n"
        "Das „canary deployment“ darf das „feature flag“ nicht aktivieren."
    )


def test_repair_fails_closed_for_unknown_critic_flag() -> None:
    document = parse_sst("[DOC]\n[P]Unverändert.[/P]\n[/DOC]")

    try:
        repair_document(document, _plan("Sachstand"), {"unreviewed_repair"})
    except ValueError as error:
        assert "unsupported critic repair flag" in str(error)
    else:
        raise AssertionError("unknown critic flag must fail closed")

from __future__ import annotations

import pytest

from scriber_polishing.coverage_audit import CoverageAuditError, assert_mandatory_coverage, audit_coverage


def _record(
    example_id: str,
    *,
    split: str = "train",
    source_text: str = "Bitte prüfen Sie den Betrag von 12,00 Euro.",
    target_text: str = "Bitte prüfen Sie den Betrag von 12,00 €.",
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "split": split,
        "domain": "tax_legal",
        "language": "de-DE",
        "address_mode": "formal_sie",
        "risk_tags": ["formal_address", "third_person_reference", "accepted_variant"],
        "source_text": source_text,
        "target_plain_text": target_text,
        "target_ast": {
            "schema_version": 1,
            "blocks": [
                {"type": "subject", "text": "Prüfung", "inlines": [], "items": [], "lines": []},
                {"type": "heading_1", "text": "Status", "inlines": [], "items": [], "lines": []},
                {"type": "paragraph", "text": target_text, "inlines": [], "items": [], "lines": []},
                {"type": "paragraph", "text": "Bitte antworten Sie.", "inlines": [], "items": [], "lines": []},
                {
                    "type": "ordered_list",
                    "text": "",
                    "inlines": [],
                    "lines": [],
                    "items": [
                        {"level": 1, "text": "Prüfen", "inlines": []},
                        {"level": 2, "text": "Freigeben", "inlines": []},
                    ],
                },
                {"type": "salutation", "text": "Sehr geehrte Frau Beispiel,", "inlines": [], "items": [], "lines": []},
                {"type": "closing", "text": "Mit freundlichen Grüßen", "inlines": [], "items": [], "lines": []},
                {"type": "signature", "text": "", "inlines": [], "items": [], "lines": ["Alex", "Scriber"]},
            ],
        },
        "operations": ["grammar_error", "spelling_error", "punctuation_error", "repetition", "self_correction"],
        "value_mappings": [
            {"kind": "unit_surface", "source": "12,00 Euro", "target": "12,00 €"},
            {
                "kind": "legal_citation_surface",
                "source": "Paragraf 7 Absatz 4 Satz 2 Einkommensteuergesetz",
                "target": "§ 7 Abs. 4 Satz 2 EStG",
            },
        ],
        "corruption_profile": "heavy_combined",
        "applied_normalizations": ["house_style"],
        "semantic_plan": {"entities": [{"type": "organization", "value": "Scriber GmbH"}]},
        "provenance": {
            "canonical_generator_id": "local_canonical_expander",
            "plan_generator_agent": "generator_tax_legal_01",
            "target_writer_agent": "writer_tax_legal_01",
        },
    }


def test_audit_uses_explicit_evidence_and_separates_total_from_train_counts() -> None:
    train = _record("example_train")
    validation = _record("example_validation", split="validation")

    report = audit_coverage((validation, train))

    assert report.record_count == 2
    assert report.train_record_count == 1
    assert report.quota("german_business_communication").total_count == 2
    assert report.quota("german_business_communication").train_count == 1
    assert report.quota("tax_legal_terminology").train_count == 1
    assert report.quota("numbered_lists").train_count == 1
    assert report.quota("nested_lists").train_count == 1
    assert report.quota("spoken_unit_conversions").train_count == 1
    assert report.quota("complete_legal_citations").train_count == 1
    assert report.quota("formal_sie_addresses").train_count == 1
    assert report.quota("accepted_spelling_variants").train_count == 1
    assert report.diversity["unique_normalized_source_texts"] == 1
    assert report.diversity["unique_generator_families"] == 1
    assert report.passed is False


def test_audit_is_deterministic_and_fail_closed_with_a_sorted_deficit_report() -> None:
    first = _record("example_first")
    second = _record(
        "example_second", source_text="Bitte prüfen Sie den Vertrag.", target_text="Bitte prüfen Sie den Vertrag."
    )

    report = audit_coverage((first, second))

    assert report == audit_coverage((second, first))
    with pytest.raises(CoverageAuditError, match="abandoned_starts") as error:
        assert_mandatory_coverage((first, second))
    assert error.value.report is report or error.value.report.passed is False
    assert list(error.value.deficits) == sorted(error.value.deficits)


def test_audit_rejects_invalid_evidence_instead_of_guessing() -> None:
    invalid = _record("example_invalid")
    invalid["operations"] = ["grammar_error", 3]

    with pytest.raises(ValueError, match="operations"):
        audit_coverage((invalid,))


def test_audit_recognizes_long_form_legal_hierarchy_and_article_citations() -> None:
    record = _record(
        "legal_long",
        target_text=(
            "Es gelten § 7 Absatz 4 Satz 2 EStG, § 8 Nummer 1 Buchstabe a GewStG, "
            "§§ 8c und 8d KStG sowie Artikel 3 Absatz 1 GG."
        ),
    )
    record["value_mappings"] = []

    report = audit_coverage((record,))

    assert report.quota("complete_legal_citations").train_count == 1
    assert report.quota("citations_with_paragraph_and_sentence").train_count == 1
    assert report.quota("citations_with_number_and_letter").train_count == 1
    assert report.quota("multiple_section_citations").train_count == 1
    assert report.quota("article_citations").train_count == 1
    assert report.quota("tax_law_citations").train_count == 1


def test_audit_credits_only_explicit_hard_negative_and_orthography_evidence() -> None:
    hard_negative = _record(
        "hard_negative",
        source_text="Das Wort Überschrift bleibt Inhalt; 18 K bleibt unverändert.",
        target_text="Das Wort Überschrift bleibt Inhalt; 18 K bleibt unverändert.",
    )
    hard_negative["domain"] = "hard_negatives"
    hard_negative["risk_tags"] = [
        "do_not_invent_format",
        "format_word_ordinary",
        "legal_hard_negative",
        "ambiguous_unit_hard_negative",
    ]
    hard_negative["target_ast"]["blocks"] = [
        block
        for block in hard_negative["target_ast"]["blocks"]
        if block["type"] not in {"heading_1", "heading_2"}
    ]
    hard_negative["value_mappings"] = []
    hard_negative["applied_normalizations"] = []
    orthography = _record("orthography")
    orthography["domain"] = "general_business"
    orthography["risk_tags"] = ["orthography_pronouns"]
    orthography["value_mappings"] = [
        {"kind": "orthography_surface", "source": "zur Zeit", "target": "zurzeit"}
    ]
    orthography["corruption_profile"] = "identity"

    report = audit_coverage((hard_negative, orthography))

    assert report.quota("hard_negative_no_heading").train_count == 1
    assert report.quota("hard_negative_formatting_terms").train_count == 1
    assert report.quota("hard_negative_legal_hierarchy").train_count == 1
    assert report.quota("hard_negative_no_unit_conversion").train_count == 1
    assert report.quota("accepted_spelling_variants").train_count == 1
    assert report.quota("house_style_normalizations").train_count == 1
    assert report.quota("identity_preferred_house_style").train_count == 1


def test_four_semantic_facts_can_prove_four_thematic_paragraphs() -> None:
    record = _record("four_topics")
    paragraph = {
        "type": "paragraph",
        "text": "Eigenständiger Sachverhalt.",
        "inlines": [],
        "items": [],
        "lines": [],
    }
    record["target_ast"]["blocks"].extend([paragraph, paragraph])
    record["semantic_plan"]["facts"] = [
        {"fact_id": f"f{index}", "text": f"Sachverhalt {index}."}
        for index in range(4)
    ]

    report = audit_coverage((record,))

    assert report.quota("four_or_more_thematic_paragraphs").train_count == 1


def test_compound_unit_and_context_spelling_operations_are_explicit_quota_evidence() -> None:
    record = _record(
        "compound_units",
        source_text="Der Wert ist 120 Kilowattstunden je Quadratmeter und Jahr; das ist klar.",
        target_text="Der Wert ist 120 kWh/(m²·a); dass ist klar.",
    )
    record["operations"].append("context_spelling_error")

    report = audit_coverage((record,))

    assert report.quota("energy_per_area_year").train_count == 1
    assert report.quota("speed_or_compound_units").train_count == 1
    assert report.quota("das_dass_seit_seid_wider_wieder_ss_sz").train_count == 1

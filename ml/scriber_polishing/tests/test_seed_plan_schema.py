from copy import deepcopy

import pytest

from scriber_polishing.schemas import validate_seed_plan

VALID_PLAN = {
    "schema_version": 1,
    "seed_id": "general_business_0001",
    "generator_agent": "generator_business_a",
    "batch_id": "business_batch_01",
    "prompt_hash": "sha256:" + "a" * 64,
    "seed": 1,
    "language": "de-DE",
    "domain": "general_business",
    "document_type": "business_email",
    "style_profile": "duden_preferred_business",
    "address_mode": "formal_sie",
    "legal_citation_style": "long",
    "unit_style": "professional_symbols",
    "semantic_plan": {
        "facts": [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Die fiktive Orion Projekt GmbH benötigt die Unterlagen.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["Orion Projekt GmbH"],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Die Rückmeldung soll bis zum 15.08.2026 erfolgen.",
                "polarity": "positive",
                "modality": "recommendation",
                "condition": None,
                "protected_values": ["15.08.2026"],
            },
        ],
        "entities": [
            {
                "type": "organization",
                "value": "Orion Projekt GmbH",
                "fictional": True,
                "protected": True,
            }
        ],
        "structure": {
            "has_subject": True,
            "has_salutation": True,
            "paragraph_topics": ["Unterlagen", "Rückmeldefrist"],
            "heading_levels": [],
            "list_kind": "none",
            "list_items": [],
            "has_closing": True,
            "signature_lines": ["Mara Klein", "Projektbüro"],
        },
    },
    "risk_tags": ["date", "formal_address"],
    "source_class": "ai_generated",
    "private_data": False,
}


def test_valid_ai_seed_semantic_plan_passes_the_versioned_schema() -> None:
    assert validate_seed_plan(VALID_PLAN) == VALID_PLAN


def test_ai_seed_rejects_private_or_unstructured_content() -> None:
    private = deepcopy(VALID_PLAN)
    private["private_data"] = True
    unstructured = deepcopy(VALID_PLAN)
    del unstructured["semantic_plan"]["facts"]

    with pytest.raises(ValueError, match="seed plan failed"):
        validate_seed_plan(private)
    with pytest.raises(ValueError, match="seed plan failed"):
        validate_seed_plan(unstructured)


def test_ai_seed_accepts_explicit_quote_attachment_and_post_script_structure() -> None:
    structured = deepcopy(VALID_PLAN)
    structure = structured["semantic_plan"]["structure"]
    structure.update(
        {
            "has_quote": True,
            "quote_text": "„Die Freigabe gilt nur für Projekt Orion.“",
            "has_attachments": True,
            "attachment_lines": ["Anlage 1: Fiktiver Prüfplan"],
            "has_post_script": True,
            "post_script_text": "P. S.: Bitte beachten Sie die Rückmeldefrist.",
        }
    )

    assert validate_seed_plan(structured) == structured

    inconsistent = deepcopy(structured)
    inconsistent["semantic_plan"]["structure"]["attachment_lines"] = []
    with pytest.raises(ValueError, match="seed plan failed"):
        validate_seed_plan(inconsistent)

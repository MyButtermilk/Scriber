"""Behavioural checks for schema-backed canonical data validation."""

from __future__ import annotations

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.document_ast import Block, BlockType, Document
from scriber_polishing.validators import validate_canonical_record


def _canonical_record() -> dict[str, object]:
    document = Document(blocks=(Block(BlockType.PARAGRAPH, text="Bitte prüfen Sie den Betrag."),))
    return {
        "schema_version": 1,
        "canonical_document_id": "seed-de-business-0001",
        "seed_id": "general_business_100001",
        "source_plan_batch": "general_business_batch_01",
        "plan_generator_agent": "generator_general_business_01",
        "target_writer_agent": "target_writer_business_01",
        "target_prompt_hash": f"sha256:{'a' * 64}",
        "semantic_plan_sha256": f"sha256:{'b' * 64}",
        "source_text": "bitte pruefen sie den betrag",
        "target_sst": "[DOC]\n[P]Bitte prüfen Sie den Betrag.[/P]\n[/DOC]",
        "target_plain_text": "Bitte prüfen Sie den Betrag.",
        "target_markdown": "Bitte prüfen Sie den Betrag.",
        "target_html": "<p>Bitte prüfen Sie den Betrag.</p>",
        "target_ast": document_to_dict(document),
        "language": "de-DE",
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "normalize_valid_variants": True,
        "protected_spans": [],
        "applied_normalizations": [],
        "rejected_normalizations": [],
        "ambiguity_flags": [],
    }


def test_canonical_record_is_accepted_only_when_it_satisfies_the_sst_v1_schema() -> None:
    assert validate_canonical_record(_canonical_record()) == _canonical_record()


def test_canonical_record_rejects_unknown_fields_and_missing_required_fields() -> None:
    malformed = _canonical_record() | {"unexpected": "not allowed"}
    malformed.pop("target_sst")

    with pytest.raises(ValueError, match="canonical record"):
        validate_canonical_record(malformed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_plain_text", "anderer Inhalt", "plain-text"),
        ("target_markdown", "**anderer Inhalt**", "Markdown"),
        ("target_html", "<script>unsafe()</script>", "HTML"),
        ("target_ast", {"schema_version": 1, "blocks": []}, "AST"),
        ("protected_spans", ["1.000,00 €"], "protected span"),
    ],
)
def test_canonical_record_rejects_cross_representation_or_protection_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    malformed = _canonical_record()
    malformed[field] = value

    with pytest.raises(ValueError, match=message):
        validate_canonical_record(malformed)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.hub_regression_sources import (
    AI_GOLD_CATEGORY,
    REQUIRED_CATEGORIES,
    SYNTHETIC_CATEGORIES,
    HubRegressionSourceError,
    build_hub_regression_sources,
    write_hub_regression_sources,
)
from scriber_polishing.sst_parser import parse_sst


def _record(index: int, category: str, *, split: str = "validation") -> dict[str, object]:
    risk_tags: list[str] = []
    operations: list[str] = []
    domain = "technical_project"
    paragraph_boundaries: list[int] = [0]
    heading_boundaries: list[int] = []
    list_boundaries: list[int] = []
    address_mode = "neutral_third_person"
    severity = "medium"
    corruption_profile = "punctuation"
    if category == "business_post":
        domain = "general_business"
    elif category == "tax_law":
        domain = "tax_legal"
    elif category == "paragraphs":
        paragraph_boundaries = [0, 10]
    elif category == "headings":
        heading_boundaries = [0]
    elif category == "lists":
        list_boundaries = [0]
    elif category == "units":
        operations = ["unit_spoken"]
    elif category == "legal_citations":
        operations = ["legal_citation_spoken"]
    elif category == "address_pronouns":
        address_mode = "formal_sie"
    elif category == "identity":
        severity = "identity"
        corruption_profile = "identity"
    protected = f"AZ-{category}-{index:04d}"
    target_sst = f"[DOC]\n[P]Bitte bestätigen Sie {protected}.[/P]\n[/DOC]"
    target_plain_text = f"Bitte bestätigen Sie {protected}."
    return {
        "schema_version": 1,
        "example_id": f"example_{category}_{index:04d}",
        "canonical_document_id": f"canonical_{category}_{index:04d}",
        "seed_id": f"seed_{category}_{index:04d}",
        "split": split,
        "domain": domain,
        "address_mode": address_mode,
        "risk_tags": risk_tags,
        "source_text": (
            target_plain_text
            if severity == "identity"
            else f"bitte bestätigen sie {protected}"
        ),
        "target_sst": target_sst,
        "target_plain_text": target_plain_text,
        "target_ast": document_to_dict(parse_sst(target_sst)),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": f"Die Bestätigung betrifft {protected}.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [protected],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Bestätigung wird erbeten.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                }
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Bestätigung"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [protected],
        "paragraph_boundaries": paragraph_boundaries,
        "heading_boundaries": heading_boundaries,
        "list_boundaries": list_boundaries,
        "operations": operations,
        "applied_normalizations": [],
        "severity": severity,
        "corruption_profile": corruption_profile,
        "provenance": {
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _source_pools(tmp_path: Path) -> tuple[Path, Path]:
    synthetic = tmp_path / "synthetic.jsonl"
    ai_gold = tmp_path / "ai-gold.jsonl"
    _write_jsonl(
        synthetic,
        [
            _record(index, category)
            for category in SYNTHETIC_CATEGORIES
            for index in range(25)
        ],
    )
    _write_jsonl(
        ai_gold,
        [_record(index + 1000, AI_GOLD_CATEGORY, split="train") for index in range(25)],
    )
    return synthetic, ai_gold


def test_builds_deterministic_110_case_non_holdout_suite(tmp_path: Path) -> None:
    synthetic, ai_gold = _source_pools(tmp_path)
    first = build_hub_regression_sources(
        synthetic_paths=[synthetic],
        ai_gold_paths=[ai_gold],
    )
    second = build_hub_regression_sources(
        synthetic_paths=[synthetic],
        ai_gold_paths=[ai_gold],
    )

    assert first == second
    assert len(first.sources) == 110
    assert len(first.references) == 110
    assert set(first.evidence["category_counts"]) == set(REQUIRED_CATEGORIES)
    assert set(first.evidence["category_counts"].values()) == {10}
    assert first.evidence["synthetic_case_count"] == 100
    assert first.evidence["ai_gold_smoke_count"] == 10
    assert first.evidence["holdout_data"] is False
    assert first.evidence["private_data"] is False
    assert len({record["case_id"] for record in first.sources}) == 110
    assert {record["split"] for record in first.references} == {"regression"}
    assert all(record["holdout"] is False for record in first.sources)

    sources_output = tmp_path / "out" / "sources.jsonl"
    references_output = tmp_path / "out" / "references.jsonl"
    report_output = tmp_path / "out" / "report.json"
    write_hub_regression_sources(
        first,
        sources_output=sources_output,
        references_output=references_output,
        report_output=report_output,
    )
    write_hub_regression_sources(
        first,
        sources_output=sources_output,
        references_output=references_output,
        report_output=report_output,
    )
    assert sources_output.read_bytes().endswith(b"\n")
    assert references_output.read_bytes().endswith(b"\n")


def test_rejects_holdout_record_before_selection(tmp_path: Path) -> None:
    synthetic, ai_gold = _source_pools(tmp_path)
    holdout = tmp_path / "holdout.jsonl"
    _write_jsonl(holdout, [_record(9999, "identity", split="test")])

    with pytest.raises(HubRegressionSourceError, match="holdout"):
        build_hub_regression_sources(
            synthetic_paths=[holdout, synthetic],
            ai_gold_paths=[ai_gold],
        )


def test_fails_closed_when_one_category_is_underfilled(tmp_path: Path) -> None:
    synthetic = tmp_path / "synthetic.jsonl"
    ai_gold = tmp_path / "ai-gold.jsonl"
    _write_jsonl(
        synthetic,
        [_record(index, "identity") for index in range(25)],
    )
    _write_jsonl(
        ai_gold,
        [_record(index + 1000, AI_GOLD_CATEGORY, split="train") for index in range(25)],
    )

    with pytest.raises(HubRegressionSourceError, match="business_post"):
        build_hub_regression_sources(
            synthetic_paths=[synthetic],
            ai_gold_paths=[ai_gold],
        )

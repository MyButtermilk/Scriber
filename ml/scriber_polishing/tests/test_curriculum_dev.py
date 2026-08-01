from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.curriculum_dev import (
    build_curriculum_dev_references,
    exclusion_ids_from_manifest,
    load_curriculum_dev_manifest,
    write_curriculum_dev_references,
)
from scriber_polishing.evaluation_io import load_evaluation_references
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _pair(
    example_id: str,
    profile: str,
    *,
    domain: str = "general_business",
) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    target_plain_text = render_plain_text(document)
    return {
        "example_id": example_id,
        "split": "validation",
        "source_text": "bitte bis 31.07.2026 antworten",
        "target_sst": target_sst,
        "target_plain_text": target_plain_text,
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Eine Antwort ist erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Frist endet am 31.07.2026.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["31.07.2026"],
                }
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Antwortfrist"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": ["31.07.2026"],
        "risk_tags": ["Dates"],
        "domain": domain,
        "document_type": "Geschaeftsbrief",
        "corruption_profile": profile,
        "operations": ["lowercase_sentence_start"],
        "provenance": {
            "synthetic_only": True,
            "human_curation": False,
            "generative_api": False,
        },
    }


def _records(per_phase: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    specs = (
        ("p1", "identity", "general_business"),
        ("p2", "grammar", "general_business"),
        ("p3", "spoken_formatting_list", "general_business"),
        ("p4", "domain_specific_cleanup", "general_business"),
        ("p5", "identity", "hard_negatives"),
    )
    for prefix, profile, domain in specs:
        for index in range(per_phase):
            rows.append(
                _pair(
                    f"{prefix}_case_{index:02d}",
                    profile,
                    domain=domain,
                )
            )
    return rows


def _source_binding(count: int) -> dict[str, object]:
    return {
        "filename": "validation.jsonl",
        "sha256": "a" * 64,
        "record_count": count,
        "split": "validation",
    }


def _ai_binding() -> dict[str, object]:
    return {
        "filename": "ai_gold_train.jsonl",
        "sha256": "b" * 64,
        "record_count": 1,
        "split": "train",
    }


def test_balanced_dev_selection_is_validation_only_deterministic_and_immutable(
    tmp_path: Path,
) -> None:
    records = _records()
    kwargs = {
        "records": records,
        "source_binding": _source_binding(len(records)),
        "ai_gold_ids": frozenset({"unrelated_gold_train"}),
        "ai_gold_binding": _ai_binding(),
        "purpose": "curriculum",
        "per_phase": 2,
        "seed": 270013,
    }
    references, pending = build_curriculum_dev_references(**kwargs)
    replay_references, replay_pending = build_curriculum_dev_references(**kwargs)

    assert references == replay_references
    assert pending == replay_pending
    assert pending["phase_counts"] == {str(phase): 2 for phase in range(1, 6)}
    assert pending["holdout_inputs"] is False
    assert {reference["split"] for reference in references} == {"regression"}
    assert all(
        any(tag.startswith("curriculum_phase_") for tag in reference["slice_tags"])
        for reference in references
    )

    destination = tmp_path / "curriculum.references.jsonl"
    manifest_destination = tmp_path / "curriculum.references.manifest.json"
    manifest = write_curriculum_dev_references(
        references,
        pending,
        references_destination=destination,
        manifest_destination=manifest_destination,
    )
    assert (
        manifest["references"]["sha256"]
        == hashlib.sha256(destination.read_bytes()).hexdigest()
    )
    manifest_sha256 = hashlib.sha256(manifest_destination.read_bytes()).hexdigest()
    assert (
        load_curriculum_dev_manifest(
            manifest_destination,
            expected_sha256=manifest_sha256,
        )
        == manifest
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_curriculum_dev_manifest(
            manifest_destination,
            expected_sha256="0" * 64,
        )
    assert len(load_evaluation_references(destination)) == 10


def test_checkpoint_selection_can_exclude_every_prior_curriculum_case() -> None:
    records = _records()
    common = {
        "records": records,
        "source_binding": _source_binding(len(records)),
        "ai_gold_ids": frozenset({"unrelated_gold_train"}),
        "ai_gold_binding": _ai_binding(),
        "per_phase": 1,
        "seed": 270013,
    }
    _, curriculum_manifest = build_curriculum_dev_references(
        purpose="curriculum",
        **common,
    )
    prior = exclusion_ids_from_manifest(curriculum_manifest)
    _, checkpoint_manifest = build_curriculum_dev_references(
        purpose="checkpoint_selection",
        excluded_example_ids=prior,
        **common,
    )

    assert prior.isdisjoint(checkpoint_manifest["selected_example_ids"])
    assert checkpoint_manifest["exclusions"]["count"] == 5


def test_dev_selection_rejects_insufficient_phase_and_ai_gold_train_overlap() -> None:
    records = _records(per_phase=1)
    kwargs = {
        "records": records,
        "source_binding": _source_binding(len(records)),
        "ai_gold_ids": frozenset({"unrelated_gold_train"}),
        "ai_gold_binding": _ai_binding(),
        "purpose": "curriculum",
        "per_phase": 1,
        "seed": 270013,
    }
    with pytest.raises(ValueError, match="insufficient development phase 1"):
        build_curriculum_dev_references(
            excluded_example_ids=frozenset({"p1_case_00"}),
            **kwargs,
        )

    with pytest.raises(ValueError, match="overlaps AI-Gold train"):
        build_curriculum_dev_references(
            **{
                **kwargs,
                "ai_gold_ids": frozenset({"p4_case_00"}),
            }
        )


def test_written_dev_manifest_refuses_different_replay(tmp_path: Path) -> None:
    records = _records()
    common = {
        "records": records,
        "source_binding": _source_binding(len(records)),
        "ai_gold_ids": frozenset({"unrelated_gold_train"}),
        "ai_gold_binding": _ai_binding(),
        "purpose": "curriculum",
        "per_phase": 1,
        "seed": 270013,
    }
    references, manifest = build_curriculum_dev_references(**common)
    destination = tmp_path / "references.jsonl"
    manifest_destination = tmp_path / "manifest.json"
    write_curriculum_dev_references(
        references,
        manifest,
        references_destination=destination,
        manifest_destination=manifest_destination,
    )
    changed_references, changed_manifest = build_curriculum_dev_references(
        **{**common, "seed": 270014}
    )
    with pytest.raises(ValueError, match="overwrite"):
        write_curriculum_dev_references(
            changed_references,
            changed_manifest,
            references_destination=destination,
            manifest_destination=manifest_destination,
        )

    assert json.loads(manifest_destination.read_text(encoding="utf-8"))[
        "selected_example_ids"
    ] == manifest["selected_example_ids"]

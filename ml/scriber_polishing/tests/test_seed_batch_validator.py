"""Tests for the fail-closed AI seed batch validation boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scriber_polishing.seed_batch_validator import SeedBatchValidationError, validate_seed_batches


def _plan(*, seed: int, batch_id: str, generator_agent: str, prompt_hash: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed_id": f"seed_plan_{seed}",
        "generator_agent": generator_agent,
        "batch_id": batch_id,
        "prompt_hash": prompt_hash,
        "seed": seed,
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
                    "text": "Die fiktive Orion GmbH benötigt die Unterlagen.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["Orion GmbH"],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Rückmeldung erfolgt bis zum 15.08.2026.",
                    "polarity": "positive",
                    "modality": "recommendation",
                    "condition": None,
                    "protected_values": ["15.08.2026"],
                },
            ],
            "entities": [{"type": "organization", "value": "Orion GmbH", "fictional": True, "protected": True}],
            "structure": {
                "has_subject": True,
                "has_salutation": True,
                "paragraph_topics": ["Unterlagen"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": True,
                "signature_lines": ["Mara Klein"],
            },
        },
        "risk_tags": ["formal_address"],
        "source_class": "ai_generated",
        "private_data": False,
    }


def _write_batch(root: Path, name: str, plans: list[dict[str, object]]) -> Path:
    batch = root / name
    batch.mkdir()
    plans_path = batch / "plans.jsonl"
    plans_path.write_bytes(b"".join(json.dumps(plan, sort_keys=True).encode() + b"\n" for plan in plans))
    first = plans[0]
    manifest = {
        "record_count": len(plans),
        "batch_id": first["batch_id"],
        "generator_agent": first["generator_agent"],
        "prompt_hash": first["prompt_hash"],
        "seed_range": [min(plan["seed"] for plan in plans), max(plan["seed"] for plan in plans)],
        "plans_sha256": hashlib.sha256(plans_path.read_bytes()).hexdigest(),
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return batch


def test_validates_batches_and_returns_deterministic_redacted_summary(tmp_path: Path) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    prompt_hash = "sha256:" + "a" * 64
    _write_batch(
        root,
        "generator_beta",
        [_plan(seed=20, batch_id="batch_beta", generator_agent="agent_beta", prompt_hash=prompt_hash)],
    )
    _write_batch(
        root,
        "generator_alpha",
        [_plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash=prompt_hash)],
    )

    summary = validate_seed_batches(root)

    assert summary == {
        "batch_count": 2,
        "batches": [
            {"batch_id": "batch_alpha", "generator_agent": "agent_alpha", "record_count": 1, "seed_range": [10, 10]},
            {"batch_id": "batch_beta", "generator_agent": "agent_beta", "record_count": 1, "seed_range": [20, 20]},
        ],
        "record_count": 2,
    }
    assert json.dumps(summary, sort_keys=True, separators=(",", ":")) == json.dumps(
        validate_seed_batches(root), sort_keys=True, separators=(",", ":")
    )


@pytest.mark.parametrize(
    "manifest_field", ["record_count", "batch_id", "generator_agent", "prompt_hash", "seed_range", "plans_sha256"]
)
def test_rejects_manifest_mismatch(tmp_path: Path, manifest_field: str) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    batch = _write_batch(
        root,
        "generator_alpha",
        [_plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash="sha256:" + "a" * 64)],
    )
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacements = {
        "record_count": 2,
        "batch_id": "batch_other",
        "generator_agent": "agent_other",
        "prompt_hash": "sha256:" + "b" * 64,
        "seed_range": [0, 1],
        "plans_sha256": "0" * 64,
    }
    manifest[manifest_field] = replacements[manifest_field]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SeedBatchValidationError, match=manifest_field):
        validate_seed_batches(root)


def test_rejects_cross_batch_duplicate_seed_and_seed_id(tmp_path: Path) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    prompt_hash = "sha256:" + "a" * 64
    original = _plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash=prompt_hash)
    duplicate = deepcopy(original)
    duplicate["batch_id"] = "batch_beta"
    duplicate["generator_agent"] = "agent_beta"
    _write_batch(root, "generator_alpha", [original])
    _write_batch(root, "generator_beta", [duplicate])

    with pytest.raises(SeedBatchValidationError, match="duplicate seed_id"):
        validate_seed_batches(root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan["semantic_plan"]["entities"][0].update({"fictional": False}), "fictional"),
        (lambda plan: plan.update({"private_data": True}), "schema"),
        (lambda plan: plan.update({"target": "forbidden"}), "forbidden field"),
    ],
)
def test_rejects_nonfictional_private_and_forbidden_plan_fields(tmp_path: Path, mutate: object, message: str) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    plan = _plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash="sha256:" + "a" * 64)
    mutate(plan)  # type: ignore[operator]
    _write_batch(root, "generator_alpha", [plan])

    with pytest.raises(SeedBatchValidationError, match=message):
        validate_seed_batches(root)


def test_rejects_prompt_hash_that_changes_within_a_batch(tmp_path: Path) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    first = _plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash="sha256:" + "a" * 64)
    second = _plan(seed=11, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash="sha256:" + "b" * 64)
    _write_batch(root, "generator_alpha", [first, second])

    with pytest.raises(SeedBatchValidationError, match="prompt_hash"):
        validate_seed_batches(root)


def test_cli_writes_the_redacted_summary_for_an_explicit_root(tmp_path: Path) -> None:
    root = tmp_path / "seeds"
    root.mkdir()
    _write_batch(
        root,
        "generator_alpha",
        [_plan(seed=10, batch_id="batch_alpha", generator_agent="agent_alpha", prompt_hash="sha256:" + "a" * 64)],
    )
    output = tmp_path / "summary.json"
    script = Path(__file__).parents[1] / "scripts" / "validate_seed_batches.py"

    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["record_count"] == 1

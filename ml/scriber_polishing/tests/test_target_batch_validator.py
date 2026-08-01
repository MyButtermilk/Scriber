"""Tests for the fail-closed canonical target batch validation boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.target_batch_validator import TargetBatchValidationError, validate_target_batches


def _canonical_record(
    seed: str, writer: str = "target_writer_alpha", generator: str = "generator_alpha"
) -> dict[str, object]:
    fixture_path = (
        Path(__file__).parents[1] / "data" / "targets" / "target_writer_business_technical_01" / "targets.jsonl"
    )
    record = json.loads(fixture_path.read_text(encoding="utf-8").splitlines()[0])
    record["canonical_document_id"] = f"canonical_{seed}"
    record["seed_id"] = seed
    record["target_writer_agent"] = writer
    record["plan_generator_agent"] = generator
    record["source_plan_batch"] = "source_batch_alpha"
    record["target_prompt_hash"] = "sha256:" + "a" * 64
    return record


def _write_batch(root: Path, name: str, records: list[dict[str, object]]) -> Path:
    batch = root / name
    batch.mkdir()
    targets_path = batch / "targets.jsonl"
    targets_path.write_bytes(b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records))
    source_path = root.parents[1] / "data" / "seeds" / "generator_alpha" / "plans.jsonl"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b'{"seed":"fixture"}\n')
    first = records[0]
    manifest = {
        "manifest_version": 1,
        "writer_id": first["target_writer_agent"],
        "target_prompt_hash": first["target_prompt_hash"],
        "assigned_input_batches": [
            {
                "path": "data/seeds/generator_alpha/plans.jsonl",
                "sha256": "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
        ],
        "record_count": len(records),
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in records}),
        "unique_seed_id_count": len({record["seed_id"] for record in records}),
        "targets_sha256": "sha256:" + hashlib.sha256(targets_path.read_bytes()).hexdigest(),
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return batch


def test_validates_target_batches_and_returns_a_deterministic_redacted_summary(tmp_path: Path) -> None:
    root = tmp_path / "data" / "targets"
    root.mkdir(parents=True)
    _write_batch(root, "target_writer_beta", [_canonical_record("seed_beta")])
    _write_batch(root, "target_writer_alpha", [_canonical_record("seed_alpha")])

    summary = validate_target_batches(root)

    assert summary == {
        "batch_count": 2,
        "batches": [
            {"record_count": 1, "target_prompt_hash": "sha256:" + "a" * 64, "writer_id": "target_writer_alpha"},
            {"record_count": 1, "target_prompt_hash": "sha256:" + "a" * 64, "writer_id": "target_writer_alpha"},
        ],
        "record_count": 2,
    }
    assert json.dumps(summary, sort_keys=True, separators=(",", ":")) == json.dumps(
        validate_target_batches(root), sort_keys=True, separators=(",", ":")
    )


@pytest.mark.parametrize(
    "field",
    ["writer_id", "target_prompt_hash", "assigned_input_batches", "record_count", "targets_sha256"],
)
def test_rejects_manifest_evidence_mismatches(tmp_path: Path, field: str) -> None:
    root = tmp_path / "data" / "targets"
    root.mkdir(parents=True)
    batch = _write_batch(root, "target_writer_alpha", [_canonical_record("seed_alpha")])
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacements = {
        "writer_id": "target_writer_other",
        "target_prompt_hash": "sha256:" + "b" * 64,
        "assigned_input_batches": [{"path": "data/seeds/generator_alpha/plans.jsonl", "sha256": "sha256:" + "0" * 64}],
        "record_count": 2,
        "targets_sha256": "sha256:" + "0" * 64,
    }
    manifest[field] = replacements[field]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(TargetBatchValidationError, match=field):
        validate_target_batches(root)


def test_rejects_duplicate_canonical_and_seed_ids_across_batches(tmp_path: Path) -> None:
    root = tmp_path / "data" / "targets"
    root.mkdir(parents=True)
    _write_batch(root, "target_writer_alpha", [_canonical_record("seed_duplicate")])
    _write_batch(root, "target_writer_beta", [_canonical_record("seed_duplicate", writer="target_writer_beta")])

    with pytest.raises(TargetBatchValidationError, match="duplicate canonical_document_id"):
        validate_target_batches(root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda record: record.update({"source_text": "different"}), "source_text"),
        (lambda record: record.update({"target_writer_agent": record["plan_generator_agent"]}), "must differ"),
        (lambda record: record.update({"critic": {"verdict": "pass"}}), "forbidden field"),
        (lambda record: record["target_ast"].update({"model_output": "raw"}), "forbidden field"),
        (lambda record: record.update({"private_data": False}), "private-data marker"),
    ],
)
def test_rejects_invalid_identity_and_forbidden_target_content(tmp_path: Path, mutate: object, message: str) -> None:
    root = tmp_path / "data" / "targets"
    root.mkdir(parents=True)
    record = _canonical_record("seed_alpha")
    mutate(record)  # type: ignore[operator]
    _write_batch(root, "target_writer_alpha", [record])

    with pytest.raises(TargetBatchValidationError, match=message):
        validate_target_batches(root)


def test_cli_writes_redacted_summary_for_explicit_root(tmp_path: Path) -> None:
    root = tmp_path / "data" / "targets"
    root.mkdir(parents=True)
    _write_batch(root, "target_writer_alpha", [_canonical_record("seed_alpha")])
    output = tmp_path / "summary.json"
    script = Path(__file__).parents[1] / "scripts" / "validate_target_batches.py"

    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["record_count"] == 1

"""Fail-closed validation for repository-local canonical target batches.

The primary manifest names mirror the target-writer handoff: ``writer_id``,
``target_prompt_hash``, ``assigned_input_batches``, ``record_count``, and
``targets_sha256``.  The unambiguous legacy aliases ``target_writer_agent``,
``prompt_hash``, and ``input_batches`` are accepted for the first three names.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scriber_polishing.validators import validate_canonical_record

_MANIFEST_FIELD_ALIASES = {
    "writer_id": ("writer_id", "target_writer_agent"),
    "target_prompt_hash": ("target_prompt_hash", "prompt_hash"),
    "assigned_input_batches": ("assigned_input_batches", "input_batches"),
    "record_count": ("record_count",),
    "targets_sha256": ("targets_sha256",),
}
_OPTIONAL_COUNT_FIELDS = ("unique_canonical_document_id_count", "unique_seed_id_count")
_FORBIDDEN_RECORD_FIELDS = frozenset(
    {
        "critic",
        "critics",
        "critic_verdict",
        "split",
        "splits",
        "corruption",
        "corruptions",
        "model_output",
        "model_outputs",
        "private_data",
        "private-data",
    }
)


class TargetBatchValidationError(ValueError):
    """Raised when one or more target batches violate the generation boundary."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("target batch validation failed:\n" + "\n".join(f"- {error}" for error in self.errors))


@dataclass(frozen=True, slots=True)
class _BatchSummary:
    writer_id: str
    target_prompt_hash: str
    record_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "record_count": self.record_count,
            "target_prompt_hash": self.target_prompt_hash,
            "writer_id": self.writer_id,
        }


def validate_target_batches(root: str | Path) -> dict[str, object]:
    """Validate every ``target_writer_*`` batch below *root* without exposing targets."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise TargetBatchValidationError([f"root is not a directory: {root_path}"])
    batch_dirs = sorted(path for path in root_path.glob("target_writer_*") if path.is_dir())
    if not batch_dirs:
        raise TargetBatchValidationError([f"no target_writer_* batch directories under {root_path}"])

    errors: list[str] = []
    known_document_ids: set[str] = set()
    known_seed_ids: set[str] = set()
    summaries: list[_BatchSummary] = []
    project_root = root_path.parent.parent
    for batch_dir in batch_dirs:
        summary, batch_errors = _validate_batch(batch_dir, project_root, known_document_ids, known_seed_ids)
        errors.extend(batch_errors)
        if summary is not None:
            summaries.append(summary)
    if errors:
        raise TargetBatchValidationError(errors)
    return {
        "batch_count": len(summaries),
        "batches": [summary.as_dict() for summary in summaries],
        "record_count": sum(summary.record_count for summary in summaries),
    }


def _validate_batch(
    batch_dir: Path,
    project_root: Path,
    known_document_ids: set[str],
    known_seed_ids: set[str],
) -> tuple[_BatchSummary | None, list[str]]:
    errors: list[str] = []
    label = batch_dir.name
    manifest = _load_json_object(batch_dir / "manifest.json", label, "manifest", errors)
    targets_path = batch_dir / "targets.jsonl"
    try:
        targets_bytes = targets_path.read_bytes()
    except OSError as error:
        errors.append(f"{label}: cannot read targets.jsonl ({error.__class__.__name__})")
        return None, errors
    records = _parse_jsonl(targets_bytes, label, errors)
    valid_records: list[dict[str, Any]] = []
    for line_number, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            errors.append(f"{label}: line {line_number} must be a JSON object")
            continue
        forbidden_field = _find_forbidden_field(record)
        if forbidden_field is not None:
            marker = (
                "private-data marker"
                if forbidden_field.rsplit(".", 1)[-1] in {"private_data", "private-data"}
                else "forbidden field"
            )
            errors.append(f"{label}: line {line_number} has {marker} {forbidden_field}")
            continue
        try:
            canonical = validate_canonical_record(record)
        except ValueError as error:
            errors.append(f"{label}: line {line_number} failed canonical validation ({error})")
            continue
        if canonical["source_text"] != canonical["target_plain_text"]:
            errors.append(f"{label}: line {line_number} source_text must equal target_plain_text")
            continue
        if canonical["target_writer_agent"] == canonical["plan_generator_agent"]:
            errors.append(f"{label}: line {line_number} target_writer_agent must differ from plan_generator_agent")
            continue
        valid_records.append(canonical)

    if not valid_records or len(valid_records) != len(records):
        if not records:
            errors.append(f"{label}: targets.jsonl must contain at least one record")
        return None, errors
    writer_id = _single_value(valid_records, "target_writer_agent", label, errors)
    prompt_hash = _single_value(valid_records, "target_prompt_hash", label, errors)
    for record in valid_records:
        document_id = record["canonical_document_id"]
        seed_id = record["seed_id"]
        if document_id in known_document_ids:
            errors.append(f"{label}: duplicate canonical_document_id across batches")
        known_document_ids.add(document_id)
        if seed_id in known_seed_ids:
            errors.append(f"{label}: duplicate seed_id across batches")
        known_seed_ids.add(seed_id)

    if manifest is None:
        return None, errors
    manifest_values = _manifest_values(manifest, label, errors)
    expected = {
        "writer_id": writer_id,
        "target_prompt_hash": prompt_hash,
        "record_count": len(valid_records),
        "targets_sha256": "sha256:" + hashlib.sha256(targets_bytes).hexdigest(),
    }
    for field, expected_value in expected.items():
        if manifest_values.get(field) != expected_value:
            errors.append(f"{label}: manifest {field} does not match targets.jsonl")
    _validate_input_hashes(manifest_values.get("assigned_input_batches"), project_root, label, errors)
    optional_expected = {
        "unique_canonical_document_id_count": len({record["canonical_document_id"] for record in valid_records}),
        "unique_seed_id_count": len({record["seed_id"] for record in valid_records}),
    }
    for field in _OPTIONAL_COUNT_FIELDS:
        if field in manifest and manifest[field] != optional_expected[field]:
            errors.append(f"{label}: manifest {field} does not match targets.jsonl")
    if errors:
        return None, errors
    return _BatchSummary(writer_id, prompt_hash, len(valid_records)), errors


def _manifest_values(manifest: Mapping[str, Any], label: str, errors: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field, aliases in _MANIFEST_FIELD_ALIASES.items():
        present = [alias for alias in aliases if alias in manifest]
        if not present:
            errors.append(f"{label}: manifest missing required field {field}")
            continue
        values[field] = manifest[present[0]]
        if len(present) > 1 and any(manifest[alias] != values[field] for alias in present[1:]):
            errors.append(f"{label}: manifest aliases for {field} disagree")
    return values


def _validate_input_hashes(value: object, project_root: Path, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label}: manifest assigned_input_batches must be a non-empty list")
        return
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            errors.append(f"{label}: manifest assigned_input_batches[{index}] must be an object")
            continue
        path_value = item.get("path")
        expected_hash = item.get("sha256", item.get("hash"))
        if not isinstance(path_value, str) or not isinstance(expected_hash, str):
            errors.append(f"{label}: manifest assigned_input_batches[{index}] needs path and sha256")
            continue
        candidate = Path(path_value)
        if candidate.is_absolute() or ".." in candidate.parts:
            errors.append(f"{label}: manifest assigned_input_batches[{index}] path is not repository-relative")
            continue
        input_path = (project_root / candidate).resolve()
        try:
            input_path.relative_to(project_root.resolve())
        except ValueError:
            errors.append(f"{label}: manifest assigned_input_batches[{index}] escapes project root")
            continue
        try:
            actual_hash = "sha256:" + hashlib.sha256(input_path.read_bytes()).hexdigest()
        except OSError as error:
            errors.append(f"{label}: cannot read assigned input batch {index} ({error.__class__.__name__})")
            continue
        if expected_hash != actual_hash:
            errors.append(f"{label}: manifest assigned_input_batches[{index}] sha256 does not match input bytes")


def _load_json_object(path: Path, label: str, kind: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"{label}: invalid {kind}.json ({error.__class__.__name__})")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label}: {kind}.json must be a JSON object")
        return None
    return value


def _parse_jsonl(targets_bytes: bytes, label: str, errors: list[str]) -> list[object]:
    records: list[object] = []
    for line_number, line in enumerate(targets_bytes.splitlines(), start=1):
        if not line.strip():
            errors.append(f"{label}: line {line_number} must not be blank")
            continue
        try:
            records.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{label}: line {line_number} is not valid JSON ({error.__class__.__name__})")
    return records


def _find_forbidden_field(value: object, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            key_text = str(key)
            current_path = f"{path}.{key_text}" if path else key_text
            if key_text.casefold() in _FORBIDDEN_RECORD_FIELDS:
                return current_path
            nested = _find_forbidden_field(nested_value, current_path)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            nested = _find_forbidden_field(nested_value, f"{path}[{index}]")
            if nested is not None:
                return nested
    return None


def _single_value(records: Sequence[Mapping[str, Any]], field: str, label: str, errors: list[str]) -> Any:
    values = {record[field] for record in records}
    if len(values) != 1:
        errors.append(f"{label}: {field} must be constant within a batch")
    return records[0][field]

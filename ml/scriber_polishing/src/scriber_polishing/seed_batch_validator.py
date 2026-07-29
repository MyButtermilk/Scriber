"""Fail-closed validation for repository-local AI seed plan batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scriber_polishing.schemas import validate_seed_plan

_FORBIDDEN_PLAN_FIELDS = frozenset({"target", "targets", "split", "splits", "critic", "critics"})
_MANIFEST_FIELDS = ("record_count", "batch_id", "generator_agent", "prompt_hash", "seed_range", "plans_sha256")


class SeedBatchValidationError(ValueError):
    """Raised when one or more seed batches violate the generation boundary."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("seed batch validation failed:\n" + "\n".join(f"- {error}" for error in self.errors))


@dataclass(frozen=True, slots=True)
class _BatchSummary:
    batch_id: str
    generator_agent: str
    record_count: int
    seed_range: list[int]

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "generator_agent": self.generator_agent,
            "record_count": self.record_count,
            "seed_range": self.seed_range,
        }


def validate_seed_batches(root: str | Path) -> dict[str, object]:
    """Validate every ``generator_*`` batch below *root* without exposing plan data."""
    root_path = Path(root)
    errors: list[str] = []
    if not root_path.is_dir():
        raise SeedBatchValidationError([f"root is not a directory: {root_path}"])

    batch_dirs = sorted(path for path in root_path.glob("generator_*") if path.is_dir())
    if not batch_dirs:
        raise SeedBatchValidationError([f"no generator_* batch directories under {root_path}"])

    seed_ids: set[str] = set()
    seeds: set[int] = set()
    summaries: list[_BatchSummary] = []
    for batch_dir in batch_dirs:
        summary, batch_errors = _validate_batch(batch_dir, seed_ids, seeds)
        errors.extend(batch_errors)
        if summary is not None:
            summaries.append(summary)

    if errors:
        raise SeedBatchValidationError(errors)
    return {
        "batch_count": len(summaries),
        "batches": [summary.as_dict() for summary in summaries],
        "record_count": sum(summary.record_count for summary in summaries),
    }


def _validate_batch(
    batch_dir: Path, known_seed_ids: set[str], known_seeds: set[int]
) -> tuple[_BatchSummary | None, list[str]]:
    errors: list[str] = []
    label = batch_dir.name
    manifest = _load_json_object(batch_dir / "manifest.json", label, "manifest", errors)
    plans_path = batch_dir / "plans.jsonl"
    try:
        plans_bytes = plans_path.read_bytes()
    except OSError as error:
        errors.append(f"{label}: cannot read plans.jsonl ({error.__class__.__name__})")
        return None, errors

    plans = _parse_jsonl(plans_bytes, label, errors)
    valid_plans: list[dict[str, Any]] = []
    for line_number, plan in enumerate(plans, start=1):
        if not isinstance(plan, Mapping):
            errors.append(f"{label}: line {line_number} must be a JSON object")
            continue
        forbidden_field = _find_forbidden_field(plan)
        if forbidden_field is not None:
            errors.append(f"{label}: line {line_number} has forbidden field {forbidden_field}")
            continue
        try:
            valid_plans.append(validate_seed_plan(plan))
        except ValueError as error:
            errors.append(f"{label}: line {line_number} failed schema validation ({error})")

    if not valid_plans or len(valid_plans) != len(plans):
        if not valid_plans and not plans:
            errors.append(f"{label}: plans.jsonl must contain at least one record")
        return None, errors

    nonfictional_lines: list[int] = []
    for line_number, plan in enumerate(valid_plans, start=1):
        if plan["source_class"] != "ai_generated":
            errors.append(f"{label}: line {line_number} source_class must be ai_generated")
        if plan["private_data"] is not False:
            errors.append(f"{label}: line {line_number} private_data must be false")
        entities = plan["semantic_plan"]["entities"]
        if any(entity["fictional"] is not True for entity in entities):
            nonfictional_lines.append(line_number)
    if nonfictional_lines:
        errors.append(
            f"{label}: {len(nonfictional_lines)} record(s) have non-fictional entities "
            f"(first line {nonfictional_lines[0]})"
        )

    batch_id = _single_value(valid_plans, "batch_id", label, errors)
    generator_agent = _single_value(valid_plans, "generator_agent", label, errors)
    prompt_hash = _single_value(valid_plans, "prompt_hash", label, errors)
    seed_values = [plan["seed"] for plan in valid_plans]
    for plan in valid_plans:
        seed_id = plan["seed_id"]
        seed = plan["seed"]
        if seed_id in known_seed_ids:
            errors.append(f"{label}: duplicate seed_id across batches")
        known_seed_ids.add(seed_id)
        if seed in known_seeds:
            errors.append(f"{label}: duplicate seed across batches")
        known_seeds.add(seed)

    if manifest is None:
        return None, errors
    missing = [field for field in _MANIFEST_FIELDS if field not in manifest]
    if missing:
        errors.append(f"{label}: manifest missing required field {missing[0]}")
        return None, errors
    expected = {
        "record_count": len(valid_plans),
        "batch_id": batch_id,
        "generator_agent": generator_agent,
        "prompt_hash": prompt_hash,
        "seed_range": [min(seed_values), max(seed_values)],
        "plans_sha256": hashlib.sha256(plans_bytes).hexdigest(),
    }
    for field in _MANIFEST_FIELDS:
        if manifest[field] != expected[field]:
            errors.append(f"{label}: manifest {field} does not match plans.jsonl")

    if errors:
        return None, errors
    return _BatchSummary(batch_id, generator_agent, len(valid_plans), expected["seed_range"]), errors


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


def _parse_jsonl(plans_bytes: bytes, label: str, errors: list[str]) -> list[object]:
    plans: list[object] = []
    for line_number, line in enumerate(plans_bytes.splitlines(), start=1):
        if not line.strip():
            errors.append(f"{label}: line {line_number} must not be blank")
            continue
        try:
            plans.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"{label}: line {line_number} is not valid JSON ({error.__class__.__name__})")
    return plans


def _find_forbidden_field(value: object, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            key_text = str(key)
            current_path = f"{path}.{key_text}" if path else key_text
            if key_text.casefold() in _FORBIDDEN_PLAN_FIELDS:
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


def _single_value(plans: Sequence[Mapping[str, Any]], field: str, label: str, errors: list[str]) -> Any:
    values = {plan[field] for plan in plans}
    if len(values) != 1:
        errors.append(f"{label}: {field} must be constant within a batch")
    return plans[0][field]

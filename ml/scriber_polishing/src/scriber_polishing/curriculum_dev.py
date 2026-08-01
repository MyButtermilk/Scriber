"""Balanced, validation-only development references for curriculum decisions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import (
    PHASE_RULES_VERSION,
    PHASES,
    classify_curriculum_phase,
    hash_identifier_sequence,
    validate_curriculum_plan,
    write_immutable_json,
)
from .evaluation_reference_builder import (
    build_evaluation_references,
    write_evaluation_references,
)

SELECTION_ALGORITHM = "phase_stratified_seeded_sha256_bottom_k_v1"

_PROJECT_ROOT = Path(__file__).parents[2]
_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "curriculum_dev_manifest_schema.json"


def _rank(seed: int, purpose: str, phase: int, example_id: str) -> bytes:
    return hashlib.sha256(
        f"curriculum-dev-v1:{seed}:{purpose}:{phase}:{example_id}".encode()
    ).digest()


def exclusion_ids_from_manifest(value: Mapping[str, Any]) -> frozenset[str]:
    """Read only identifier metadata from a prior closed dev manifest."""

    validated = validate_curriculum_dev_manifest(value)
    return frozenset(validated["selected_example_ids"])


def exclusion_ids_from_plan(value: Mapping[str, Any]) -> frozenset[str]:
    """Expose assignment IDs for explicit overlap checks without opening text."""

    validated = validate_curriculum_plan(value)
    return frozenset(item["example_id"] for item in validated["assignments"])


def build_curriculum_dev_references(
    records: Sequence[Mapping[str, Any]],
    *,
    source_binding: Mapping[str, object],
    ai_gold_ids: frozenset[str],
    ai_gold_binding: Mapping[str, object],
    purpose: str,
    per_phase: int,
    seed: int,
    excluded_example_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select equal per-phase validation rows and convert them to regression refs."""

    if purpose not in {"curriculum", "checkpoint_selection"}:
        raise ValueError("development purpose must be curriculum or checkpoint_selection")
    if isinstance(per_phase, bool) or not isinstance(per_phase, int) or per_phase <= 0:
        raise ValueError("per_phase must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("development seed must be a non-negative integer")
    if not records:
        raise ValueError("development source records are empty")
    if not ai_gold_ids:
        raise ValueError("development AI-Gold train membership is empty")
    if ai_gold_binding.get("record_count") != len(ai_gold_ids):
        raise ValueError("development AI-Gold binding differs from membership IDs")
    source = dict(source_binding)
    if source.get("split") != "validation" or source.get("record_count") != len(records):
        raise ValueError("development source binding differs from validation records")

    by_phase: dict[int, list[Mapping[str, Any]]] = {phase: [] for phase in PHASES}
    seen: set[str] = set()
    matched_ai_gold = 0
    for record in records:
        if record.get("split") != "validation":
            raise ValueError("development references may contain only split=validation inputs")
        example_id = record.get("example_id")
        if not isinstance(example_id, str) or not example_id:
            raise ValueError("development record requires example_id")
        if example_id in seen:
            raise ValueError(f"duplicate development example_id: {example_id}")
        seen.add(example_id)
        if example_id in ai_gold_ids:
            matched_ai_gold += 1
        if example_id in excluded_example_ids:
            continue
        phase = classify_curriculum_phase(record, ai_gold_ids=ai_gold_ids)
        by_phase[phase].append(record)
    if matched_ai_gold:
        raise ValueError("validation source overlaps AI-Gold train membership")

    selected: list[Mapping[str, Any]] = []
    for phase in PHASES:
        candidates = sorted(
            by_phase[phase],
            key=lambda record: (
                _rank(seed, purpose, phase, str(record["example_id"])),
                str(record["example_id"]),
            ),
        )
        if len(candidates) < per_phase:
            raise ValueError(
                f"insufficient development phase {phase}: required {per_phase}, found {len(candidates)}"
            )
        selected.extend(candidates[:per_phase])

    retagged: list[dict[str, Any]] = []
    for record in selected:
        phase = classify_curriculum_phase(record, ai_gold_ids=ai_gold_ids)
        materialized = dict(record)
        materialized["split"] = "regression"
        risk_tags = list(materialized.get("risk_tags", ()))
        phase_tag = f"curriculum_phase_{phase}"
        if phase_tag not in risk_tags:
            risk_tags.append(phase_tag)
        materialized["risk_tags"] = risk_tags
        retagged.append(materialized)
    references = build_evaluation_references(retagged, output_split="regression")
    selected_ids = sorted(str(record["example_id"]) for record in selected)
    selected_counts = Counter(
        classify_curriculum_phase(record, ai_gold_ids=ai_gold_ids) for record in selected
    )
    ai_gold = {
        "filename": ai_gold_binding.get("filename"),
        "sha256": ai_gold_binding.get("sha256"),
        "record_count": ai_gold_binding.get("record_count"),
        "matched_source_records": 0,
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_curriculum_development_references",
        "purpose": purpose,
        "algorithm": SELECTION_ALGORITHM,
        "phase_rules_version": PHASE_RULES_VERSION,
        "seed": seed,
        "per_phase": per_phase,
        "source": source,
        "ai_gold_train": ai_gold,
        "exclusions": {
            "count": len(excluded_example_ids),
            "example_ids_sha256": hash_identifier_sequence(sorted(excluded_example_ids)),
        },
        "phase_counts": {str(phase): selected_counts[phase] for phase in PHASES},
        "selected_example_ids": selected_ids,
        "selected_example_ids_sha256": hash_identifier_sequence(selected_ids),
        "references": {
            "filename": "pending",
            "sha256": "0" * 64,
            "record_count": len(references),
            "split": "regression",
        },
        "parent_split_leakage": False,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "holdout_inputs": False,
    }
    return references, manifest


@cache
def _manifest_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8")))


def validate_curriculum_dev_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed manifest and all derivable counts/hashes."""

    materialized = json.loads(json.dumps(value, ensure_ascii=False))
    errors = sorted(_manifest_validator().iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"curriculum dev manifest schema failed: {errors[0].message}")
    selected = materialized["selected_example_ids"]
    if selected != sorted(selected):
        raise ValueError("curriculum dev selected IDs must be sorted")
    if len(selected) != materialized["per_phase"] * len(PHASES):
        raise ValueError("curriculum dev selected count differs from per_phase")
    if materialized["references"]["record_count"] != len(selected):
        raise ValueError("curriculum dev reference count differs from selected IDs")
    if materialized["selected_example_ids_sha256"] != hash_identifier_sequence(selected):
        raise ValueError("curriculum dev selected ID SHA-256 mismatch")
    expected_counts = {str(phase): materialized["per_phase"] for phase in PHASES}
    if materialized["phase_counts"] != expected_counts:
        raise ValueError("curriculum dev phase counts are not balanced")
    return materialized


def write_curriculum_dev_references(
    references: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    references_destination: str | Path,
    manifest_destination: str | Path,
) -> dict[str, Any]:
    """Write immutable references, bind their exact bytes, then seal the manifest."""

    reference_binding = write_evaluation_references(references, references_destination)
    completed = json.loads(json.dumps(manifest, ensure_ascii=False))
    completed["references"] = {
        "filename": Path(references_destination).name,
        "sha256": reference_binding["records_sha256"],
        "record_count": reference_binding["record_count"],
        "split": "regression",
    }
    validated = validate_curriculum_dev_manifest(completed)
    write_immutable_json(
        manifest_destination,
        validated,
        label="curriculum development manifest",
    )
    return validated


def load_curriculum_dev_manifest(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"curriculum dev manifest must be a regular file: {candidate}")
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ValueError(f"curriculum dev manifest must be a regular file: {candidate}")
    try:
        before = candidate.stat()
        payload = candidate.read_bytes()
        after = candidate.stat()
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("curriculum dev manifest changed while it was read")
        if expected_sha256 is not None:
            if (
                len(expected_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha256
                )
            ):
                raise ValueError(
                    "curriculum dev manifest expected SHA-256 must be lowercase hexadecimal"
                )
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    "curriculum dev manifest SHA-256 mismatch: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"curriculum dev manifest is invalid JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("curriculum dev manifest must contain an object")
    return validate_curriculum_dev_manifest(value)

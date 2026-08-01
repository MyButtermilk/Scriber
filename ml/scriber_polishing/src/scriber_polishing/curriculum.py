"""Fail-closed curriculum phase assignment and immutable order plans."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PHASES = (1, 2, 3, 4, 5)
PHASE_RULES_VERSION = "scriber_curriculum_phase_v1"
ORDER_ALGORITHM = "phase_grouped_seeded_sha256_v1"

_PROJECT_ROOT = Path(__file__).parents[2]
_PLAN_SCHEMA = _PROJECT_ROOT / "contracts" / "curriculum_plan_schema.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]+$")
_PHASE_ONE_PROFILE = re.compile(r"^(?:identity|minimal_punctuation.*|punctuation_case)$")
_PHASE_TWO_PROFILE = re.compile(
    r"(?:^grammar$|abandoned|repetition_self_correction|context_spelling|coverage_.*(?:filler|grammar|spelling))"
)
_PHASE_THREE_PROFILE = re.compile(r"^spoken_formatting")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one artifact deterministically with a single trailing LF."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def hash_identifier_sequence(values: Sequence[str]) -> str:
    """Hash an ordered identifier sequence without ambiguous line framing."""

    return sha256_bytes(canonical_json_bytes(list(values)))


def write_immutable_bytes(path: str | Path, payload: bytes, *, label: str) -> Path:
    """Write once, allowing only byte-identical replay."""

    destination = Path(path)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different {label}: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        raise ValueError(f"stale temporary {label}: {temporary}")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


def write_immutable_json(path: str | Path, value: object, *, label: str) -> Path:
    return write_immutable_bytes(path, canonical_json_bytes(value), label=label)


def _checked_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _stable_regular_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError(f"{label} changed while it was being read")
    return resolved, payload


def load_hash_bound_jsonl(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_split: str,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    """Load exact JSONL bytes and require one declared non-holdout split."""

    expected = _checked_sha256(expected_sha256, f"{label} expected SHA-256")
    if expected_split not in {"train", "validation"}:
        raise ValueError("curriculum inputs may only use train or validation splits")
    resolved, payload = _stable_regular_bytes(path, label)
    actual = sha256_bytes(payload)
    if actual != expected:
        raise ValueError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ValueError(f"blank {label} JSONL line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {label} JSON at line {line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} JSONL row {line_number} must be an object")
        example_id = _required_identifier(value, "example_id")
        if example_id in seen:
            raise ValueError(f"duplicate {label} example_id: {example_id}")
        seen.add(example_id)
        if value.get("split") != expected_split:
            raise ValueError(
                f"{label} record {example_id} declares split {value.get('split')!r}, "
                f"expected {expected_split!r}"
            )
        _require_ai_only_provenance(value, label=label)
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} JSONL is empty")
    return rows, {
        "filename": resolved.name,
        "sha256": actual,
        "record_count": len(rows),
        "split": expected_split,
    }


def _required_identifier(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not 3 <= len(value) <= 300 or not _EXAMPLE_ID.fullmatch(value):
        raise ValueError(f"curriculum record requires a valid {field}")
    return value


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"curriculum record requires non-empty {field}")
    return value


def _require_ai_only_provenance(record: Mapping[str, Any], *, label: str) -> None:
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError(f"{label} record requires provenance")
    if provenance.get("synthetic_only") is not True:
        raise ValueError(f"{label} record must declare synthetic_only provenance")
    if provenance.get("human_curation") is not False:
        raise ValueError(f"{label} record must declare human_curation=false")
    if provenance.get("generative_api") is not False:
        raise ValueError(f"{label} record must declare generative_api=false")


def ai_gold_train_ids(
    rows: Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Return the exact unique IDs from an intended training-only AI-Gold split."""

    result: set[str] = set()
    for row in rows:
        if row.get("split") != "train":
            raise ValueError("AI-Gold membership input may contain only split=train")
        example_id = _required_identifier(row, "example_id")
        if example_id in result:
            raise ValueError(f"duplicate AI-Gold train example_id: {example_id}")
        result.add(example_id)
    if not result:
        raise ValueError("AI-Gold train membership is empty")
    return frozenset(result)


def classify_curriculum_phase(
    record: Mapping[str, Any],
    *,
    ai_gold_ids: frozenset[str] = frozenset(),
) -> int:
    """Assign one exclusive phase with hard-negative/AI-Gold precedence."""

    example_id = _required_identifier(record, "example_id")
    domain = _required_text(record, "domain")
    profile = _required_text(record, "corruption_profile")
    if example_id in ai_gold_ids or domain == "hard_negatives":
        return 5
    if _PHASE_ONE_PROFILE.search(profile):
        return 1
    if _PHASE_TWO_PROFILE.search(profile):
        return 2
    if _PHASE_THREE_PROFILE.search(profile):
        return 3
    return 4


def _rank(seed: int, epoch: int, example_id: str) -> bytes:
    return hashlib.sha256(f"curriculum-order-v1:{seed}:{epoch}:{example_id}".encode()).digest()


def _ordered_ids(
    assignments: Mapping[str, int],
    *,
    mode: str,
    seed: int,
    epoch: int,
) -> list[str]:
    globally_ranked = sorted(assignments, key=lambda value: (_rank(seed, epoch, value), value))
    if mode == "shuffled":
        return globally_ranked
    if mode == "staged":
        return [
            example_id
            for phase in PHASES
            for example_id in globally_ranked
            if assignments[example_id] == phase
        ]
    raise ValueError(f"unsupported curriculum mode: {mode}")


def build_curriculum_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    source_binding: Mapping[str, object],
    ai_gold_ids: frozenset[str],
    ai_gold_binding: Mapping[str, object],
    mode: str,
    epochs: int,
    seed: int,
) -> dict[str, Any]:
    """Build exact matched shuffled/staged epoch permutations."""

    if mode not in {"shuffled", "staged"}:
        raise ValueError("curriculum mode must be shuffled or staged")
    if isinstance(epochs, bool) or not isinstance(epochs, int) or not 1 <= epochs <= 10:
        raise ValueError("curriculum epochs must be an integer from 1 to 10")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("curriculum seed must be a non-negative integer")
    if not records:
        raise ValueError("curriculum source records are empty")
    if not ai_gold_ids:
        raise ValueError("curriculum AI-Gold train membership is empty")
    if ai_gold_binding.get("record_count") != len(ai_gold_ids):
        raise ValueError("curriculum AI-Gold binding differs from membership IDs")

    assignments: dict[str, int] = {}
    for record in records:
        if record.get("split") != "train":
            raise ValueError("curriculum order plans may contain only split=train")
        example_id = _required_identifier(record, "example_id")
        if example_id in assignments:
            raise ValueError(f"duplicate curriculum example_id: {example_id}")
        assignments[example_id] = classify_curriculum_phase(record, ai_gold_ids=ai_gold_ids)
    phase_counts = Counter(assignments.values())
    missing_phases = [phase for phase in PHASES if phase_counts[phase] == 0]
    if missing_phases:
        raise ValueError(f"curriculum source does not cover phase {missing_phases[0]}")

    source = dict(source_binding)
    if source.get("split") != "train" or source.get("record_count") != len(records):
        raise ValueError("curriculum source binding differs from records")
    ai_gold = {
        "filename": ai_gold_binding.get("filename"),
        "sha256": ai_gold_binding.get("sha256"),
        "record_count": ai_gold_binding.get("record_count"),
        "matched_source_records": sum(example_id in ai_gold_ids for example_id in assignments),
    }
    assignments_list = [
        {"example_id": example_id, "phase": assignments[example_id]}
        for example_id in sorted(assignments)
    ]
    epoch_orders: list[dict[str, Any]] = []
    for epoch in range(epochs):
        identifiers = _ordered_ids(assignments, mode=mode, seed=seed, epoch=epoch)
        epoch_orders.append(
            {
                "epoch": epoch,
                "example_ids": identifiers,
                "example_ids_sha256": hash_identifier_sequence(identifiers),
            }
        )
    plan: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_curriculum_order_plan",
        "algorithm": ORDER_ALGORITHM,
        "phase_rules_version": PHASE_RULES_VERSION,
        "mode": mode,
        "seed": seed,
        "epochs": epochs,
        "source": source,
        "ai_gold_train": ai_gold,
        "phase_counts": {str(phase): phase_counts[phase] for phase in PHASES},
        "assignments": assignments_list,
        "epoch_orders": epoch_orders,
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "holdout_inputs": False,
    }
    return validate_curriculum_plan(plan)


@cache
def _plan_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_PLAN_SCHEMA.read_text(encoding="utf-8")))


def validate_curriculum_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate both the closed schema and every derived order/count."""

    materialized = json.loads(json.dumps(value, ensure_ascii=False))
    errors = sorted(_plan_validator().iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"curriculum plan schema failed: {errors[0].message}")
    assignments_list = materialized["assignments"]
    if assignments_list != sorted(
        assignments_list,
        key=lambda item: item["example_id"],
    ):
        raise ValueError("curriculum assignments must be sorted by example_id")
    assignments: dict[str, int] = {}
    for item in assignments_list:
        example_id = item["example_id"]
        if example_id in assignments:
            raise ValueError(f"duplicate curriculum assignment: {example_id}")
        assignments[example_id] = item["phase"]
    declared_counts = {int(key): value for key, value in materialized["phase_counts"].items()}
    actual_counts = Counter(assignments.values())
    if declared_counts != {phase: actual_counts[phase] for phase in PHASES}:
        raise ValueError("curriculum phase counts differ from assignments")
    if materialized["source"]["record_count"] != len(assignments):
        raise ValueError("curriculum source count differs from assignments")
    matched_ai_gold = materialized["ai_gold_train"]["matched_source_records"]
    if matched_ai_gold > min(
        materialized["source"]["record_count"],
        materialized["ai_gold_train"]["record_count"],
    ):
        raise ValueError("curriculum AI-Gold matched count exceeds bound inputs")
    if len(materialized["epoch_orders"]) != materialized["epochs"]:
        raise ValueError("curriculum epoch order count differs from epochs")
    expected_ids = set(assignments)
    for expected_epoch, order in enumerate(materialized["epoch_orders"]):
        if order["epoch"] != expected_epoch:
            raise ValueError("curriculum epoch indices must be contiguous from zero")
        identifiers = order["example_ids"]
        if len(identifiers) != len(expected_ids) or len(set(identifiers)) != len(identifiers):
            raise ValueError("curriculum epoch order is not a unique full-length permutation")
        if set(identifiers) != expected_ids:
            raise ValueError("curriculum epoch order differs from assigned examples")
        if order["example_ids_sha256"] != hash_identifier_sequence(identifiers):
            raise ValueError("curriculum epoch order SHA-256 mismatch")
        recomputed = _ordered_ids(
            assignments,
            mode=materialized["mode"],
            seed=materialized["seed"],
            epoch=expected_epoch,
        )
        if identifiers != recomputed:
            raise ValueError("curriculum epoch order differs from the declared algorithm")
    return materialized


def load_curriculum_plan(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Load one stable closed plan and optionally require its exact byte hash."""

    _, payload = _stable_regular_bytes(path, "curriculum plan")
    actual_sha256 = sha256_bytes(payload)
    if expected_sha256 is not None:
        expected = _checked_sha256(
            expected_sha256,
            "curriculum plan expected SHA-256",
        )
        if actual_sha256 != expected:
            raise ValueError(
                "curriculum plan SHA-256 mismatch: "
                f"expected {expected}, got {actual_sha256}"
            )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"curriculum plan is invalid JSON: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("curriculum plan must contain an object")
    return validate_curriculum_plan(value), actual_sha256


def write_curriculum_plan(
    plan: Mapping[str, Any],
    *,
    destination: str | Path,
    manifest_destination: str | Path,
) -> dict[str, Any]:
    """Write a validated plan plus its compact immutable binding manifest."""

    validated = validate_curriculum_plan(plan)
    payload = canonical_json_bytes(validated)
    destination_path = write_immutable_bytes(destination, payload, label="curriculum plan")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_curriculum_order_plan_manifest",
        "plan_filename": destination_path.name,
        "plan_sha256": sha256_bytes(payload),
        "mode": validated["mode"],
        "seed": validated["seed"],
        "epochs": validated["epochs"],
        "record_count": validated["source"]["record_count"],
        "phase_counts": validated["phase_counts"],
        "source_sha256": validated["source"]["sha256"],
        "ai_gold_train_sha256": validated["ai_gold_train"]["sha256"],
    }
    write_immutable_json(
        manifest_destination,
        manifest,
        label="curriculum plan manifest",
    )
    return manifest

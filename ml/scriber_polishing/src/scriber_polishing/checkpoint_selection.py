"""Fail-closed, within-run selection for completed final-training checkpoints.

The selector consumes evidence only. It never trains, loads a model, uses a
GPU, or contacts a hub. Publication receives a selected-checkpoint handoff and
validates only that checkpoint's pinned-tokenizer reload and fresh inventory.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_PROJECT_ROOT = Path(__file__).parents[2]
_CONTRACTS_ROOT = _PROJECT_ROOT / "contracts"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_RE = re.compile(r"^checkpoint-([1-9][0-9]*)$")
_PUBLICATION_HOOKS = (
    "scriber_polishing.hub_publication.prepare_publication_manifest",
    "scriber_polishing.hub_publication.offline_reload_model",
    "scriber_polishing.hub_publication.publish_private_bundle",
)
_EXPECTED_COMPOSITE = [
    {"metric": "generated_safety_content", "weight": 0.6},
    {"metric": "task", "weight": 0.25},
    {"metric": "structure", "weight": 0.1},
    {"metric": "normalized_eval_loss", "weight": 0.05},
]
_EXPECTED_TIE_BREAKERS = [
    {"field": "hard_safety_passed", "direction": "desc"},
    {"field": "composite_score", "direction": "desc"},
    {"field": "critical_error_count", "direction": "asc"},
    {"field": "generated_subtotal", "direction": "desc"},
    {"field": "eval_loss", "direction": "asc"},
    {"field": "global_step", "direction": "asc"},
]


class CheckpointSelectionError(ValueError):
    """Raised when immutable checkpoint-selection evidence is inconsistent."""


def canonical_json_sha256(value: object) -> str:
    """Return the SHA-256 of a canonical JSON value, including its prefix."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def publication_integration_hooks() -> tuple[str, ...]:
    """List future publication hooks without invoking them."""

    return _PUBLICATION_HOOKS


@cache
def _validator(file_name: str) -> Draft202012Validator:
    schema = json.loads((_CONTRACTS_ROOT / file_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CheckpointSelectionError(f"{label} must be a mapping")
    return value


def _validate_schema(value: Mapping[str, Any], file_name: str, label: str) -> dict[str, Any]:
    materialized = dict(value)
    errors = sorted(_validator(file_name).iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise CheckpointSelectionError(f"{label} failed schema: {errors[0].message}")
    return materialized


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise CheckpointSelectionError(f"{label} must use sha256:<64 lowercase hex>")
    return value


def _finite_number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise CheckpointSelectionError(f"{label} must be finite numeric evidence")
    number = float(value)
    if number < minimum:
        raise CheckpointSelectionError(f"{label} must be at least {minimum}")
    return number


def _json_copy(value: object) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def validate_checkpoint_selection_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen within-run policy without accepting static candidate IDs."""

    materialized = _validate_schema(config, "checkpoint_selection_config_schema.json", "checkpoint selection config")
    if materialized["generated_metrics"]["composite"] != _EXPECTED_COMPOSITE:
        raise CheckpointSelectionError("checkpoint selection composite must use the frozen 60/25/10/5 formula")
    if materialized["tie_breakers"] != _EXPECTED_TIE_BREAKERS:
        raise CheckpointSelectionError("checkpoint selection tie-breakers do not match the frozen order")
    if materialized["publication"]["base_tokenizer"] != materialized["checkpoint"]["base_model"]:
        raise CheckpointSelectionError("pinned base tokenizer must exactly match the checkpoint base model pin")
    if "candidates" in materialized:
        raise CheckpointSelectionError("within-run checkpoint selection config must not declare candidate IDs")
    return materialized


def load_checkpoint_selection_config(path: str | Path) -> dict[str, Any]:
    """Load one repository-owned within-run checkpoint selection policy."""

    with Path(path).open(encoding="utf-8") as file_obj:
        config = yaml.safe_load(file_obj)
    if not isinstance(config, Mapping):
        raise CheckpointSelectionError("checkpoint selection config must be a mapping")
    return validate_checkpoint_selection_config(config)


def _checkpoint_step(checkpoint: object, label: str) -> int:
    if not isinstance(checkpoint, str):
        raise CheckpointSelectionError(f"{label} must be checkpoint-<step>")
    match = _CHECKPOINT_RE.fullmatch(checkpoint)
    if match is None:
        raise CheckpointSelectionError(f"{label} must be checkpoint-<step>")
    return int(match.group(1))


def validate_completed_final_training_report(
    report: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the immutable completed final report used for candidate discovery."""

    validated_config = validate_checkpoint_selection_config(config)
    materialized = _validate_schema(
        report,
        "completed_final_training_report_schema.json",
        "completed final training report",
    )
    policy = validated_config["training_report"]
    if materialized["schema_version"] != policy["schema_version"]:
        raise CheckpointSelectionError("completed training report schema version does not match selection policy")
    if materialized["run_kind"] != policy["run_kind"]:
        raise CheckpointSelectionError("completed training report is not the configured final run")
    if policy["require_bf16"] and materialized["bf16"] is not True:
        raise CheckpointSelectionError("completed final training report must attest BF16")
    if policy["require_training_completed"] and materialized["training_completed"] is not True:
        raise CheckpointSelectionError("completed final training report is not complete")
    checkpoint_policy = validated_config["checkpoint"]
    if materialized["base_model"] != checkpoint_policy["base_model"]["id"]:
        raise CheckpointSelectionError("completed training report base model does not match selection policy")
    if materialized["base_revision"] != checkpoint_policy["base_model"]["revision"]:
        raise CheckpointSelectionError("completed training report base revision does not match selection policy")

    state = _mapping(materialized["checkpoint_state"], "completed training checkpoint state")
    best_checkpoint = state["best_model_checkpoint"]
    best_metric = state["best_metric"]
    if (best_checkpoint is None) != (best_metric is None):
        raise CheckpointSelectionError(
            "completed training best_model_checkpoint and best_metric must either both be present or both be null"
        )
    best_present = best_checkpoint is not None
    if materialized["early_stopping"] is not best_present:
        raise CheckpointSelectionError(
            "completed training early_stopping state does not match best checkpoint tracking evidence"
        )
    if best_metric is not None:
        _finite_number(best_metric, "completed training best eval loss")
    completed_step = _checkpoint_step(state["last_checkpoint"], "completed training last checkpoint")
    if state["global_step"] < completed_step:
        raise CheckpointSelectionError("completed training global step is before last checkpoint")
    if state["global_step"] > materialized["max_steps"]:
        raise CheckpointSelectionError("completed training global step exceeds max_steps")
    if not materialized["early_stopping"] and state["global_step"] != materialized["max_steps"]:
        raise CheckpointSelectionError("completed training without early stopping did not reach max_steps")
    if best_checkpoint is not None:
        _checkpoint_step(best_checkpoint, "completed training best-loss checkpoint")
    survivors = state["surviving_checkpoints"]
    by_name: dict[str, Mapping[str, Any]] = {}
    prior_step = 0
    for survivor in survivors:
        entry = _mapping(survivor, "surviving checkpoint")
        step = _checkpoint_step(entry["checkpoint"], "surviving checkpoint")
        if entry["global_step"] != step:
            raise CheckpointSelectionError("surviving checkpoint global step differs from checkpoint ID")
        if step <= prior_step:
            raise CheckpointSelectionError("surviving checkpoints must be strictly increasing by global step")
        if entry["run_fingerprint"] != materialized["run_fingerprint"]:
            raise CheckpointSelectionError("surviving checkpoint run fingerprint differs from completed report")
        prior_step = step
        by_name[entry["checkpoint"]] = entry
    if state["last_checkpoint"] not in by_name or state["last_checkpoint"] != survivors[-1]["checkpoint"]:
        raise CheckpointSelectionError("completed training last checkpoint is not the latest surviving checkpoint")
    if best_checkpoint is not None and best_checkpoint not in by_name:
        raise CheckpointSelectionError("completed training best-loss checkpoint is not surviving")
    return materialized


def discover_surviving_checkpoint_candidates(
    completed_training_report: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Discover exactly the best-loss checkpoint plus the configured recent tail."""

    report = validate_completed_final_training_report(completed_training_report, config)
    policy = validate_checkpoint_selection_config(config)["training_report"]
    state = report["checkpoint_state"]
    survivors = state["surviving_checkpoints"]
    tail = survivors[-policy["recent_tail_count"] :]
    selected = {entry["checkpoint"] for entry in tail}
    if state["best_model_checkpoint"] is not None:
        selected.add(state["best_model_checkpoint"])
    ordered = [entry for entry in survivors if entry["checkpoint"] in selected]
    return {
        "run_fingerprint": report["run_fingerprint"],
        "best_checkpoint_metric": policy["best_checkpoint_metric"],
        "best_loss_checkpoint": state["best_model_checkpoint"],
        "recent_tail_checkpoints": [entry["checkpoint"] for entry in tail],
        "candidate_checkpoint_ids": [entry["checkpoint"] for entry in ordered],
        "surviving_checkpoints": _json_copy(ordered),
    }


def _assert_exact_inventory(inventory: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    files = inventory["files"]
    paths = [entry["path"] for entry in files]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise CheckpointSelectionError("checkpoint inventory files must be strictly path-sorted and unique")
    if any(
        Path(path).is_absolute() or path.startswith(("/", "\\")) or ".." in Path(path).parts
        for path in paths
    ):
        raise CheckpointSelectionError("checkpoint inventory contains an unsafe file path")
    if inventory["file_count"] != len(files):
        raise CheckpointSelectionError("checkpoint inventory file_count does not match files")
    if inventory["total_bytes"] != sum(entry["bytes"] for entry in files):
        raise CheckpointSelectionError("checkpoint inventory total_bytes does not match files")
    config_entries = [entry for entry in files if entry["path"] == "config.json"]
    if len(config_entries) != 1:
        raise CheckpointSelectionError("checkpoint inventory must contain exactly one config.json")
    if identity["config_sha256"] != config_entries[0]["sha256"]:
        raise CheckpointSelectionError("checkpoint identity config hash does not match inventory config.json")
    if identity["tree_sha256"] != inventory["tree_sha256"]:
        raise CheckpointSelectionError("checkpoint identity tree hash does not match inventory")
    model_files = [
        {"path": entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
        for entry in files
        if entry["path"].endswith(".safetensors")
    ]
    if not model_files:
        raise CheckpointSelectionError("checkpoint inventory has no safetensors model files")
    if identity["model_sha256"] != canonical_json_sha256(model_files):
        raise CheckpointSelectionError("checkpoint identity model hash does not match inventory model files")


def _assert_automatic_report(
    automatic_report: Mapping[str, Any],
    evidence: Mapping[str, Any],
    config: Mapping[str, Any],
) -> None:
    identity = evidence["checkpoint_identity"]
    manifests = evidence["evaluation_manifests"]
    policy = config["generated_metrics"]
    expected = {
        "candidate_id": evidence["candidate_id"],
        "run_fingerprint": evidence["run_fingerprint"],
        "checkpoint": identity["checkpoint"],
        "global_step": identity["global_step"],
        "identity_sha256": identity["identity_sha256"],
        "reference_manifest_sha256": manifests["reference_manifest_sha256"],
        "prediction_manifest_sha256": manifests["prediction_manifest_sha256"],
    }
    for field, value in expected.items():
        if automatic_report[field] != value:
            raise CheckpointSelectionError(f"deterministic automatic report {field} does not match candidate evidence")
    if automatic_report["generator"] != policy["generator"]:
        raise CheckpointSelectionError("deterministic automatic report generator does not match selection policy")
    if automatic_report["status"] != "complete" or automatic_report["provenance"] != "generated":
        raise CheckpointSelectionError("deterministic automatic report must be complete generated evidence")
    required_gates = set(policy["required_hard_safety_gates"])
    if set(automatic_report["hard_safety_gates"]) != required_gates:
        raise CheckpointSelectionError("hard safety gates do not exactly match selection policy")
    if "critical_errors_zero" in required_gates:
        expected_critical_gate = automatic_report["critical_error_count"] == 0
        if automatic_report["hard_safety_gates"]["critical_errors_zero"] is not expected_critical_gate:
            raise CheckpointSelectionError("critical_errors_zero gate does not match automatic critical error count")
    for field in ("generated_safety_content_score", "task_score", "structure_score"):
        score = _finite_number(automatic_report[field], f"deterministic automatic report {field}")
        if score > 1.0:
            raise CheckpointSelectionError(f"deterministic automatic report {field} must be normalized to [0, 1]")


def validate_checkpoint_candidate_evidence(
    evidence: Mapping[str, Any],
    completed_training_report: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
) -> dict[str, Any]:
    """Bind one evaluation candidate to its completed final-training checkpoint."""

    validated_config = validate_checkpoint_selection_config(config)
    report = validate_completed_final_training_report(completed_training_report, validated_config)
    _require_hash(completed_training_report_sha256, "completed training report hash")
    _require_hash(selection_config_sha256, "selection config hash")
    materialized = _validate_schema(
        evidence,
        "checkpoint_candidate_evidence_schema.json",
        "checkpoint candidate evidence",
    )
    if materialized["completed_training_report_sha256"] != completed_training_report_sha256:
        raise CheckpointSelectionError("candidate evidence completed training report hash does not match input")
    if materialized["selection_config_sha256"] != selection_config_sha256:
        raise CheckpointSelectionError("candidate evidence selection config hash does not match input")
    if materialized["run_fingerprint"] != report["run_fingerprint"]:
        raise CheckpointSelectionError("candidate run fingerprint does not match completed training report")

    discovery = discover_surviving_checkpoint_candidates(report, validated_config)
    surviving = {
        entry["checkpoint"]: entry
        for entry in discovery["surviving_checkpoints"]
    }
    candidate_id = materialized["candidate_id"]
    if candidate_id not in surviving:
        raise CheckpointSelectionError("candidate checkpoint is not in the discovered completed-training inventory")
    expected_checkpoint = surviving[candidate_id]
    identity = _mapping(materialized["checkpoint_identity"], "checkpoint identity")
    expected_identity = {
        "checkpoint": candidate_id,
        "global_step": expected_checkpoint["global_step"],
        "run_fingerprint": report["run_fingerprint"],
        "identity_sha256": expected_checkpoint["identity_sha256"],
    }
    for field, expected in expected_identity.items():
        if identity[field] != expected:
            raise CheckpointSelectionError(f"checkpoint identity {field} does not match completed training report")
    checkpoint_policy = validated_config["checkpoint"]
    for field in ("architecture", "model_type", "base_model"):
        if identity[field] != checkpoint_policy[field]:
            raise CheckpointSelectionError(f"checkpoint identity {field} does not match selection policy")

    inventory = _mapping(materialized["checkpoint_inventory"], "checkpoint inventory")
    _validate_schema(inventory, "checkpoint_inventory_schema.json", "checkpoint inventory")
    for field in ("format", "variant", "model_type"):
        if inventory[field] != checkpoint_policy[field]:
            raise CheckpointSelectionError(f"checkpoint inventory {field} does not match selection policy")
    _assert_exact_inventory(inventory, identity)

    automatic_report = _mapping(materialized["deterministic_automatic_report"], "deterministic automatic report")
    _assert_automatic_report(automatic_report, materialized, validated_config)
    checkpoint_eval = _mapping(materialized["checkpoint_eval"], "checkpoint eval-loss evidence")
    if checkpoint_eval["metric"] != validated_config["training_report"]["best_checkpoint_metric"]:
        raise CheckpointSelectionError("checkpoint eval-loss metric does not match selection policy")
    if checkpoint_eval["global_step"] != identity["global_step"]:
        raise CheckpointSelectionError("checkpoint eval loss global step does not match checkpoint identity")
    if checkpoint_eval["trainer_state_sha256"] != expected_checkpoint["trainer_state_sha256"]:
        raise CheckpointSelectionError("checkpoint eval loss trainer-state hash does not match completed training report")
    _finite_number(checkpoint_eval["eval_loss"], "checkpoint eval loss")
    return materialized


def _validated_candidate_map(
    completed_training_report: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not isinstance(candidates, Sequence) or isinstance(candidates, str | bytes):
        raise CheckpointSelectionError("checkpoint candidates must be a sequence of evidence mappings")
    report = validate_completed_final_training_report(completed_training_report, config)
    discovery = discover_surviving_checkpoint_candidates(report, config)
    validated: dict[str, dict[str, Any]] = {}
    for evidence in candidates:
        materialized = validate_checkpoint_candidate_evidence(
            evidence,
            report,
            config,
            completed_training_report_sha256=completed_training_report_sha256,
            selection_config_sha256=selection_config_sha256,
        )
        candidate_id = materialized["candidate_id"]
        if candidate_id in validated:
            raise CheckpointSelectionError(f"duplicate checkpoint candidate evidence: {candidate_id}")
        validated[candidate_id] = materialized
    expected_ids = set(discovery["candidate_checkpoint_ids"])
    observed_ids = set(validated)
    if observed_ids != expected_ids:
        missing = sorted(expected_ids.difference(observed_ids))
        unexpected = sorted(observed_ids.difference(expected_ids))
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if unexpected:
            detail.append(f"unexpected={','.join(unexpected)}")
        raise CheckpointSelectionError(
            f"exact discovered candidate inventory is incomplete or changed ({'; '.join(detail)})"
        )
    return validated, discovery


def _selected_checkpoint_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    identity = evidence["checkpoint_identity"]
    inventory = evidence["checkpoint_inventory"]
    automatic_report = evidence["deterministic_automatic_report"]
    checkpoint_eval = evidence["checkpoint_eval"]
    return {
        "candidate_id": evidence["candidate_id"],
        "checkpoint": identity["checkpoint"],
        "global_step": identity["global_step"],
        "run_fingerprint": identity["run_fingerprint"],
        "identity_sha256": identity["identity_sha256"],
        "tree_sha256": identity["tree_sha256"],
        "config_sha256": identity["config_sha256"],
        "model_sha256": identity["model_sha256"],
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "candidate_evidence_sha256": canonical_json_sha256(evidence),
        "deterministic_automatic_report_sha256": automatic_report["report_sha256"],
        "eval_loss": checkpoint_eval["eval_loss"],
    }


def _selection_rows(
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    discovery: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    policy = config["generated_metrics"]
    losses = [float(evidence["checkpoint_eval"]["eval_loss"]) for evidence in evidence_by_id.values()]
    lowest_loss = min(losses)
    highest_loss = max(losses)
    loss_span = highest_loss - lowest_loss
    rows: list[dict[str, Any]] = []
    for checkpoint in discovery["candidate_checkpoint_ids"]:
        evidence = evidence_by_id[checkpoint]
        automatic = evidence["deterministic_automatic_report"]
        eval_loss = float(evidence["checkpoint_eval"]["eval_loss"])
        normalized_eval_loss = 0.0 if loss_span == 0.0 else (eval_loss - lowest_loss) / loss_span
        score_values = {
            "generated_safety_content": float(automatic["generated_safety_content_score"]),
            "task": float(automatic["task_score"]),
            "structure": float(automatic["structure_score"]),
            "normalized_eval_loss": 1.0 - normalized_eval_loss,
        }
        generated_subtotal = round(
            math.fsum(
                float(component["weight"]) * score_values[component["metric"]]
                for component in policy["composite"]
                if component["metric"] != "normalized_eval_loss"
            ),
            12,
        )
        composite_score = round(
            math.fsum(
                float(component["weight"]) * score_values[component["metric"]]
                for component in policy["composite"]
            ),
            12,
        )
        hard_safety_gates = {
            gate_id: automatic["hard_safety_gates"][gate_id]
            for gate_id in policy["required_hard_safety_gates"]
        }
        failed_gates = [gate_id for gate_id, passed in hard_safety_gates.items() if not passed]
        rows.append(
            {
                "candidate_id": evidence["candidate_id"],
                "checkpoint": _selected_checkpoint_summary(evidence),
                "hard_safety_gates": hard_safety_gates,
                "hard_safety_passed": not failed_gates,
                "failed_hard_safety_gates": failed_gates,
                "critical_error_count": automatic["critical_error_count"],
                "generated_subtotal": generated_subtotal,
                "normalized_eval_loss": round(normalized_eval_loss, 12),
                "eval_loss": eval_loss,
                "composite_score": composite_score,
                "rank": 0,
            }
        )
    ranked = sorted(
        rows,
        key=lambda row: (
            not row["hard_safety_passed"],
            -float(row["composite_score"]),
            int(row["critical_error_count"]),
            -float(row["generated_subtotal"]),
            float(row["eval_loss"]),
            int(row["checkpoint"]["global_step"]),
        ),
    )
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    return rows


def select_checkpoint(
    completed_training_report: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
) -> dict[str, Any]:
    """Select one safety-passed checkpoint from the exact best-loss-plus-tail set."""

    validated_config = validate_checkpoint_selection_config(config)
    _require_hash(completed_training_report_sha256, "completed training report hash")
    _require_hash(selection_config_sha256, "selection config hash")
    evidence_by_id, discovery = _validated_candidate_map(
        completed_training_report,
        candidates,
        validated_config,
        completed_training_report_sha256=completed_training_report_sha256,
        selection_config_sha256=selection_config_sha256,
    )
    rows = _selection_rows(evidence_by_id, discovery, validated_config)
    selected_row = next((row for row in rows if row["rank"] == 1 and row["hard_safety_passed"]), None)
    selected = _json_copy(selected_row["checkpoint"]) if selected_row is not None else None
    report = {
        "schema_version": 2,
        "status": "selected" if selected is not None else "no_safe_candidate",
        "selection_config_sha256": selection_config_sha256,
        "completed_training_report_sha256": completed_training_report_sha256,
        "run_fingerprint": discovery["run_fingerprint"],
        "candidate_inventory": _json_copy(discovery),
        "tie_breakers": _json_copy(validated_config["tie_breakers"]),
        "candidates": rows,
        "selected": selected,
    }
    _validate_schema(report, "checkpoint_selection_report_schema.json", "checkpoint selection report")
    return report


def validate_checkpoint_selection_report(
    selection_report: Mapping[str, Any],
    completed_training_report: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
) -> dict[str, Any]:
    """Recompute selection from the immutable report and reject altered rankings."""

    materialized = _validate_schema(
        selection_report,
        "checkpoint_selection_report_schema.json",
        "checkpoint selection report",
    )
    expected = select_checkpoint(
        completed_training_report,
        candidates,
        config,
        completed_training_report_sha256=completed_training_report_sha256,
        selection_config_sha256=selection_config_sha256,
    )
    if materialized != expected:
        raise CheckpointSelectionError("checkpoint selection report does not match recomputed selection")
    return materialized


def build_checkpoint_selection_handoff(
    selection_report: Mapping[str, Any],
    completed_training_report: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
    selection_report_sha256: str,
) -> dict[str, Any]:
    """Emit an immutable selected-checkpoint handoff without publishing anything."""

    validated_config = validate_checkpoint_selection_config(config)
    _require_hash(selection_report_sha256, "selection report hash")
    report = validate_checkpoint_selection_report(
        selection_report,
        completed_training_report,
        candidates,
        validated_config,
        completed_training_report_sha256=completed_training_report_sha256,
        selection_config_sha256=selection_config_sha256,
    )
    if selection_report_sha256 != canonical_json_sha256(report):
        raise CheckpointSelectionError("selection report hash does not match canonical selection report")
    if report["status"] != "selected" or report["selected"] is None:
        raise CheckpointSelectionError("cannot emit a selection handoff without a safety-passed checkpoint")
    handoff = {
        "schema_version": 2,
        "status": "selected",
        "selection_config_sha256": selection_config_sha256,
        "completed_training_report_sha256": completed_training_report_sha256,
        "selection_report_sha256": selection_report_sha256,
        "run_fingerprint": report["run_fingerprint"],
        "selected_checkpoint": _json_copy(report["selected"]),
        "base_tokenizer": _json_copy(validated_config["publication"]["base_tokenizer"]),
    }
    _validate_schema(handoff, "checkpoint_selection_handoff_schema.json", "checkpoint selection handoff")
    return handoff


def validate_checkpoint_selection_handoff(
    handoff: Mapping[str, Any],
    selection_report: Mapping[str, Any],
    completed_training_report: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    completed_training_report_sha256: str,
    selection_config_sha256: str,
    selection_report_sha256: str,
) -> dict[str, Any]:
    """Recompute the handoff before it crosses into a later publication stage."""

    materialized = _validate_schema(
        handoff,
        "checkpoint_selection_handoff_schema.json",
        "checkpoint selection handoff",
    )
    expected = build_checkpoint_selection_handoff(
        selection_report,
        completed_training_report,
        candidates,
        config,
        completed_training_report_sha256=completed_training_report_sha256,
        selection_config_sha256=selection_config_sha256,
        selection_report_sha256=selection_report_sha256,
    )
    if materialized != expected:
        raise CheckpointSelectionError("checkpoint selection handoff does not match recomputed selection")
    return materialized


def _validate_handoff_for_publication(
    handoff: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    selection_handoff_sha256: str,
) -> dict[str, Any]:
    validated_config = validate_checkpoint_selection_config(config)
    materialized = _validate_schema(
        handoff,
        "checkpoint_selection_handoff_schema.json",
        "checkpoint selection handoff",
    )
    _require_hash(selection_handoff_sha256, "selection handoff hash")
    if selection_handoff_sha256 != canonical_json_sha256(materialized):
        raise CheckpointSelectionError("selection handoff hash does not match canonical selection handoff")
    if materialized["base_tokenizer"] != validated_config["publication"]["base_tokenizer"]:
        raise CheckpointSelectionError("selection handoff base tokenizer does not match pinned publication tokenizer")
    selected = materialized["selected_checkpoint"]
    if selected["candidate_id"] != selected["checkpoint"]:
        raise CheckpointSelectionError("selection handoff candidate ID does not match checkpoint identity")
    if selected["run_fingerprint"] != materialized["run_fingerprint"]:
        raise CheckpointSelectionError("selection handoff selected checkpoint run fingerprint does not match handoff")
    if _checkpoint_step(selected["checkpoint"], "selection handoff checkpoint") != selected["global_step"]:
        raise CheckpointSelectionError("selection handoff global step does not match checkpoint identity")
    return materialized


def _validate_publication_evidence(
    evidence: Mapping[str, Any],
    handoff: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    materialized = _validate_schema(
        evidence,
        "selected_checkpoint_publication_evidence_schema.json",
        "selected checkpoint publication evidence",
    )
    selected = handoff["selected_checkpoint"]
    if materialized["candidate_id"] != selected["candidate_id"]:
        raise CheckpointSelectionError("publication evidence candidate ID does not match selected checkpoint")
    if materialized["run_fingerprint"] != selected["run_fingerprint"]:
        raise CheckpointSelectionError("publication evidence run fingerprint does not match selected checkpoint")
    identity = materialized["checkpoint_identity"]
    for field in (
        "checkpoint",
        "global_step",
        "run_fingerprint",
        "identity_sha256",
        "tree_sha256",
        "config_sha256",
        "model_sha256",
    ):
        if identity[field] != selected[field]:
            raise CheckpointSelectionError(f"publication evidence checkpoint {field} does not match handoff")
    checkpoint_policy = config["checkpoint"]
    for field in ("architecture", "model_type", "base_model"):
        if identity[field] != checkpoint_policy[field]:
            raise CheckpointSelectionError(f"publication evidence checkpoint {field} does not match selection policy")
    if materialized["base_tokenizer"] != config["publication"]["base_tokenizer"]:
        raise CheckpointSelectionError("publication evidence base tokenizer does not match pinned tokenizer")
    reload_evidence = materialized["fresh_reload"]
    for field, expected in config["publication"]["required_reload"].items():
        if reload_evidence[field] != expected:
            raise CheckpointSelectionError(f"fresh reload {field} must be {expected}")
    reload_fields = {
        "tree_sha256": "model_tree_sha256",
        "config_sha256": "model_config_sha256",
        "model_sha256": "model_sha256",
    }
    for field, reload_field in reload_fields.items():
        if reload_evidence[reload_field] != selected[field]:
            raise CheckpointSelectionError(f"fresh reload {field} does not match selected checkpoint")
    fresh_inventory = materialized["fresh_inventory"]
    expected_inventory = {
        "tree_sha256": selected["tree_sha256"],
        "config_sha256": selected["config_sha256"],
        "model_sha256": selected["model_sha256"],
        "file_count": selected["file_count"],
        "total_bytes": selected["total_bytes"],
    }
    for field, expected in expected_inventory.items():
        if fresh_inventory[field] != expected:
            raise CheckpointSelectionError(f"fresh inventory {field} does not match selected checkpoint")
    return materialized


def build_selected_checkpoint_publication(
    selection_handoff: Mapping[str, Any],
    publication_evidence: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    selection_handoff_sha256: str,
) -> dict[str, Any]:
    """Prepare a publication handoff from one selected checkpoint only."""

    validated_config = validate_checkpoint_selection_config(config)
    handoff = _validate_handoff_for_publication(
        selection_handoff,
        validated_config,
        selection_handoff_sha256=selection_handoff_sha256,
    )
    evidence = _validate_publication_evidence(publication_evidence, handoff, validated_config)
    publication = {
        "schema_version": 2,
        "status": "ready_for_publication",
        "selection_handoff_sha256": selection_handoff_sha256,
        "selected_candidate_id": handoff["selected_checkpoint"]["candidate_id"],
        "checkpoint": _json_copy(handoff["selected_checkpoint"]),
        "base_tokenizer": _json_copy(validated_config["publication"]["base_tokenizer"]),
        "fresh_reload": _json_copy(evidence["fresh_reload"]),
        "fresh_inventory": _json_copy(evidence["fresh_inventory"]),
        "integration_hooks": list(publication_integration_hooks()),
    }
    _validate_schema(publication, "selected_checkpoint_publication_schema.json", "selected checkpoint publication")
    return publication


def validate_selected_checkpoint_publication(
    publication: Mapping[str, Any],
    selection_handoff: Mapping[str, Any],
    publication_evidence: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    selection_handoff_sha256: str,
) -> dict[str, Any]:
    """Validate a publication handoff without revisiting unselected candidates."""

    materialized = _validate_schema(
        publication,
        "selected_checkpoint_publication_schema.json",
        "selected checkpoint publication",
    )
    expected = build_selected_checkpoint_publication(
        selection_handoff,
        publication_evidence,
        config,
        selection_handoff_sha256=selection_handoff_sha256,
    )
    if materialized != expected:
        raise CheckpointSelectionError("selected checkpoint publication does not match selected-checkpoint evidence")
    return materialized

"""Fail-closed adoption comparison for completed remediation A/B arms."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import (
    canonical_json_bytes,
    hash_identifier_sequence,
    write_immutable_json,
)
from .curriculum_comparison import (
    aggregate_case_evaluations,
    load_curriculum_comparison_config,
    paired_bootstrap_composite_delta,
    score_comparison_candidate,
)
from .evaluation_io import (
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)
from .metrics import CaseEvaluation, EvaluationCase, evaluate_predictions
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .remediation_experiment import (
    validate_matched_remediation_plans,
    validate_remediation_experiment_manifest,
    validate_remediation_order_plan,
    validate_remediation_parity_dev_manifest,
)

_PROJECT_ROOT = Path(__file__).parents[2]
_REPORT_SCHEMA = (
    _PROJECT_ROOT
    / "contracts"
    / "remediation_comparison_report_schema.json"
)
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT = re.compile(r"^checkpoint-([1-9][0-9]*)$")
_EPSILON = 1e-12
_EXPECTED_WEIGHTS = {
    "safety_content": 0.60,
    "task_quality": 0.25,
    "structure": 0.10,
    "eval_loss": 0.05,
}
_QUALITY_METRICS = (
    "identity_exact_match",
    "punctuation_f1",
    "filler_f1",
    "repetition_cleanup_f1",
    "normalized_exact_match",
)
_MATCHED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "base_model",
    "base_revision",
    "run_kind",
    "gpu",
    "torch_version",
    "cuda_runtime",
    "bf16",
    "sdpa",
    "seed",
    "method",
    "optimizer",
    "learning_rate",
    "max_sequence_length",
    "micro_batch_size",
    "gradient_accumulation_steps",
    "effective_batch_size",
    "max_steps",
    "schedule",
    "early_stopping",
    "save_total_limit",
    "train_examples_loaded",
    "train_examples_used",
    "validation_examples_loaded",
    "validation_examples_used",
    "oversize_rejections",
    "protection_rejections",
)


class RemediationComparisonError(ValueError):
    """Completed remediation evidence is missing, inconsistent, or unsafe."""


@dataclass(frozen=True, slots=True)
class RemediationArmArtifacts:
    """Exact files required to evaluate one completed remediation arm."""

    training_report: Path
    predictions: Path
    prediction_manifest: Path
    evaluation_report: Path


@dataclass(frozen=True, slots=True)
class _LoadedFile:
    path: Path
    payload: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _ArmEvidence:
    mode: str
    training: dict[str, Any]
    training_sha256: str
    predictions_sha256: str
    prediction_manifest: dict[str, Any]
    prediction_manifest_sha256: str
    evaluation: dict[str, Any]
    evaluation_sha256: str
    cases: tuple[EvaluationCase, ...]
    results: tuple[CaseEvaluation, ...]
    aggregate: dict[str, Any]
    eval_loss: float
    restoration_error_count: int
    valid_sst_count: int
    run_fingerprint: str
    final_model_tree_sha256: str


@cache
def _report_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(_REPORT_SCHEMA.read_text(encoding="utf-8"))
    )


def _stable_bytes(path: str | Path, label: str) -> _LoadedFile:
    candidate = Path(path)
    if candidate.is_symlink():
        raise RemediationComparisonError(
            f"{label} must not be a symlink: {candidate}"
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise RemediationComparisonError(
            f"{label} must be a regular file: {resolved}"
        )
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise RemediationComparisonError(
            f"cannot read {label}: {error}"
        ) from error
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
        raise RemediationComparisonError(
            f"{label} changed while it was being read"
        )
    return _LoadedFile(
        path=resolved,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _json_object(loaded: _LoadedFile, label: str) -> dict[str, Any]:
    try:
        value = json.loads(loaded.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemediationComparisonError(
            f"{label} is invalid JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RemediationComparisonError(
            f"{label} must contain an object"
        )
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RemediationComparisonError(f"{label} must contain an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list | tuple):
        raise RemediationComparisonError(f"{label} must contain a sequence")
    return value


def _raw_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _RAW_SHA256.fullmatch(value) is None:
        raise RemediationComparisonError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return value


def _prefixed_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or _PREFIXED_SHA256.fullmatch(value) is None
    ):
        raise RemediationComparisonError(
            f"{label} must use sha256:<64 lowercase hexadecimal characters>"
        )
    return value


def _normalized_raw_sha256(value: object, label: str) -> str:
    if isinstance(value, str):
        value = value.removeprefix("sha256:")
    return _raw_sha256(value, label)


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RemediationComparisonError(
            f"{label} must be a non-negative integer"
        )
    return value


def _positive_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RemediationComparisonError(
            f"{label} must be a finite positive number"
        )
    return float(value)


def _finite_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        raise RemediationComparisonError(
            f"{label} must be a finite number"
        )
    return float(value)


def _artifact(
    bundle_dir: Path,
    binding: Mapping[str, Any],
    label: str,
) -> _LoadedFile:
    filename = binding.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
    ):
        raise RemediationComparisonError(
            f"{label} must declare one safe basename"
        )
    loaded = _stable_bytes(bundle_dir / filename, label)
    expected = _raw_sha256(binding.get("sha256"), f"{label} SHA-256")
    if loaded.sha256 != expected:
        raise RemediationComparisonError(
            f"{label} SHA-256 mismatch: expected {expected}, "
            f"got {loaded.sha256}"
        )
    return loaded


def _canonical_object_artifact(
    bundle_dir: Path,
    binding: Mapping[str, Any],
    label: str,
) -> tuple[_LoadedFile, dict[str, Any]]:
    loaded = _artifact(bundle_dir, binding, label)
    value = _json_object(loaded, label)
    if loaded.payload != canonical_json_bytes(value):
        raise RemediationComparisonError(
            f"{label} is not canonical immutable JSON"
        )
    return loaded, value


def _load_bundle(
    manifest_path: str | Path,
    references_path: str | Path,
) -> tuple[
    _LoadedFile,
    dict[str, Any],
    _LoadedFile,
    tuple[dict[str, Any], ...],
    str,
    dict[str, Any],
]:
    loaded_manifest = _stable_bytes(
        manifest_path,
        "remediation experiment manifest",
    )
    try:
        manifest = validate_remediation_experiment_manifest(
            _json_object(
                loaded_manifest,
                "remediation experiment manifest",
            )
        )
    except (TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"remediation experiment manifest is invalid: {error}"
        ) from error
    if loaded_manifest.payload != canonical_json_bytes(manifest):
        raise RemediationComparisonError(
            "remediation experiment manifest is not canonical immutable JSON"
        )
    bundle_dir = loaded_manifest.path.parent

    parity_loaded, parity_value = _canonical_object_artifact(
        bundle_dir,
        _mapping(manifest["parity_dev"]["manifest"], "parity manifest binding"),
        "parity-dev manifest",
    )
    try:
        parity_manifest = validate_remediation_parity_dev_manifest(
            parity_value
        )
    except (TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"parity-dev manifest is invalid: {error}"
        ) from error
    if parity_loaded.payload != canonical_json_bytes(parity_manifest):
        raise RemediationComparisonError(
            "validated parity-dev manifest changed canonical bytes"
        )
    if (
        parity_manifest["references"] != manifest["parity_dev"]["references"]
        or parity_manifest["selected_example_ids_sha256"]
        != manifest["parity_dev"]["selected_example_ids_sha256"]
        or parity_manifest["parent_split"] != "validation"
        or parity_manifest["holdout_inputs"] is not False
    ):
        raise RemediationComparisonError(
            "parity-dev manifest differs from the experiment binding"
        )

    references_loaded = _stable_bytes(
        references_path,
        "parity-dev references",
    )
    expected_references = manifest["parity_dev"]["references"]
    if (
        references_loaded.path.parent != bundle_dir
        or references_loaded.path.name != expected_references["filename"]
        or references_loaded.sha256 != expected_references["sha256"]
    ):
        raise RemediationComparisonError(
            "references are not the exact bundle-local parity-dev artifact"
        )
    try:
        references = load_evaluation_references(references_loaded.path)
    except (OSError, TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"parity-dev references are invalid: {error}"
        ) from error
    if (
        len(references) != expected_references["record_count"]
        or any(reference["split"] != "regression" for reference in references)
    ):
        raise RemediationComparisonError(
            "parity-dev references differ from the validation-only count/split"
        )
    case_ids_sha256 = hash_identifier_sequence(
        [str(reference["case_id"]) for reference in references]
    )

    policy_binding = _mapping(
        manifest["adoption_policy"]["comparison_config"],
        "comparison policy binding",
    )
    policy_loaded = _artifact(
        bundle_dir,
        policy_binding,
        "remediation comparison policy",
    )
    try:
        policy = load_curriculum_comparison_config(policy_loaded.path)
    except (OSError, TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"remediation comparison policy is invalid: {error}"
        ) from error
    _require_fixed_policy(manifest, policy, policy_loaded.sha256)

    dataset_loaded, dataset_manifest = _canonical_object_artifact(
        bundle_dir,
        _mapping(
            manifest["selection"]["dataset_manifest"],
            "dataset manifest binding",
        ),
        "remediation dataset manifest",
    )
    del dataset_loaded
    _validate_dataset_manifest(dataset_manifest, manifest)

    plans: dict[str, dict[str, Any]] = {}
    for mode in ("shuffled", "staged"):
        _artifact(
            bundle_dir,
            _mapping(
                manifest["arms"][mode]["training_config"],
                f"{mode} training config binding",
            ),
            f"{mode} training config",
        )
        plan_loaded, plan_value = _canonical_object_artifact(
            bundle_dir,
            _mapping(
                manifest["arms"][mode]["plan"],
                f"{mode} order-plan binding",
            ),
            f"{mode} remediation order plan",
        )
        del plan_loaded
        try:
            plans[mode] = validate_remediation_order_plan(plan_value)
        except (TypeError, ValueError) as error:
            raise RemediationComparisonError(
                f"{mode} remediation order plan is invalid: {error}"
            ) from error
        _validate_plan_against_bundle(plans[mode], manifest, mode)
    try:
        validate_matched_remediation_plans(
            plans["shuffled"],
            plans["staged"],
        )
    except (TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"remediation plans are not a matched pair: {error}"
        ) from error
    return (
        loaded_manifest,
        manifest,
        references_loaded,
        references,
        case_ids_sha256,
        policy,
    )


def _require_fixed_policy(
    manifest: Mapping[str, Any],
    policy: Mapping[str, Any],
    policy_sha256: str,
) -> None:
    adoption = _mapping(
        manifest["adoption_policy"],
        "remediation adoption policy",
    )
    if (
        policy_sha256
        != adoption["comparison_config"]["sha256"]
        or policy["weights"] != _EXPECTED_WEIGHTS
        or adoption["weights"] != _EXPECTED_WEIGHTS
        or policy["seed"] != 270014
        or policy["bootstrap_resamples"] != 10_000
        or policy["confidence_level"] != 0.95
        or policy["minimum_composite_delta"] != 0.005
        or policy["maximum_eval_loss_ratio"] != 1.02
        or policy["require_no_new_critical_errors"] is not True
        or policy["require_metric_non_regression"] is not True
    ):
        raise RemediationComparisonError(
            "comparison policy differs from the pre-bound "
            "60/25/10/5, 10,000-resample adoption contract"
        )
    if (
        adoption["bootstrap"]
        != {
            "paired": True,
            "resamples": 10_000,
            "confidence_level": 0.95,
            "required_lower_bound": 0,
        }
        or adoption["minimum_composite_delta"] != 0.005
        or adoption["maximum_eval_loss_ratio"] != 1.02
        or adoption["hard_gates"]
        != {
            "protected_span_restoration_rate": 1,
            "protected_span_deletions": 0,
            "protected_span_duplications": 0,
            "protected_span_malformed": 0,
            "protected_span_reordered": 0,
            "new_critical_errors": 0,
            "sst_parse_rate_noninferior": True,
            "quality_metrics_noninferior": True,
            "evaluation_scope": "validation_only_parity_dev",
        }
        or adoption["fallback"] != "keep_shuffled"
    ):
        raise RemediationComparisonError(
            "experiment manifest changed the fixed adoption thresholds"
        )


def _validate_dataset_manifest(
    dataset: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    selection = manifest["selection"]
    parent = manifest["inputs"]["parent_checkpoint"]
    policy = manifest["inputs"]["protection_policy"]
    expected = {
        "schema_version": 1,
        "kind": "scriber_remediation_training_dataset",
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "holdout_inputs": False,
        "source_pilot_manifest_sha256": (
            manifest["inputs"]["pilot_manifest"]["sha256"]
        ),
        "protection_policy": {
            "policy_id": policy["policy_id"],
            "policy_sha256": policy["policy_sha256"],
            "artifact_sha256": policy["artifact_sha256"],
        },
        "parent_checkpoint": {
            "candidate_id": parent["candidate_id"],
            "tree_sha256": parent["tree_sha256"],
            "relationship": "warm_start_parent_not_resume",
        },
        "selected_train_example_ids_sha256": (
            selection["selected_example_ids_sha256"]
        ),
        "split_counts": {
            "train": selection["train"]["record_count"],
            "validation": selection["validation"]["record_count"],
        },
        "split_sha256": {
            "train": selection["train"]["sha256"],
            "validation": selection["validation"]["sha256"],
        },
    }
    if dict(dataset) != expected:
        raise RemediationComparisonError(
            "remediation dataset manifest differs from the experiment cohort"
        )


def _validate_plan_against_bundle(
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    mode: str,
) -> None:
    selection = manifest["selection"]
    parent = manifest["inputs"]["parent_checkpoint"]
    policy = manifest["inputs"]["protection_policy"]
    arm = manifest["arms"][mode]
    if (
        plan["mode"] != mode
        or plan["comparison_pair_sha256"]
        != manifest["comparison_pair_sha256"]
        or plan["source"]["sha256"] != selection["train"]["sha256"]
        or plan["source"]["record_count"]
        != selection["train"]["record_count"]
        or plan["source"]["selected_safe_example_ids_sha256"]
        != selection["selected_example_ids_sha256"]
        or plan["training_contract"] != manifest["training_contract"]
        or plan["protection_policy"]
        != {
            "policy_id": policy["policy_id"],
            "policy_sha256": policy["policy_sha256"],
            "artifact_sha256": policy["artifact_sha256"],
        }
        or plan["parent_checkpoint"]
        != {
            "candidate_id": parent["candidate_id"],
            "training_report_sha256": parent["training_report_sha256"],
            "tree_sha256": parent["tree_sha256"],
            "relationship": "warm_start_parent_not_resume",
        }
        or arm["plan"]["sha256"]
        != hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
    ):
        raise RemediationComparisonError(
            f"{mode} remediation plan differs from the experiment contract"
        )


def _self_hashed_training_report(
    report: Mapping[str, Any],
    label: str,
) -> str:
    fingerprint = _prefixed_sha256(
        report.get("run_fingerprint"),
        f"{label} run fingerprint",
    )
    bindings = _mapping(report.get("bindings"), f"{label} bindings")
    identity = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": dict(bindings),
    }
    expected = "sha256:" + hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    if fingerprint != expected:
        raise RemediationComparisonError(
            f"{label} run fingerprint is not the self-hash of its bindings"
        )
    return fingerprint


def _checkpoint_step(value: object, label: str) -> int:
    if not isinstance(value, str):
        raise RemediationComparisonError(
            f"{label} must use checkpoint-<step>"
        )
    match = _CHECKPOINT.fullmatch(value)
    if match is None:
        raise RemediationComparisonError(
            f"{label} must use checkpoint-<step>"
        )
    return int(match.group(1))


def _validate_training_report(
    loaded: _LoadedFile,
    *,
    mode: str,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], float, str, str]:
    label = f"{mode} training report"
    report = _json_object(loaded, label)
    if (
        report.get("schema_version") != 2
        or report.get("run_kind") != "improvement"
        or report.get("training_completed") is not True
        or "curriculum_order" in report
    ):
        raise RemediationComparisonError(
            f"{label} is not a completed improvement report"
        )
    fingerprint = _self_hashed_training_report(report, label)
    order = _mapping(
        report.get("remediation_order"),
        f"{label} remediation_order",
    )
    arm = manifest["arms"][mode]
    selection = manifest["selection"]
    parent = manifest["inputs"]["parent_checkpoint"]
    policy = manifest["inputs"]["protection_policy"]
    contract = manifest["training_contract"]
    empty_ids_sha256 = hash_identifier_sequence([])
    if (
        order.get("kind") != "scriber_remediation_order_plan"
        or order.get("mode") != mode
        or order.get("plan_sha256") != arm["plan"]["sha256"]
        or order.get("comparison_pair_sha256")
        != manifest["comparison_pair_sha256"]
        or order.get("source_sha256") != selection["train"]["sha256"]
        or order.get("record_count") != selection["train"]["record_count"]
        or order.get("safe_record_count")
        != selection["train"]["record_count"]
        or order.get("safe_example_ids_sha256")
        != selection["selected_example_ids_sha256"]
        or order.get("selected_policy_rejected_record_count") != 0
        or order.get("selected_policy_rejected_example_ids_sha256")
        != empty_ids_sha256
        or order.get("sequence_rejected_record_count") != 0
        or order.get("sequence_rejected_example_ids_sha256")
        != empty_ids_sha256
        or order.get("used_record_count")
        != selection["train"]["record_count"]
        or order.get("used_example_ids_sha256")
        != selection["selected_example_ids_sha256"]
        or order.get("protection_policy")
        != {
            "policy_id": policy["policy_id"],
            "policy_sha256": policy["policy_sha256"],
            "artifact_sha256": policy["artifact_sha256"],
        }
        or order.get("parent_checkpoint")
        != {
            "candidate_id": parent["candidate_id"],
            "training_report_sha256": parent["training_report_sha256"],
            "tree_sha256": parent["tree_sha256"],
            "relationship": "warm_start_parent_not_resume",
        }
        or order.get("training_contract") != contract
        or order.get("full_epoch_required") is not True
        or order.get("implicit_trainer_shuffling") is not False
        or order.get("state_initialization")
        != {
            "optimizer": "fresh",
            "scheduler": "fresh",
            "parent_as_resume": "forbidden",
            "same_run_resume": "identity_bound_recovery_only",
        }
    ):
        raise RemediationComparisonError(
            f"{label} remediation evidence differs from its bound arm"
        )

    expected_steps = manifest["schedule"]["update_steps"]
    schedule = _mapping(report.get("schedule"), f"{label} schedule")
    checkpoint = _mapping(
        report.get("checkpoint_state"),
        f"{label} checkpoint state",
    )
    last_checkpoint_step = _checkpoint_step(
        checkpoint.get("last_checkpoint"),
        f"{label} last checkpoint",
    )
    if (
        report.get("max_steps") != expected_steps
        or schedule.get("max_steps") != expected_steps
        or schedule.get("declared_epochs") != 1.0
        or schedule.get("effective_examples_per_step")
        != manifest["schedule"]["effective_batch_size"]
        or schedule.get("schedule_source") != "epochs"
        or schedule.get("update_steps_per_epoch") != expected_steps
        or schedule.get("epoch_boundaries_preserved") is not True
        or checkpoint.get("global_step") != expected_steps
        or last_checkpoint_step > expected_steps
    ):
        raise RemediationComparisonError(
            f"{label} did not complete the exact planned full epoch"
        )
    effective_orders = _sequence(
        order.get("effective_epoch_orders"),
        f"{label} effective epoch orders",
    )
    if (
        len(effective_orders) != 1
        or _mapping(
            effective_orders[0],
            f"{label} effective epoch order",
        ).get("record_count")
        != selection["train"]["record_count"]
    ):
        raise RemediationComparisonError(
            f"{label} lacks one complete effective epoch"
        )

    _validate_report_training_contract(report, contract, manifest, mode)
    _validate_report_dataset(report, manifest, mode)
    _validate_report_policy_parent(report, manifest, mode)
    _validate_resume(report, fingerprint, label)

    final_model = _mapping(
        report.get("final_model"),
        f"{label} final model",
    )
    reload_evidence = _mapping(
        final_model.get("reload_verification"),
        f"{label} final reload verification",
    )
    inventory = _mapping(
        final_model.get("inventory"),
        f"{label} final model inventory",
    )
    tree_sha256 = _prefixed_sha256(
        inventory.get("tree_sha256"),
        f"{label} final model tree SHA-256",
    )
    if (
        not isinstance(final_model.get("path"), str)
        or not final_model["path"]
        or final_model.get("run_fingerprint") != fingerprint
        or reload_evidence.get("status") != "passed"
        or reload_evidence.get("run_fingerprint") != fingerprint
    ):
        raise RemediationComparisonError(
            f"{label} lacks a fingerprint-bound passed final-model reload"
        )
    eval_metrics = _mapping(
        report.get("eval_metrics"),
        f"{label} eval metrics",
    )
    eval_loss = _positive_number(
        eval_metrics.get("eval_loss"),
        f"{label} eval loss",
    )
    return report, eval_loss, fingerprint, tree_sha256


def _validate_report_training_contract(
    report: Mapping[str, Any],
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
    mode: str,
) -> None:
    label = f"{mode} training report"
    expected_top = {
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "bf16": contract["bf16"],
        "seed": _mapping(
            report["remediation_order"],
            f"{label} remediation order",
        )["seed"],
        "method": "full_sft",
        "optimizer": contract["optimizer"],
        "learning_rate": contract["learning_rate"],
        "max_sequence_length": contract["max_sequence_length"],
        "micro_batch_size": contract["micro_batch_size"],
        "gradient_accumulation_steps": (
            contract["gradient_accumulation_steps"]
        ),
        "effective_batch_size": contract["effective_batch_size"],
        "early_stopping": contract["early_stopping"],
        "save_total_limit": contract["save_total_limit"],
    }
    for field, expected in expected_top.items():
        if report.get(field) != expected:
            raise RemediationComparisonError(
                f"{label} differs from the training contract at {field}"
            )
    bindings = _mapping(report.get("bindings"), f"{label} bindings")
    config = _mapping(bindings.get("config"), f"{label} config binding")
    if (
        _normalized_raw_sha256(
            config.get("sha256"),
            f"{label} config SHA-256",
        )
        != manifest["arms"][mode]["training_config"]["sha256"]
    ):
        raise RemediationComparisonError(
            f"{label} used a different training config artifact"
        )
    training_order = _mapping(
        bindings.get("training_order"),
        f"{label} training-order binding",
    )
    if (
        training_order.get("mode") != mode
        or _normalized_raw_sha256(
            training_order.get("plan_sha256"),
            f"{label} bound plan SHA-256",
        )
        != manifest["arms"][mode]["plan"]["sha256"]
        or training_order.get("training_contract") != contract
    ):
        raise RemediationComparisonError(
            f"{label} bound a different remediation plan/contract"
        )
    effective = _mapping(
        bindings.get("effective_training"),
        f"{label} effective training",
    )
    expected_effective = {
        "run_kind": "improvement",
        "method": "full_sft",
        "seed": report["seed"],
        "learning_rate": contract["learning_rate"],
        "max_sequence_length": contract["max_sequence_length"],
        "micro_batch_size": contract["micro_batch_size"],
        "gradient_accumulation_steps": (
            contract["gradient_accumulation_steps"]
        ),
        "effective_batch_size": contract["effective_batch_size"],
        "schedule": report["schedule"],
        "train_examples_loaded": report["train_examples_loaded"],
        "train_examples_used": report["train_examples_used"],
        "validation_examples_loaded": report[
            "validation_examples_loaded"
        ],
        "validation_examples_used": report["validation_examples_used"],
        "bf16": contract["bf16"],
        "gradient_checkpointing": contract[
            "gradient_checkpointing"
        ],
        "optimizer": contract["optimizer"],
        "sdpa": True,
        "completion_only_loss": contract[
            "assistant_output_loss_only"
        ],
        "save_total_limit": contract["save_total_limit"],
        "training_order_mode": mode,
    }
    if dict(effective) != expected_effective:
        raise RemediationComparisonError(
            f"{label} effective training differs from the report/contract"
        )


def _validate_report_dataset(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    mode: str,
) -> None:
    label = f"{mode} training report"
    selection = manifest["selection"]
    bindings = _mapping(report.get("bindings"), f"{label} bindings")
    dataset = _mapping(
        bindings.get("dataset"),
        f"{label} dataset binding",
    )
    dataset_manifest = _mapping(
        dataset.get("manifest"),
        f"{label} dataset-manifest binding",
    )
    train = _mapping(dataset.get("train"), f"{label} train binding")
    validation = _mapping(
        dataset.get("validation"),
        f"{label} validation binding",
    )
    if (
        _normalized_raw_sha256(
            dataset_manifest.get("sha256"),
            f"{label} dataset manifest SHA-256",
        )
        != selection["dataset_manifest"]["sha256"]
        or _normalized_raw_sha256(
            dataset_manifest.get("declared_train_sha256"),
            f"{label} declared train SHA-256",
        )
        != selection["train"]["sha256"]
        or _normalized_raw_sha256(
            dataset_manifest.get("declared_validation_sha256"),
            f"{label} declared validation SHA-256",
        )
        != selection["validation"]["sha256"]
        or dataset_manifest.get("declared_train_records")
        != selection["train"]["record_count"]
        or dataset_manifest.get("declared_validation_records")
        != selection["validation"]["record_count"]
        or _normalized_raw_sha256(
            train.get("sha256"),
            f"{label} train SHA-256",
        )
        != selection["train"]["sha256"]
        or train.get("total_records")
        != selection["train"]["record_count"]
        or train.get("selected_records")
        != selection["train"]["record_count"]
        or _normalized_raw_sha256(
            validation.get("sha256"),
            f"{label} validation SHA-256",
        )
        != selection["validation"]["sha256"]
        or validation.get("total_records")
        != selection["validation"]["record_count"]
        or validation.get("selected_records")
        != selection["validation"]["record_count"]
        or report.get("train_examples_loaded")
        != selection["train"]["record_count"]
        or report.get("train_examples_used")
        != selection["train"]["record_count"]
        or report.get("validation_examples_loaded")
        != selection["validation"]["record_count"]
        or report.get("validation_examples_used")
        != selection["validation"]["record_count"]
        or report.get("oversize_rejections")
        != {"train": 0, "validation": 0}
        or report.get("protection_rejections")
        != {"train": 0, "validation": 0}
    ):
        raise RemediationComparisonError(
            f"{label} did not consume the exact full matched cohort"
        )


def _validate_report_policy_parent(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    mode: str,
) -> None:
    label = f"{mode} training report"
    policy = manifest["inputs"]["protection_policy"]
    parent = manifest["inputs"]["parent_checkpoint"]
    expected_policy = {
        "artifact_sha256": policy["artifact_sha256"],
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
    }
    report_policy = _mapping(
        report.get("protection_policy"),
        f"{label} protection policy",
    )
    if (
        _normalized_raw_sha256(
            report_policy.get("sha256"),
            f"{label} policy artifact SHA-256",
        )
        != expected_policy["artifact_sha256"]
        or report_policy.get("policy_id")
        != expected_policy["policy_id"]
        or report_policy.get("policy_sha256")
        != expected_policy["policy_sha256"]
    ):
        raise RemediationComparisonError(
            f"{label} used a different protection policy"
        )
    bindings = _mapping(report.get("bindings"), f"{label} bindings")
    bound_policy = _mapping(
        bindings.get("protection_policy"),
        f"{label} bound protection policy",
    )
    if (
        _normalized_raw_sha256(
            bound_policy.get("sha256"),
            f"{label} bound policy artifact SHA-256",
        )
        != expected_policy["artifact_sha256"]
        or bound_policy.get("policy_id")
        != expected_policy["policy_id"]
        or bound_policy.get("policy_sha256")
        != expected_policy["policy_sha256"]
    ):
        raise RemediationComparisonError(
            f"{label} bound a different protection policy"
        )
    warm_start = _mapping(
        report.get("warm_start_parent_checkpoint"),
        f"{label} warm-start parent",
    )
    bound_parent = _mapping(
        bindings.get("parent_checkpoint"),
        f"{label} bound parent",
    )
    for evidence in (warm_start, bound_parent):
        if (
            evidence.get("tree_sha256") != parent["tree_sha256"]
            or evidence.get("relationship")
            != "warm_start_parent_not_resume"
        ):
            raise RemediationComparisonError(
                f"{label} used a different warm-start parent"
            )


def _validate_resume(
    report: Mapping[str, Any],
    fingerprint: str,
    label: str,
) -> None:
    resume = report.get("resume_from_checkpoint")
    if resume is None:
        return
    evidence = _mapping(resume, f"{label} resume evidence")
    if (
        evidence.get("run_fingerprint") != fingerprint
        or evidence.get("relationship", "same_run_identity_bound_recovery")
        != "same_run_identity_bound_recovery"
    ):
        raise RemediationComparisonError(
            f"{label} resume is not identity-bound same-run recovery"
        )


def _enrich_cases(
    references: Sequence[Mapping[str, Any]],
    predictions_path: Path,
) -> tuple[EvaluationCase, ...]:
    try:
        predictions = load_candidate_predictions(predictions_path)
        joined = join_evaluation_cases(references, predictions)
    except (OSError, TypeError, ValueError) as error:
        raise RemediationComparisonError(
            f"candidate predictions are invalid: {error}"
        ) from error
    semantic_plans = {
        str(reference["case_id"]): reference["semantic_plan"]
        for reference in references
    }
    address_modes = {
        str(reference["case_id"]): reference.get("address_mode")
        for reference in references
    }
    return tuple(
        replace(
            case,
            semantic_plan=semantic_plans[case.case_id],
            address_mode=address_modes[case.case_id],
        )
        for case in joined
    )


def _validate_prediction_manifest(
    manifest: Mapping[str, Any],
    *,
    loaded: _LoadedFile,
    mode: str,
    training: Mapping[str, Any],
    references_sha256: str,
    predictions_sha256: str,
    case_count: int,
    results: Sequence[CaseEvaluation],
    experiment: Mapping[str, Any],
) -> tuple[int, int]:
    label = f"{mode} prediction manifest"
    policy = experiment["inputs"]["protection_policy"]
    final_model = _mapping(
        training.get("final_model"),
        f"{mode} final model",
    )
    restoration_errors = _nonnegative_int(
        manifest.get("restoration_error_count"),
        f"{label} restoration error count",
    )
    valid_sst_count = _nonnegative_int(
        manifest.get("valid_sst_count"),
        f"{label} valid SST count",
    )
    raw_sst_valid_count = sum(result.sst_valid for result in results)
    if (
        manifest.get("schema_version") != 1
        or manifest.get("candidate") != mode
        or manifest.get("record_count") != case_count
        or restoration_errors > case_count
        or valid_sst_count + restoration_errors > case_count
        or valid_sst_count > raw_sst_valid_count
        or manifest.get("prediction_sha256") != predictions_sha256
        or manifest.get("reference_sha256") != references_sha256
        or Path(str(manifest.get("model"))).resolve()
        != Path(str(final_model.get("path"))).resolve()
        or manifest.get("quantization") != "none"
    ):
        raise RemediationComparisonError(
            f"{label} differs from its model/references/predictions"
        )
    protection = _mapping(
        manifest.get("protection_policy"),
        f"{label} protection policy",
    )
    if (
        protection.get("policy_id") != policy["policy_id"]
        or protection.get("policy_sha256") != policy["policy_sha256"]
        or _normalized_raw_sha256(
            protection.get("artifact_sha256"),
            f"{label} protection-policy artifact SHA-256",
        )
        != policy["artifact_sha256"]
    ):
        raise RemediationComparisonError(
            f"{label} used a different protection policy"
        )
    for field in (
        "total_generation_seconds",
        "total_input_tokens",
        "total_output_tokens",
    ):
        value = manifest.get(field)
        if field == "total_generation_seconds":
            if _finite_number(value, f"{label} {field}") < 0:
                raise RemediationComparisonError(
                    f"{label} {field} must be non-negative"
                )
        else:
            _nonnegative_int(value, f"{label} {field}")
    del loaded
    return restoration_errors, valid_sst_count


def _validate_evaluation_report(
    evaluation: Mapping[str, Any],
    *,
    mode: str,
    references_sha256: str,
    predictions_sha256: str,
    metrics: Mapping[str, Any],
) -> None:
    if (
        evaluation.get("schema_version") != 1
        or evaluation.get("candidate") != mode
        or evaluation.get("exact_case_coverage") is not True
        or evaluation.get("reference_sha256") != references_sha256
        or evaluation.get("prediction_sha256") != predictions_sha256
        or _RAW_SHA256.fullmatch(
            str(evaluation.get("evaluation_config_sha256", ""))
        )
        is None
    ):
        raise RemediationComparisonError(
            f"{mode} evaluation report has invalid input bindings"
        )
    materialized_metrics = json.loads(
        json.dumps(metrics, ensure_ascii=False, allow_nan=False)
    )
    if evaluation.get("metrics") != materialized_metrics:
        raise RemediationComparisonError(
            f"{mode} evaluation metrics differ from independent recomputation"
        )


def _load_arm(
    mode: str,
    artifacts: RemediationArmArtifacts,
    *,
    manifest: Mapping[str, Any],
    references_loaded: _LoadedFile,
    references: Sequence[Mapping[str, Any]],
) -> _ArmEvidence:
    training_loaded = _stable_bytes(
        artifacts.training_report,
        f"{mode} training report",
    )
    training, eval_loss, fingerprint, tree_sha256 = (
        _validate_training_report(
            training_loaded,
            mode=mode,
            manifest=manifest,
        )
    )
    predictions_loaded = _stable_bytes(
        artifacts.predictions,
        f"{mode} predictions",
    )
    cases = _enrich_cases(references, predictions_loaded.path)
    metrics = evaluate_predictions(cases)
    results = metrics.case_results
    aggregate = aggregate_case_evaluations(results)

    prediction_manifest_loaded = _stable_bytes(
        artifacts.prediction_manifest,
        f"{mode} prediction manifest",
    )
    prediction_manifest = _json_object(
        prediction_manifest_loaded,
        f"{mode} prediction manifest",
    )
    restoration_errors, valid_sst_count = _validate_prediction_manifest(
        prediction_manifest,
        loaded=prediction_manifest_loaded,
        mode=mode,
        training=training,
        references_sha256=references_loaded.sha256,
        predictions_sha256=predictions_loaded.sha256,
        case_count=len(references),
        results=results,
        experiment=manifest,
    )

    evaluation_loaded = _stable_bytes(
        artifacts.evaluation_report,
        f"{mode} evaluation report",
    )
    evaluation = _json_object(
        evaluation_loaded,
        f"{mode} evaluation report",
    )
    _validate_evaluation_report(
        evaluation,
        mode=mode,
        references_sha256=references_loaded.sha256,
        predictions_sha256=predictions_loaded.sha256,
        metrics=asdict(metrics),
    )
    return _ArmEvidence(
        mode=mode,
        training=training,
        training_sha256=training_loaded.sha256,
        predictions_sha256=predictions_loaded.sha256,
        prediction_manifest=prediction_manifest,
        prediction_manifest_sha256=prediction_manifest_loaded.sha256,
        evaluation=evaluation,
        evaluation_sha256=evaluation_loaded.sha256,
        cases=cases,
        results=results,
        aggregate=aggregate,
        eval_loss=eval_loss,
        restoration_error_count=restoration_errors,
        valid_sst_count=valid_sst_count,
        run_fingerprint=fingerprint,
        final_model_tree_sha256=tree_sha256,
    )


def _normalized_mapping(
    value: Mapping[str, Any],
    omitted: frozenset[str],
) -> dict[str, Any]:
    return {
        str(key): item
        for key, item in value.items()
        if key not in omitted
    }


def _require_matched_runs(
    shuffled: _ArmEvidence,
    staged: _ArmEvidence,
) -> dict[str, bool]:
    if shuffled.run_fingerprint == staged.run_fingerprint:
        raise RemediationComparisonError(
            "remediation arm run fingerprints must be new and distinct"
        )
    for field in _MATCHED_TOP_LEVEL_FIELDS:
        if field not in shuffled.training or field not in staged.training:
            raise RemediationComparisonError(
                f"matched remediation reports require {field}"
            )
        if shuffled.training[field] != staged.training[field]:
            raise RemediationComparisonError(
                f"remediation reports differ at matched field {field}"
            )
    shuffled_bindings = _mapping(
        shuffled.training["bindings"],
        "shuffled bindings",
    )
    staged_bindings = _mapping(
        staged.training["bindings"],
        "staged bindings",
    )
    for field in (
        "dataset",
        "code",
        "tokenizer",
        "runtime",
        "protection_policy",
        "protection_application",
        "parent_checkpoint",
    ):
        if shuffled_bindings.get(field) != staged_bindings.get(field):
            raise RemediationComparisonError(
                f"remediation run bindings differ at {field}"
            )
    effective_omissions = frozenset({"training_order_mode"})
    if _normalized_mapping(
        _mapping(
            shuffled_bindings.get("effective_training"),
            "shuffled effective training",
        ),
        effective_omissions,
    ) != _normalized_mapping(
        _mapping(
            staged_bindings.get("effective_training"),
            "staged effective training",
        ),
        effective_omissions,
    ):
        raise RemediationComparisonError(
            "effective training differs beyond the intended order mode"
        )
    config_omissions = frozenset(
        {
            "training_order_mode",
            "training_order_plan_path",
            "training_order_plan_sha256",
        }
    )
    for mode, bindings in (
        ("shuffled", shuffled_bindings),
        ("staged", staged_bindings),
    ):
        config = _mapping(bindings["config"], f"{mode} config binding")
        values = _mapping(config.get("values"), f"{mode} config values")
        if values.get("training_order_mode") != mode:
            raise RemediationComparisonError(
                f"{mode} config values bind a different order mode"
            )
    shuffled_config = _mapping(
        _mapping(shuffled_bindings["config"], "shuffled config").get(
            "values"
        ),
        "shuffled config values",
    )
    staged_config = _mapping(
        _mapping(staged_bindings["config"], "staged config").get("values"),
        "staged config values",
    )
    if _normalized_mapping(
        shuffled_config,
        config_omissions,
    ) != _normalized_mapping(staged_config, config_omissions):
        raise RemediationComparisonError(
            "training configs differ beyond the intended order fields"
        )
    cli_omissions = frozenset(
        {"config", "output_dir", "training_order_plan"}
    )
    if _normalized_mapping(
        _mapping(shuffled_bindings.get("cli"), "shuffled CLI binding"),
        cli_omissions,
    ) != _normalized_mapping(
        _mapping(staged_bindings.get("cli"), "staged CLI binding"),
        cli_omissions,
    ):
        raise RemediationComparisonError(
            "training invocations differ beyond arm-local paths"
        )
    shuffled_order = _mapping(
        shuffled.training["remediation_order"],
        "shuffled remediation order",
    )
    staged_order = _mapping(
        staged.training["remediation_order"],
        "staged remediation order",
    )
    for field in (
        "safe_example_ids_sha256",
        "selected_policy_rejected_example_ids_sha256",
        "sequence_rejected_example_ids_sha256",
        "used_example_ids_sha256",
        "training_contract",
        "parent_checkpoint",
        "protection_policy",
    ):
        if shuffled_order.get(field) != staged_order.get(field):
            raise RemediationComparisonError(
                f"effective remediation cohorts differ at {field}"
            )
    if [case.case_id for case in shuffled.cases] != [
        case.case_id for case in staged.cases
    ]:
        raise RemediationComparisonError(
            "remediation arms do not cover identical ordered parity-dev cases"
        )
    if (
        shuffled.evaluation.get("evaluation_config_sha256")
        != staged.evaluation.get("evaluation_config_sha256")
    ):
        raise RemediationComparisonError(
            "remediation arms used different evaluation configs"
        )
    return {
        "accepted_training_ids_equal": True,
        "rejected_training_ids_equal": True,
        "training_contract_equal": True,
        "parent_checkpoint_equal": True,
        "protection_policy_equal": True,
        "parity_dev_case_ids_equal": True,
        "evaluation_config_equal": True,
    }


def _protected_integrity(
    evidence: _ArmEvidence,
) -> dict[str, Any]:
    total = len(evidence.results)
    exact_rate = float(evidence.aggregate["protected_span_exact_rate"])
    zero_restoration_errors = evidence.restoration_error_count == 0
    exact_final_sequence = math.isclose(
        exact_rate,
        1.0,
        rel_tol=0.0,
        abs_tol=_EPSILON,
    )
    marker_value: int | None = 0 if zero_restoration_errors else None
    reorder_value: int | None = 0 if exact_final_sequence else None
    passed = zero_restoration_errors and exact_final_sequence
    return {
        "successful_restoration_attempt_rate": (
            (total - evidence.restoration_error_count) / total
        ),
        "restoration_error_count": evidence.restoration_error_count,
        "protected_span_exact_rate": exact_rate,
        "deletions": marker_value,
        "duplications": marker_value,
        "malformed": marker_value,
        "reordered": reorder_value,
        "classification": (
            "proven_zero"
            if passed
            else "unclassified_failure_no_zero_claim"
        ),
        "passed": passed,
    }


def _comparison_report(
    *,
    manifest_loaded: _LoadedFile,
    manifest: Mapping[str, Any],
    references_loaded: _LoadedFile,
    case_ids_sha256: str,
    policy: Mapping[str, Any],
    shuffled: _ArmEvidence,
    staged: _ArmEvidence,
    matched: Mapping[str, bool],
) -> dict[str, Any]:
    best_loss = min(shuffled.eval_loss, staged.eval_loss)
    shuffled_loss_score = best_loss / shuffled.eval_loss
    staged_loss_score = best_loss / staged.eval_loss
    weights = policy["weights"]
    shuffled_score = score_comparison_candidate(
        shuffled.aggregate,
        eval_loss_score=shuffled_loss_score,
        weights=weights,
    )
    staged_score = score_comparison_candidate(
        staged.aggregate,
        eval_loss_score=staged_loss_score,
        weights=weights,
    )
    delta = float(staged_score["composite"]) - float(
        shuffled_score["composite"]
    )
    loss_ratio = staged.eval_loss / shuffled.eval_loss

    critical_names = sorted(
        set(shuffled.aggregate["critical_errors"])
        | set(staged.aggregate["critical_errors"])
    )
    shuffled_results = {
        result.case_id: result for result in shuffled.results
    }
    critical_checks: dict[str, dict[str, Any]] = {}
    new_critical_count = 0
    for name in critical_names:
        control = int(
            shuffled.aggregate["critical_errors"].get(name, 0)
        )
        candidate = int(staged.aggregate["critical_errors"].get(name, 0))
        new_case_ids = sorted(
            result.case_id
            for result in staged.results
            if name in result.critical_errors
            and name
            not in shuffled_results[result.case_id].critical_errors
        )
        added = len(new_case_ids)
        new_critical_count += added
        critical_checks[name] = {
            "shuffled": control,
            "staged": candidate,
            "new": added,
            "new_case_ids": new_case_ids,
            "new_case_ids_sha256": hash_identifier_sequence(
                new_case_ids
            ),
            "passed": added == 0,
        }
    sst_check = {
        "shuffled": float(shuffled.aggregate["sst_parse_rate"]),
        "staged": float(staged.aggregate["sst_parse_rate"]),
    }
    sst_check["passed"] = (
        sst_check["staged"] + _EPSILON >= sst_check["shuffled"]
    )
    quality_checks = {
        name: {
            "shuffled": float(shuffled.aggregate[name]),
            "staged": float(staged.aggregate[name]),
            "passed": (
                float(staged.aggregate[name]) + _EPSILON
                >= float(shuffled.aggregate[name])
            ),
        }
        for name in _QUALITY_METRICS
    }
    shuffled_integrity = _protected_integrity(shuffled)
    staged_integrity = _protected_integrity(staged)
    protected_passed = bool(
        shuffled_integrity["passed"] and staged_integrity["passed"]
    )
    hard_passed = (
        protected_passed
        and new_critical_count == 0
        and bool(sst_check["passed"])
        and all(
            bool(check["passed"])
            for check in quality_checks.values()
        )
    )

    bootstrap = paired_bootstrap_composite_delta(
        shuffled.results,
        staged.results,
        control_loss_score=shuffled_loss_score,
        staged_loss_score=staged_loss_score,
        weights=weights,
        seed=int(policy["seed"]),
        resamples=int(policy["bootstrap_resamples"]),
        confidence_level=float(policy["confidence_level"]),
    )
    bootstrap_passed = float(bootstrap["lower"]) > 0.0
    bootstrap["passed"] = bootstrap_passed
    checks = {
        "matched_execution": all(matched.values()),
        "hard_gates": hard_passed,
        "eval_loss": (
            loss_ratio
            <= float(policy["maximum_eval_loss_ratio"]) + _EPSILON
        ),
        "minimum_composite_delta": (
            delta + _EPSILON
            >= float(policy["minimum_composite_delta"])
        ),
        "bootstrap_lower_bound": bootstrap_passed,
    }
    adopt = all(checks.values())

    candidates: dict[str, Any] = {}
    for evidence, score in (
        (shuffled, shuffled_score),
        (staged, staged_score),
    ):
        candidates[evidence.mode] = {
            "run_fingerprint": evidence.run_fingerprint,
            "final_model_tree_sha256": (
                evidence.final_model_tree_sha256
            ),
            "actual_completed_steps": evidence.training[
                "checkpoint_state"
            ]["global_step"],
            "eval_loss": evidence.eval_loss,
            "valid_sst_count": evidence.valid_sst_count,
            "aggregate": evidence.aggregate,
            "components": score["components"],
            "composite": score["composite"],
        }
    return {
        "schema_version": 1,
        "kind": "scriber_remediation_comparison",
        "status": "complete",
        "decision": "ADOPT_STAGED" if adopt else "KEEP_SHUFFLED",
        "adoption": "STAGED" if adopt else "NO_ADOPTION",
        "experiment": {
            "experiment_id": manifest["experiment_id"],
            "comparison_pair_sha256": (
                manifest["comparison_pair_sha256"]
            ),
            "bundle_manifest_sha256": manifest_loaded.sha256,
            "evaluation_scope": "validation_only_parity_dev",
        },
        "inputs": {
            "parity_dev_manifest_sha256": (
                manifest["parity_dev"]["manifest"]["sha256"]
            ),
            "references_sha256": references_loaded.sha256,
            "case_ids_sha256": case_ids_sha256,
            "case_count": len(shuffled.cases),
            "comparison_config_sha256": (
                manifest["adoption_policy"]["comparison_config"][
                    "sha256"
                ]
            ),
            "arms": {
                evidence.mode: {
                    "training_report_sha256": (
                        evidence.training_sha256
                    ),
                    "predictions_sha256": (
                        evidence.predictions_sha256
                    ),
                    "prediction_manifest_sha256": (
                        evidence.prediction_manifest_sha256
                    ),
                    "evaluation_report_sha256": (
                        evidence.evaluation_sha256
                    ),
                    "evaluation_config_sha256": (
                        evidence.evaluation[
                            "evaluation_config_sha256"
                        ]
                    ),
                }
                for evidence in (shuffled, staged)
            },
        },
        "matched_execution": {
            "passed": all(matched.values()),
            "assertions": dict(matched),
        },
        "policy": {
            "weights": dict(weights),
            "bootstrap_resamples": policy["bootstrap_resamples"],
            "confidence_level": policy["confidence_level"],
            "minimum_composite_delta": (
                policy["minimum_composite_delta"]
            ),
            "maximum_eval_loss_ratio": (
                policy["maximum_eval_loss_ratio"]
            ),
            "required_bootstrap_lower_bound": 0.0,
        },
        "candidates": candidates,
        "hard_gates": {
            "passed": hard_passed,
            "protected_span_integrity": {
                "passed": protected_passed,
                "shuffled": shuffled_integrity,
                "staged": staged_integrity,
            },
            "new_critical_errors": {
                "actual": new_critical_count,
                "maximum": 0,
                "checks": critical_checks,
                "passed": new_critical_count == 0,
            },
            "sst_parse_rate_noninferior": sst_check,
            "quality_metrics_noninferior": {
                "passed": all(
                    bool(check["passed"])
                    for check in quality_checks.values()
                ),
                "checks": quality_checks,
            },
        },
        "eval_loss": {
            "shuffled": shuffled.eval_loss,
            "staged": staged.eval_loss,
            "ratio": loss_ratio,
            "maximum_ratio": policy["maximum_eval_loss_ratio"],
            "passed": checks["eval_loss"],
        },
        "composite_delta": {
            "actual": delta,
            "minimum": policy["minimum_composite_delta"],
            "passed": checks["minimum_composite_delta"],
        },
        "bootstrap": bootstrap,
        "checks": checks,
    }


def validate_remediation_comparison_report(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate report shape and recompute all decision-level assertions."""

    try:
        materialized = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise RemediationComparisonError(
            "remediation comparison report requires JSON-safe finite values"
        ) from error
    errors = sorted(
        _report_validator().iter_errors(materialized),
        key=lambda error: list(error.path),
    )
    if errors:
        raise RemediationComparisonError(
            "remediation comparison report schema failed: "
            f"{errors[0].message}"
        )
    weights = materialized["policy"]["weights"]
    if weights != _EXPECTED_WEIGHTS:
        raise RemediationComparisonError(
            "remediation comparison report changed fixed weights"
        )
    for candidate in materialized["candidates"].values():
        expected = sum(
            float(weights[name]) * float(candidate["components"][name])
            for name in weights
        )
        if not math.isclose(
            float(candidate["composite"]),
            expected,
            rel_tol=0.0,
            abs_tol=_EPSILON,
        ):
            raise RemediationComparisonError(
                "candidate composite is inconsistent"
            )
    matched = materialized["matched_execution"]
    if matched["passed"] != all(matched["assertions"].values()):
        raise RemediationComparisonError(
            "matched-execution summary is inconsistent"
        )
    hard = materialized["hard_gates"]
    protected = hard["protected_span_integrity"]
    case_count = materialized["inputs"]["case_count"]
    for mode in ("shuffled", "staged"):
        integrity = protected[mode]
        expected_rate = (
            case_count - integrity["restoration_error_count"]
        ) / case_count
        restoration_is_exact = math.isclose(
            integrity["successful_restoration_attempt_rate"],
            expected_rate,
            rel_tol=0.0,
            abs_tol=_EPSILON,
        )
        zero_restoration_errors = (
            integrity["restoration_error_count"] == 0
        )
        exact_sequence = math.isclose(
            integrity["protected_span_exact_rate"],
            1.0,
            rel_tol=0.0,
            abs_tol=_EPSILON,
        )
        marker_counts = (
            integrity["deletions"],
            integrity["duplications"],
            integrity["malformed"],
        )
        if zero_restoration_errors:
            marker_evidence_consistent = marker_counts == (0, 0, 0)
        else:
            marker_evidence_consistent = marker_counts == (
                None,
                None,
                None,
            )
        reorder_evidence_consistent = integrity["reordered"] == (
            0 if exact_sequence else None
        )
        expected_pass = zero_restoration_errors and exact_sequence
        expected_classification = (
            "proven_zero"
            if expected_pass
            else "unclassified_failure_no_zero_claim"
        )
        if (
            not restoration_is_exact
            or not marker_evidence_consistent
            or not reorder_evidence_consistent
            or integrity["passed"] != expected_pass
            or integrity["classification"]
            != expected_classification
        ):
            raise RemediationComparisonError(
                f"{mode} protected-span evidence is inconsistent"
            )
    if protected["passed"] != all(
        protected[mode]["passed"] for mode in ("shuffled", "staged")
    ):
        raise RemediationComparisonError(
            "protected-span summary is inconsistent"
        )
    critical = hard["new_critical_errors"]
    expected_new = sum(
        check["new"] for check in critical["checks"].values()
    )
    if any(
        check["new_case_ids"] != sorted(check["new_case_ids"])
        or len(set(check["new_case_ids"]))
        != len(check["new_case_ids"])
        or check["new"] != len(check["new_case_ids"])
        or check["new_case_ids_sha256"]
        != hash_identifier_sequence(check["new_case_ids"])
        or check["new"]
        < max(0, check["staged"] - check["shuffled"])
        or check["passed"] != (check["new"] == 0)
        for check in critical["checks"].values()
    ):
        raise RemediationComparisonError(
            "per-error critical checks are inconsistent"
        )
    if (
        critical["actual"] != expected_new
        or critical["passed"] != (expected_new == 0)
    ):
        raise RemediationComparisonError(
            "new-critical-error summary is inconsistent"
        )
    sst = hard["sst_parse_rate_noninferior"]
    if sst["passed"] != (
        sst["staged"] + _EPSILON >= sst["shuffled"]
    ):
        raise RemediationComparisonError(
            "SST non-inferiority check is inconsistent"
        )
    quality = hard["quality_metrics_noninferior"]
    for check in quality["checks"].values():
        if check["passed"] != (
            check["staged"] + _EPSILON >= check["shuffled"]
        ):
            raise RemediationComparisonError(
                "quality non-inferiority check is inconsistent"
            )
    if quality["passed"] != all(
        check["passed"] for check in quality["checks"].values()
    ):
        raise RemediationComparisonError(
            "quality non-inferiority summary is inconsistent"
        )
    expected_hard = (
        protected["passed"]
        and critical["passed"]
        and sst["passed"]
        and quality["passed"]
    )
    if hard["passed"] != expected_hard:
        raise RemediationComparisonError(
            "hard-gate summary is inconsistent"
        )
    loss = materialized["eval_loss"]
    if not math.isclose(
        loss["ratio"],
        loss["staged"] / loss["shuffled"],
        rel_tol=0.0,
        abs_tol=_EPSILON,
    ) or loss["passed"] != (
        loss["ratio"] <= loss["maximum_ratio"] + _EPSILON
    ):
        raise RemediationComparisonError(
            "eval-loss check is inconsistent"
        )
    delta = materialized["composite_delta"]
    expected_delta = (
        materialized["candidates"]["staged"]["composite"]
        - materialized["candidates"]["shuffled"]["composite"]
    )
    if not math.isclose(
        delta["actual"],
        expected_delta,
        rel_tol=0.0,
        abs_tol=_EPSILON,
    ) or delta["passed"] != (
        delta["actual"] + _EPSILON >= delta["minimum"]
    ):
        raise RemediationComparisonError(
            "composite-delta check is inconsistent"
        )
    bootstrap = materialized["bootstrap"]
    if (
        not bootstrap["lower"]
        <= bootstrap["median"]
        <= bootstrap["upper"]
        or bootstrap["passed"] != (bootstrap["lower"] > 0.0)
    ):
        raise RemediationComparisonError(
            "bootstrap interval/check is inconsistent"
        )
    checks = materialized["checks"]
    expected_checks = {
        "matched_execution": matched["passed"],
        "hard_gates": hard["passed"],
        "eval_loss": loss["passed"],
        "minimum_composite_delta": delta["passed"],
        "bootstrap_lower_bound": bootstrap["passed"],
    }
    if checks != expected_checks:
        raise RemediationComparisonError(
            "decision checks are inconsistent"
        )
    adopted = all(checks.values())
    expected_decision = (
        "ADOPT_STAGED" if adopted else "KEEP_SHUFFLED"
    )
    expected_adoption = "STAGED" if adopted else "NO_ADOPTION"
    if (
        materialized["decision"] != expected_decision
        or materialized["adoption"] != expected_adoption
    ):
        raise RemediationComparisonError(
            "adoption decision differs from binding checks"
        )
    shuffled_candidate = materialized["candidates"]["shuffled"]
    staged_candidate = materialized["candidates"]["staged"]
    if (
        shuffled_candidate["run_fingerprint"]
        == staged_candidate["run_fingerprint"]
        or shuffled_candidate["actual_completed_steps"]
        != staged_candidate["actual_completed_steps"]
        or shuffled_candidate["valid_sst_count"] > case_count
        or staged_candidate["valid_sst_count"] > case_count
    ):
        raise RemediationComparisonError(
            "candidate identity/completion evidence is inconsistent"
        )
    return materialized


def compare_remediation_experiment(
    bundle_manifest: str | Path,
    *,
    references: str | Path,
    shuffled: RemediationArmArtifacts,
    staged: RemediationArmArtifacts,
) -> dict[str, Any]:
    """Compare two real completed arms without accepting Pilot placeholders."""

    (
        manifest_loaded,
        manifest,
        references_loaded,
        reference_records,
        case_ids_sha256,
        policy,
    ) = _load_bundle(bundle_manifest, references)
    shuffled_evidence = _load_arm(
        "shuffled",
        shuffled,
        manifest=manifest,
        references_loaded=references_loaded,
        references=reference_records,
    )
    staged_evidence = _load_arm(
        "staged",
        staged,
        manifest=manifest,
        references_loaded=references_loaded,
        references=reference_records,
    )
    matched = _require_matched_runs(
        shuffled_evidence,
        staged_evidence,
    )
    report = _comparison_report(
        manifest_loaded=manifest_loaded,
        manifest=manifest,
        references_loaded=references_loaded,
        case_ids_sha256=case_ids_sha256,
        policy=policy,
        shuffled=shuffled_evidence,
        staged=staged_evidence,
        matched=matched,
    )
    return validate_remediation_comparison_report(report)


def write_remediation_comparison_report(
    report: Mapping[str, Any],
    destination: str | Path,
) -> Path:
    """Write one immutable validated adoption report."""

    return write_immutable_json(
        destination,
        validate_remediation_comparison_report(report),
        label="remediation comparison report",
    )

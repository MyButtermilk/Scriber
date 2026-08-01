"""Fail-closed single-arm acceptance for the lexical marker repair."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .curriculum import (
    canonical_json_bytes,
    hash_identifier_sequence,
    write_immutable_json,
)
from .evaluation_io import (
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)
from .marker_repair import (
    MarkerRepairError,
    validate_marker_repair_manifest,
)
from .metrics import CaseEvaluation, EvaluationCase, evaluate_predictions

_PROJECT_ROOT = Path(__file__).parents[2]
_REPORT_SCHEMA = _PROJECT_ROOT / "contracts" / "marker_repair_acceptance_report_schema.json"
_EXPECTED_EXPERIMENT_ID = "pilot-marker-lexical-repair-v1"
_EXPECTED_UPDATE_STEPS = 146
_EXPECTED_EFFECTIVE_ORDER_SHA256 = "50daec69ef0ab52c38393d045551122bf339fae33536cd1ca432edf7e8e4e95f"
_EXPECTED_PARENT_TREE_SHA256 = "sha256:f3186be5e03b48dea35063f8ea502b2c4849abc03a9b8c4b41753bdcf7141513"
_EXPECTED_POLICY_ID = "gemma_lexical_v1"
_EXPECTED_POLICY_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_CASE_COUNT = 200
_QUALITY_METRICS = (
    "identity_exact_match",
    "punctuation_f1",
    "filler_f1",
    "repetition_cleanup_f1",
    "normalized_exact_match",
)
_EPSILON = 1e-12


class MarkerRepairAcceptanceError(ValueError):
    """Required marker-repair evidence is missing or inconsistent."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise MarkerRepairAcceptanceError(f"duplicate acceptance-policy key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@cache
def _report_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_REPORT_SCHEMA.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarkerRepairAcceptanceError(f"cannot load marker-repair acceptance schema: {error}") from error
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_bytes(path: str | Path, label: str) -> tuple[Path, bytes, str]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise MarkerRepairAcceptanceError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.expanduser().resolve()
    if not resolved.is_file():
        raise MarkerRepairAcceptanceError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise MarkerRepairAcceptanceError(f"cannot read {label}: {error}") from error
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
        raise MarkerRepairAcceptanceError(f"{label} changed while it was being read")
    return resolved, payload, _sha256(payload)


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MarkerRepairAcceptanceError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise MarkerRepairAcceptanceError(f"{label} must contain a JSON object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarkerRepairAcceptanceError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list | tuple):
        raise MarkerRepairAcceptanceError(f"{label} must be a sequence")
    return value


def _safe_bundle_file(
    root: Path,
    value: object,
    label: str,
) -> Path:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise MarkerRepairAcceptanceError(f"{label} must name one bundle-local file")
    return root / value


def _bound_bundle_artifact(
    root: Path,
    binding: Mapping[str, Any],
    label: str,
    *,
    path_field: str,
) -> tuple[Path, bytes, str]:
    path = _safe_bundle_file(root, binding.get(path_field), label)
    resolved, payload, actual = _stable_bytes(path, label)
    expected = binding.get("sha256")
    if actual != expected:
        raise MarkerRepairAcceptanceError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")
    return resolved, payload, actual


def _validate_bundle(
    bundle_dir: Path,
) -> tuple[dict[str, Any], bytes, str]:
    _, payload, manifest_sha256 = _stable_bytes(
        bundle_dir / "repair-manifest.json",
        "marker repair manifest",
    )
    value = _json_object(payload, "marker repair manifest")
    try:
        manifest = validate_marker_repair_manifest(value)
    except (MarkerRepairError, TypeError, ValueError) as error:
        raise MarkerRepairAcceptanceError(f"marker repair manifest is invalid: {error}") from error
    if payload != canonical_json_bytes(manifest):
        raise MarkerRepairAcceptanceError("marker repair manifest is not canonical immutable JSON")
    if (
        manifest["experiment_id"] != _EXPECTED_EXPERIMENT_ID
        or manifest["schedule"]["update_steps"] != _EXPECTED_UPDATE_STEPS
        or manifest["order_lock"]["source_effective_order_sha256"] != _EXPECTED_EFFECTIVE_ORDER_SHA256
        or manifest["parent_checkpoint"]["tree_sha256"] != _EXPECTED_PARENT_TREE_SHA256
    ):
        raise MarkerRepairAcceptanceError("marker repair manifest differs from the frozen pilot contract")
    policy = manifest["policies"]["training"]
    if (
        policy["policy_id"] != _EXPECTED_POLICY_ID
        or policy["policy_sha256"] != _EXPECTED_POLICY_SHA256
        or policy["pair_mapping"] != "ordered_occurrence_v1"
    ):
        raise MarkerRepairAcceptanceError("marker repair manifest does not bind gemma_lexical_v1")

    seen_filenames: set[str] = set()
    for label, binding_value in manifest["artifacts"].items():
        binding = _mapping(binding_value, f"{label} artifact binding")
        filename = binding.get("filename")
        if filename in seen_filenames:
            raise MarkerRepairAcceptanceError(f"marker repair artifact filename is bound twice: {filename}")
        seen_filenames.add(str(filename))
        _bound_bundle_artifact(
            bundle_dir,
            binding,
            f"marker repair artifact {label}",
            path_field="filename",
        )

    training_policy_path = _safe_bundle_file(
        bundle_dir,
        policy["filename"],
        "training policy",
    )
    _, policy_payload, policy_artifact_sha256 = _stable_bytes(
        training_policy_path,
        "training policy",
    )
    if policy_artifact_sha256 != policy["artifact_sha256"]:
        raise MarkerRepairAcceptanceError("training policy bytes differ from their manifest binding")
    policy_value = _json_object(policy_payload, "training policy")
    if (
        policy_value.get("policy_id") != _EXPECTED_POLICY_ID
        or policy_value.get("policy_sha256") != _EXPECTED_POLICY_SHA256
    ):
        raise MarkerRepairAcceptanceError("training policy content differs from gemma_lexical_v1")
    return manifest, payload, manifest_sha256


def _validate_acceptance_policy(
    bundle_dir: Path,
    manifest: Mapping[str, Any],
) -> str:
    matches = [binding for binding in manifest["artifacts"].values() if binding["filename"] == "acceptance-policy.yaml"]
    if len(matches) != 1:
        raise MarkerRepairAcceptanceError("acceptance-policy.yaml must have exactly one artifact binding")
    _, payload, policy_sha256 = _bound_bundle_artifact(
        bundle_dir,
        _mapping(matches[0], "acceptance policy binding"),
        "acceptance policy",
        path_field="filename",
    )
    try:
        value = yaml.load(
            payload.decode("utf-8"),
            Loader=_UniqueKeyLoader,
        )
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise MarkerRepairAcceptanceError(f"acceptance policy is invalid YAML: {error}") from error
    expected = {
        "schema_version": 1,
        "kind": "scriber_marker_repair_acceptance_policy",
        "decision": "accept_only_if_all_hard_gates_pass",
        "case_scope": "validation_only_parity_dev",
        "case_count": _CASE_COUNT,
        "hard_gates": {
            "raw_sst_parse_rate": 1.0,
            "protected_span_restoration_rate": 1.0,
            "protected_span_deletions": 0,
            "protected_span_duplications": 0,
            "protected_span_malformed": 0,
            "protected_span_reordered": 0,
            "new_critical_errors": 0,
            "quality_metrics_noninferior": True,
        },
        "fallback": "reject_repair_keep_frozen_shuffled_control",
    }
    if value != expected:
        raise MarkerRepairAcceptanceError("acceptance policy differs from the frozen all-hard-gates contract")
    return policy_sha256


def _safe_result_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise MarkerRepairAcceptanceError(f"{label} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or str(path) != value:
        raise MarkerRepairAcceptanceError(f"{label} must be a normalized relative POSIX path")
    return path


def _validate_cloud_inventory(
    cloud_output_dir: Path,
) -> tuple[dict[str, Any], str]:
    _, payload, cloud_result_sha256 = _stable_bytes(
        cloud_output_dir / "cloud-result.json",
        "cloud completion result",
    )
    result = _json_object(payload, "cloud completion result")
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_marker_repair_result"
        or result.get("training_completed") is not True
        or result.get("evaluation_completed") is not True
        or result.get("evaluation_gate_exit") not in (0, 2)
    ):
        raise MarkerRepairAcceptanceError("cloud completion result is not a completed marker-repair run")
    declared = _mapping(result.get("files"), "cloud result files")
    actual_paths: set[str] = set()
    for path in cloud_output_dir.rglob("*"):
        if path.is_symlink():
            raise MarkerRepairAcceptanceError(f"cloud output must not contain symlinks: {path}")
        if path.is_file() and path.name != "cloud-result.json":
            actual_paths.add(path.relative_to(cloud_output_dir).as_posix())
    if set(declared) != actual_paths:
        raise MarkerRepairAcceptanceError("cloud result inventory differs from downloaded output files")
    required = {
        "repair-manifest.json",
        "training-report.json",
        "predictions.jsonl",
        "predictions.manifest.json",
        "evaluation.json",
        "acceptance-policy.yaml",
        "preflight.json",
    }
    if not required.issubset(actual_paths):
        missing = sorted(required - actual_paths)
        raise MarkerRepairAcceptanceError(f"cloud result is missing required artifact: {missing[0]}")
    for name, evidence_value in declared.items():
        relative = _safe_result_relative(name, "cloud result file")
        evidence = _mapping(evidence_value, f"cloud result file {name}")
        _, file_payload, file_sha256 = _stable_bytes(
            cloud_output_dir.joinpath(*relative.parts),
            f"cloud result file {name}",
        )
        if evidence.get("bytes") != len(file_payload) or evidence.get("sha256") != file_sha256:
            raise MarkerRepairAcceptanceError(f"cloud result file differs from inventory: {name}")
    final_directory = result.get("final_model_directory")
    if (
        not isinstance(final_directory, str)
        or not final_directory.startswith("final-")
        or Path(final_directory).name != final_directory
    ):
        raise MarkerRepairAcceptanceError("cloud result final model directory is invalid")
    model_dir = cloud_output_dir / "training" / final_directory
    if not model_dir.is_dir() or not any(path.is_file() for path in model_dir.rglob("*")):
        raise MarkerRepairAcceptanceError("cloud result final model directory is missing or empty")
    return result, cloud_result_sha256


def _load_bound_baseline(
    bundle_dir: Path,
    manifest: Mapping[str, Any],
    label: str,
) -> tuple[Path, bytes, str]:
    binding = _mapping(
        manifest["baseline"][label],
        f"baseline {label} binding",
    )
    return _bound_bundle_artifact(
        bundle_dir,
        binding,
        f"baseline {label}",
        path_field="path",
    )


def _enriched_cases(
    references: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> tuple[EvaluationCase, ...]:
    by_id = {str(reference["case_id"]): reference for reference in references}
    return tuple(
        replace(
            case,
            semantic_plan=by_id[case.case_id]["semantic_plan"],
            address_mode=by_id[case.case_id].get("address_mode"),
        )
        for case in join_evaluation_cases(references, predictions)
    )


def _validate_evaluation(
    evaluation: Mapping[str, Any],
    *,
    label: str,
    candidate: str,
    references_sha256: str,
    predictions_sha256: str,
    cases: Sequence[EvaluationCase],
) -> tuple[CaseEvaluation, ...]:
    recomputed = evaluate_predictions(cases)
    serialized_metrics = json.loads(json.dumps(asdict(recomputed), ensure_ascii=False))
    if (
        evaluation.get("schema_version") != 1
        or evaluation.get("candidate") != candidate
        or evaluation.get("reference_sha256") != references_sha256
        or evaluation.get("prediction_sha256") != predictions_sha256
        or evaluation.get("exact_case_coverage") is not True
        or evaluation.get("metrics") != serialized_metrics
        or not isinstance(evaluation.get("gate"), Mapping)
    ):
        raise MarkerRepairAcceptanceError(f"{label} evaluation differs from deterministic recomputation")
    return recomputed.case_results


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarkerRepairAcceptanceError(f"{label} must be a non-negative integer")
    return value


def _validate_baseline_manifest(
    value: Mapping[str, Any],
    *,
    references_sha256: str,
    predictions_sha256: str,
) -> None:
    if (
        value.get("schema_version") != 1
        or value.get("candidate") != "shuffled"
        or value.get("record_count") != _CASE_COUNT
        or value.get("reference_sha256") != references_sha256
        or value.get("prediction_sha256") != predictions_sha256
    ):
        raise MarkerRepairAcceptanceError("frozen shuffled prediction manifest is inconsistent")
    restoration = _require_nonnegative_int(
        value.get("restoration_error_count"),
        "baseline restoration error count",
    )
    valid = _require_nonnegative_int(
        value.get("valid_sst_count"),
        "baseline valid SST count",
    )
    if restoration > _CASE_COUNT or valid > _CASE_COUNT:
        raise MarkerRepairAcceptanceError("baseline prediction counts exceed parity scope")


def _validate_candidate_manifest(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    references_sha256: str,
    predictions_sha256: str,
) -> tuple[int, int]:
    policy = manifest["policies"]["training"]
    bound_policy = _mapping(
        value.get("protection_policy"),
        "candidate prediction protection policy",
    )
    restoration = _require_nonnegative_int(
        value.get("restoration_error_count"),
        "candidate restoration error count",
    )
    valid = _require_nonnegative_int(
        value.get("valid_sst_count"),
        "candidate valid SST count",
    )
    if (
        value.get("schema_version") != 1
        or value.get("candidate") != "lexical_repair"
        or value.get("record_count") != _CASE_COUNT
        or value.get("reference_sha256") != references_sha256
        or value.get("prediction_sha256") != predictions_sha256
        or bound_policy.get("policy_id") != policy["policy_id"]
        or bound_policy.get("policy_sha256") != policy["policy_sha256"]
        or bound_policy.get("artifact_sha256") != policy["artifact_sha256"]
    ):
        raise MarkerRepairAcceptanceError("candidate prediction manifest differs from the repair contract")
    if restoration > _CASE_COUNT or valid > _CASE_COUNT:
        raise MarkerRepairAcceptanceError("candidate prediction counts exceed parity scope")
    if restoration + valid > _CASE_COUNT:
        raise MarkerRepairAcceptanceError("candidate prediction counts cannot both describe the 200 cases")
    return restoration, valid


def _validate_training_report(
    report: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    cloud_result: Mapping[str, Any],
) -> dict[str, Any]:
    policy = manifest["policies"]["training"]
    parent_tree = manifest["parent_checkpoint"]["tree_sha256"]
    order_sha256 = manifest["order_lock"]["source_effective_order_sha256"]
    schedule = _mapping(report.get("schedule"), "training schedule")
    checkpoint = _mapping(
        report.get("checkpoint_state"),
        "training checkpoint state",
    )
    warm_start = _mapping(
        report.get("warm_start_parent_checkpoint"),
        "warm-start parent",
    )
    report_policy = _mapping(
        report.get("protection_policy"),
        "training protection policy",
    )
    application = _mapping(
        report.get("protection_application"),
        "training protection application",
    )
    order = _mapping(
        report.get("remediation_order"),
        "training remediation order",
    )
    order_parent = _mapping(
        order.get("parent_checkpoint"),
        "training order parent",
    )
    order_policy = _mapping(
        order.get("protection_policy"),
        "training order protection policy",
    )
    epoch_orders = _sequence(
        order.get("effective_epoch_orders"),
        "training effective epoch orders",
    )
    final_model = _mapping(report.get("final_model"), "final model")
    reload_verification = _mapping(
        final_model.get("reload_verification"),
        "final model reload verification",
    )
    expected_policy = {
        "artifact_sha256": policy["artifact_sha256"],
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
    }
    if (
        report.get("training_completed") is not True
        or report.get("max_steps") != _EXPECTED_UPDATE_STEPS
        or schedule.get("max_steps") != _EXPECTED_UPDATE_STEPS
        or schedule.get("update_steps_per_epoch") != _EXPECTED_UPDATE_STEPS
        or schedule.get("declared_epochs") != 1.0
        or schedule.get("epoch_boundaries_preserved") is not True
        or schedule.get("schedule_source") != "epochs"
        or checkpoint.get("global_step") != _EXPECTED_UPDATE_STEPS
        or checkpoint.get("epoch") != 1.0
        or checkpoint.get("last_checkpoint") != f"checkpoint-{_EXPECTED_UPDATE_STEPS}"
        or report.get("train_examples_loaded") != manifest["data"]["train"]["record_count"]
        or report.get("train_examples_used") != manifest["data"]["train"]["record_count"]
        or report.get("validation_examples_loaded") != manifest["data"]["validation"]["record_count"]
        or report.get("validation_examples_used") != manifest["data"]["validation"]["record_count"]
        or report.get("oversize_rejections") != {"train": 0, "validation": 0}
        or report.get("protection_rejections") != {"train": 0, "validation": 0}
        or warm_start.get("tree_sha256") != parent_tree
        or warm_start.get("relationship") != "warm_start_parent_not_resume"
        or report.get("resume_from_checkpoint") is not None
        or report_policy.get("policy_id") != policy["policy_id"]
        or report_policy.get("policy_sha256") != policy["policy_sha256"]
        or report_policy.get("sha256") != f"sha256:{policy['artifact_sha256']}"
        or ("artifact_sha256" in report_policy and report_policy["artifact_sha256"] != policy["artifact_sha256"])
        or order.get("mode") != "shuffled"
        or order.get("record_count") != manifest["data"]["train"]["record_count"]
        or order.get("epochs") != 1
        or order.get("implicit_trainer_shuffling") is not False
        or order.get("full_epoch_required") is not True
        or order_parent.get("tree_sha256") != parent_tree
        or order_parent.get("relationship") != "warm_start_parent_not_resume"
        or dict(order_policy) != expected_policy
        or len(epoch_orders) != 1
    ):
        raise MarkerRepairAcceptanceError("training report differs from the frozen 146-update contract")
    epoch = _mapping(epoch_orders[0], "training effective epoch order")
    if (
        epoch.get("epoch") != 0
        or epoch.get("record_count") != manifest["data"]["train"]["record_count"]
        or epoch.get("effective_example_ids_sha256") != order_sha256
    ):
        raise MarkerRepairAcceptanceError("training report changed the frozen effective order")
    if (
        application.get("policy_id") != policy["policy_id"]
        or application.get("policy_sha256") != policy["policy_sha256"]
    ):
        raise MarkerRepairAcceptanceError("training protection application binds a different policy")
    for split in ("train", "validation"):
        evidence = _mapping(
            application.get(split),
            f"training protection application {split}",
        )
        preflight = manifest["preflight"][split]
        expected_count = manifest["data"][split]["record_count"]
        if (
            evidence.get("loaded_record_count") != expected_count
            or evidence.get("accepted_record_count") != expected_count
            or evidence.get("rejected_record_count") != 0
            or evidence.get("policy_id") != policy["policy_id"]
            or evidence.get("policy_sha256") != policy["policy_sha256"]
            or evidence.get("source_target_marker_parity") is not True
            or evidence.get("protected_marker_count") != evidence.get("protected_span_count")
            or evidence.get("protected_marker_count") != preflight["marker_count"]
            or evidence.get("protected_span_count") != preflight["span_count"]
            or evidence.get("accepted_example_ids_sha256") != preflight["accepted_example_ids_sha256"]
            or evidence.get("transformed_pairs_sha256") != preflight["transformed_pairs_sha256"]
        ):
            raise MarkerRepairAcceptanceError(f"training protection application {split} is not lossless")
    if (
        final_model.get("directory") != cloud_result["final_model_directory"]
        or reload_verification.get("status") != "passed"
        or reload_verification.get("protection_policy_sha256") != f"sha256:{policy['artifact_sha256']}"
    ):
        raise MarkerRepairAcceptanceError("final model did not reload with the lexical policy")
    return {
        "actual_completed_steps": checkpoint["global_step"],
        "parent_checkpoint_tree_sha256": parent_tree,
        "effective_order_sha256": order_sha256,
        "protection_policy": expected_policy,
    }


def _critical_comparison(
    baseline_results: Sequence[CaseEvaluation],
    candidate_results: Sequence[CaseEvaluation],
) -> dict[str, Any]:
    if [item.case_id for item in baseline_results] != [item.case_id for item in candidate_results]:
        raise MarkerRepairAcceptanceError("baseline and candidate case ordering differs")
    names = sorted({name for result in (*baseline_results, *candidate_results) for name in result.critical_errors})
    checks: dict[str, dict[str, Any]] = {}
    actual = 0
    for name in names:
        baseline_count = sum(name in result.critical_errors for result in baseline_results)
        candidate_count = sum(name in result.critical_errors for result in candidate_results)
        new_case_ids = [
            candidate.case_id
            for baseline, candidate in zip(
                baseline_results,
                candidate_results,
                strict=True,
            )
            if name in candidate.critical_errors and name not in baseline.critical_errors
        ]
        actual += len(new_case_ids)
        checks[name] = {
            "baseline": baseline_count,
            "candidate": candidate_count,
            "new": len(new_case_ids),
            "new_case_ids": new_case_ids,
            "new_case_ids_sha256": hash_identifier_sequence(new_case_ids),
            "passed": not new_case_ids,
        }
    return {
        "actual": actual,
        "maximum": 0,
        "checks": checks,
        "passed": actual == 0,
    }


def _quality_comparison(
    baseline_metrics: Mapping[str, Any],
    candidate_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    for name in _QUALITY_METRICS:
        baseline = baseline_metrics.get(name)
        candidate = candidate_metrics.get(name)
        if (
            isinstance(baseline, bool)
            or not isinstance(baseline, int | float)
            or not math.isfinite(float(baseline))
            or isinstance(candidate, bool)
            or not isinstance(candidate, int | float)
            or not math.isfinite(float(candidate))
        ):
            raise MarkerRepairAcceptanceError(f"quality metric is unavailable: {name}")
        passed = float(candidate) + _EPSILON >= float(baseline)
        checks[name] = {
            "baseline": float(baseline),
            "candidate": float(candidate),
            "passed": passed,
        }
    return {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def compare_marker_repair_acceptance(
    *,
    bundle_dir: str | Path,
    cloud_output_dir: str | Path,
) -> dict[str, Any]:
    """Compare one completed lexical repair against its frozen control."""

    bundle_root = Path(bundle_dir).expanduser().resolve()
    output_root = Path(cloud_output_dir).expanduser().resolve()
    if not bundle_root.is_dir() or not output_root.is_dir():
        raise MarkerRepairAcceptanceError("bundle and cloud output must both be directories")
    manifest, manifest_payload, manifest_sha256 = _validate_bundle(bundle_root)
    acceptance_policy_sha256 = _validate_acceptance_policy(
        bundle_root,
        manifest,
    )
    cloud_result, cloud_result_sha256 = _validate_cloud_inventory(output_root)
    _, copied_manifest, copied_manifest_sha256 = _stable_bytes(
        output_root / "repair-manifest.json",
        "cloud marker repair manifest",
    )
    if copied_manifest != manifest_payload or copied_manifest_sha256 != manifest_sha256:
        raise MarkerRepairAcceptanceError("cloud run used a different marker repair manifest")
    _, copied_policy, copied_policy_sha256 = _stable_bytes(
        output_root / "acceptance-policy.yaml",
        "cloud acceptance policy",
    )
    _, source_policy, _ = _stable_bytes(
        bundle_root / "acceptance-policy.yaml",
        "bundle acceptance policy",
    )
    if copied_policy != source_policy or copied_policy_sha256 != acceptance_policy_sha256:
        raise MarkerRepairAcceptanceError("cloud run used a different acceptance policy")

    references_path = _safe_bundle_file(
        bundle_root,
        manifest["data"]["parity_references"]["filename"],
        "parity references",
    )
    _, references_payload, references_sha256 = _stable_bytes(
        references_path,
        "parity references",
    )
    reference_binding = manifest["data"]["parity_references"]
    if references_sha256 != reference_binding["sha256"] or reference_binding["record_count"] != _CASE_COUNT:
        raise MarkerRepairAcceptanceError("parity references differ from the 200-case repair binding")
    try:
        references = load_evaluation_references(references_path)
    except (OSError, TypeError, ValueError) as error:
        raise MarkerRepairAcceptanceError(f"parity references are invalid: {error}") from error
    if len(references) != _CASE_COUNT or any(item["split"] != "regression" for item in references):
        raise MarkerRepairAcceptanceError("parity references are not the exact 200 validation-only cases")

    baseline_predictions_path, _, baseline_predictions_sha256 = _load_bound_baseline(
        bundle_root, manifest, "predictions"
    )
    baseline_manifest_path, baseline_manifest_payload, (baseline_manifest_sha256) = _load_bound_baseline(
        bundle_root, manifest, "prediction_manifest"
    )
    baseline_evaluation_path, baseline_evaluation_payload, (baseline_evaluation_sha256) = _load_bound_baseline(
        bundle_root, manifest, "evaluation"
    )
    del baseline_manifest_path, baseline_evaluation_path
    try:
        baseline_predictions = load_candidate_predictions(baseline_predictions_path)
    except (OSError, TypeError, ValueError) as error:
        raise MarkerRepairAcceptanceError(f"baseline predictions are invalid: {error}") from error
    baseline_manifest = _json_object(
        baseline_manifest_payload,
        "baseline prediction manifest",
    )
    _validate_baseline_manifest(
        baseline_manifest,
        references_sha256=references_sha256,
        predictions_sha256=baseline_predictions_sha256,
    )

    candidate_predictions_path, _, candidate_predictions_sha256 = _stable_bytes(
        output_root / "predictions.jsonl",
        "candidate predictions",
    )
    candidate_manifest_path, candidate_manifest_payload, (candidate_manifest_sha256) = _stable_bytes(
        output_root / "predictions.manifest.json",
        "candidate prediction manifest",
    )
    candidate_evaluation_path, candidate_evaluation_payload, (candidate_evaluation_sha256) = _stable_bytes(
        output_root / "evaluation.json",
        "candidate evaluation",
    )
    training_report_path, training_report_payload, training_report_sha256 = _stable_bytes(
        output_root / "training-report.json",
        "candidate training report",
    )
    del (
        candidate_manifest_path,
        candidate_evaluation_path,
        training_report_path,
        references_payload,
    )
    try:
        candidate_predictions = load_candidate_predictions(candidate_predictions_path)
    except (OSError, TypeError, ValueError) as error:
        raise MarkerRepairAcceptanceError(f"candidate predictions are invalid: {error}") from error
    if len(baseline_predictions) != _CASE_COUNT or len(candidate_predictions) != _CASE_COUNT:
        raise MarkerRepairAcceptanceError("baseline and candidate must each contain exactly 200 predictions")
    candidate_manifest = _json_object(
        candidate_manifest_payload,
        "candidate prediction manifest",
    )
    restoration_error_count, valid_sst_count = _validate_candidate_manifest(
        candidate_manifest,
        manifest=manifest,
        references_sha256=references_sha256,
        predictions_sha256=candidate_predictions_sha256,
    )
    baseline_cases = _enriched_cases(references, baseline_predictions)
    candidate_cases = _enriched_cases(references, candidate_predictions)
    baseline_evaluation = _json_object(
        baseline_evaluation_payload,
        "baseline evaluation",
    )
    candidate_evaluation = _json_object(
        candidate_evaluation_payload,
        "candidate evaluation",
    )
    baseline_results = _validate_evaluation(
        baseline_evaluation,
        label="baseline",
        candidate="shuffled",
        references_sha256=references_sha256,
        predictions_sha256=baseline_predictions_sha256,
        cases=baseline_cases,
    )
    candidate_results = _validate_evaluation(
        candidate_evaluation,
        label="candidate",
        candidate="lexical_repair",
        references_sha256=references_sha256,
        predictions_sha256=candidate_predictions_sha256,
        cases=candidate_cases,
    )
    if baseline_evaluation.get("evaluation_config_sha256") != candidate_evaluation.get("evaluation_config_sha256"):
        raise MarkerRepairAcceptanceError("baseline and candidate evaluations use different configs")

    training_report = _json_object(
        training_report_payload,
        "candidate training report",
    )
    execution = _validate_training_report(
        training_report,
        manifest=manifest,
        cloud_result=cloud_result,
    )
    baseline_metrics = _mapping(
        baseline_evaluation["metrics"],
        "baseline metrics",
    )
    candidate_metrics = _mapping(
        candidate_evaluation["metrics"],
        "candidate metrics",
    )
    new_critical = _critical_comparison(
        baseline_results,
        candidate_results,
    )
    quality = _quality_comparison(
        baseline_metrics,
        candidate_metrics,
    )
    raw_sst_parse_rate = float(candidate_metrics["sst_parse_rate"])
    protected_span_exact_rate = float(candidate_metrics["protected_span_exact_rate"])
    checks = {
        "execution": True,
        "parity_references": True,
        "prediction_count": len(candidate_predictions) == _CASE_COUNT,
        "restoration": restoration_error_count == 0,
        "valid_sst": (valid_sst_count == _CASE_COUNT and math.isclose(raw_sst_parse_rate, 1.0)),
        "protected_span_integrity": math.isclose(
            protected_span_exact_rate,
            1.0,
        ),
        "new_critical_errors": new_critical["passed"],
        "quality_metrics_noninferior": quality["passed"],
    }
    accepted = all(checks.values())
    report = {
        "schema_version": 1,
        "kind": "scriber_marker_repair_acceptance",
        "status": "complete",
        "decision": ("ACCEPT_REPAIR" if accepted else "REJECT_REPAIR_KEEP_FROZEN_SHUFFLED"),
        "accepted": accepted,
        "experiment": {
            "experiment_id": manifest["experiment_id"],
            "repair_input_sha256": manifest["repair_input_sha256"],
            "repair_manifest_sha256": manifest_sha256,
            "acceptance_policy_sha256": acceptance_policy_sha256,
        },
        "inputs": {
            "references_sha256": references_sha256,
            "baseline": {
                "predictions_sha256": baseline_predictions_sha256,
                "prediction_manifest_sha256": baseline_manifest_sha256,
                "evaluation_sha256": baseline_evaluation_sha256,
            },
            "candidate": {
                "training_report_sha256": training_report_sha256,
                "predictions_sha256": candidate_predictions_sha256,
                "prediction_manifest_sha256": candidate_manifest_sha256,
                "evaluation_sha256": candidate_evaluation_sha256,
                "cloud_result_sha256": cloud_result_sha256,
            },
        },
        "execution": {
            "passed": True,
            **execution,
        },
        "parity": {
            "passed": True,
            "reference_count": len(references),
            "baseline_prediction_count": len(baseline_predictions),
            "candidate_prediction_count": len(candidate_predictions),
            "reference_sha256_equal": True,
            "case_ids_equal": True,
        },
        "safety": {
            "passed": all(
                checks[name]
                for name in (
                    "restoration",
                    "valid_sst",
                    "protected_span_integrity",
                    "new_critical_errors",
                    "quality_metrics_noninferior",
                )
            ),
            "restoration_error_count": restoration_error_count,
            "valid_sst_count": valid_sst_count,
            "raw_sst_parse_rate": raw_sst_parse_rate,
            "protected_span_exact_rate": protected_span_exact_rate,
            "marker_errors": {
                "deletions": 0 if restoration_error_count == 0 else None,
                "duplications": 0 if restoration_error_count == 0 else None,
                "malformed": 0 if restoration_error_count == 0 else None,
                "reordered": 0 if restoration_error_count == 0 else None,
            },
            "new_critical_errors": new_critical,
            "quality_metrics_noninferior": quality,
        },
        "checks": checks,
    }
    return validate_marker_repair_acceptance_report(report)


def validate_marker_repair_acceptance_report(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate schema plus all derivable decision claims."""

    if not isinstance(value, Mapping):
        raise MarkerRepairAcceptanceError("marker repair acceptance report must be an object")
    materialized = json.loads(json.dumps(dict(value), ensure_ascii=False))
    errors = sorted(
        _report_validator().iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise MarkerRepairAcceptanceError(
            f"marker repair acceptance report schema failed at {location}: {errors[0].message}"
        )
    checks = materialized["checks"]
    accepted = all(checks.values())
    expected_decision = "ACCEPT_REPAIR" if accepted else "REJECT_REPAIR_KEEP_FROZEN_SHUFFLED"
    safety = materialized["safety"]
    critical = safety["new_critical_errors"]
    for check in critical["checks"].values():
        new_case_ids = check["new_case_ids"]
        if (
            check["new"] != len(new_case_ids)
            or check["new_case_ids_sha256"] != hash_identifier_sequence(new_case_ids)
            or check["passed"] != (check["new"] == 0)
            or check["new"] > check["candidate"]
            or check["baseline"] > _CASE_COUNT
            or check["candidate"] > _CASE_COUNT
        ):
            raise MarkerRepairAcceptanceError("critical-error case binding is inconsistent")
    expected_new = sum(item["new"] for item in critical["checks"].values())
    quality = safety["quality_metrics_noninferior"]
    quality_passes: list[bool] = []
    for check in quality["checks"].values():
        expected_pass = float(check["candidate"]) + _EPSILON >= float(check["baseline"])
        if check["passed"] != expected_pass:
            raise MarkerRepairAcceptanceError("quality noninferiority check is inconsistent")
        quality_passes.append(expected_pass)
    if quality["passed"] != all(quality_passes):
        raise MarkerRepairAcceptanceError("quality noninferiority summary is inconsistent")
    restoration_error_count = safety["restoration_error_count"]
    expected_marker_errors = {
        "deletions": 0 if restoration_error_count == 0 else None,
        "duplications": 0 if restoration_error_count == 0 else None,
        "malformed": 0 if restoration_error_count == 0 else None,
        "reordered": 0 if restoration_error_count == 0 else None,
    }
    expected_checks = {
        "execution": True,
        "parity_references": True,
        "prediction_count": (materialized["parity"]["candidate_prediction_count"] == _CASE_COUNT),
        "restoration": restoration_error_count == 0,
        "valid_sst": (safety["valid_sst_count"] == _CASE_COUNT and math.isclose(safety["raw_sst_parse_rate"], 1.0)),
        "protected_span_integrity": math.isclose(
            safety["protected_span_exact_rate"],
            1.0,
        ),
        "new_critical_errors": critical["passed"],
        "quality_metrics_noninferior": quality["passed"],
    }
    if checks != expected_checks or safety["marker_errors"] != expected_marker_errors:
        raise MarkerRepairAcceptanceError("derived hard-gate checks are inconsistent")
    if (
        materialized["accepted"] != accepted
        or materialized["decision"] != expected_decision
        or materialized["execution"]["passed"] != checks["execution"]
        or materialized["parity"]["passed"] != checks["parity_references"]
        or critical["actual"] != expected_new
        or critical["passed"] != (expected_new == 0)
        or quality["passed"] != checks["quality_metrics_noninferior"]
        or safety["passed"]
        != all(
            checks[name]
            for name in (
                "restoration",
                "valid_sst",
                "protected_span_integrity",
                "new_critical_errors",
                "quality_metrics_noninferior",
            )
        )
    ):
        raise MarkerRepairAcceptanceError("marker repair acceptance report decision is inconsistent")
    return materialized


def write_marker_repair_acceptance_report(
    report: Mapping[str, Any],
    output_path: str | Path,
) -> Path:
    """Write one immutable validated single-arm decision report."""

    validated = validate_marker_repair_acceptance_report(report)
    return write_immutable_json(
        output_path,
        validated,
        label="marker repair acceptance report",
    )

"""One fail-closed, batch-durable continuation of the frozen final evaluation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import canonical_json_bytes
from .evaluation_io import (
    load_candidate_predictions,
    load_evaluation_references,
)
from .hf_final_evaluation import (
    FinalEvaluationError,
    _acquire_final_evaluation_writer,
    _bound_final_evaluation_output_root,
    _fsync_parent,
    _load_core_manifest,
    _load_launch_ledger,
    _prediction_model_identity,
    _stable_bytes,
    _validate_final_evaluation_lane_without_exit,
    _validate_result,
    _verify_prediction_policy,
    _write_new_durable_payload,
    bind_final_evaluation_prediction_manifest,
    release_final_evaluation_writer,
    verify_final_evaluation_lane,
    verify_final_evaluation_output_provenance,
    verify_hf_final_evaluation_packet,
)
from .inference import (
    _canonical_json,
    validate_batch_prediction_manifest,
    validate_batch_resume_journal,
)
from .protected_spans import legacy_policy_evidence

AMENDMENT_TIMEOUT_MINUTES = 180
AMENDMENT_FLAVOR = "l4x1"
AMENDMENT_MAXIMUM_COST_USD = "2.400"
PRIMARY_TIMEOUT_MINUTES = 120
PRIMARY_MAXIMUM_COST_USD = "1.600"
TOTAL_BUDGET_USD = "5.000"
MAXIMUM_TOTAL_COST_USD = "4.933668"
ACTUAL_BEFORE_EVALUATION_USD = "0.933668"
REMAINING_BUDGET_USD = "0.066332"
OUTPUT_ROOT = "/outputs/attempt8/scriber-1100-final-evaluation-v1"
OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
REMOTE_RESULT_URI = OUTPUT_BUCKET + "/attempt8/scriber-1100-final-evaluation-v1"
AMENDMENT_LEDGER_SIBLING = ".scriber-1100-final-evaluation-v1.launch-ledger-v2.json"
RUNTIME_COMPLETION_SIBLING = ".scriber-1100-final-evaluation-v1.runtime-completion-v2.json"
WRITER_LOCK_SIBLING = ".scriber-1100-final-evaluation-v1.writer-lock"

_PROJECT_ROOT = Path(__file__).parents[2]
_REPOSITORY_ROOT = _PROJECT_ROOT.parents[1]
_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_amendment_job_manifest_schema.json"
_COMPLETION_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_amendment_completion_schema.json"
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_KNOWN_INFERENCE_TEMP = re.compile(
    r"^\.(?:predictions\.jsonl|predictions\.manifest\.json|predictions\.resume\.json)\..+\.tmp$"
)
_LANE_FILENAMES = frozenset(
    {
        "predictions.jsonl",
        "predictions.manifest.json",
        "predictions.manifest.json.tmp",
        "predictions.resume.json",
        "evaluation.json",
        "evaluation-exit.json",
    }
)
_MODEL_WORK_ROOTS = MappingProxyType(
    {
        "final": "/workspace/scriber-1100-final-evaluation/final-model",
        "base_model": "/workspace/scriber-1100-final-evaluation/base-model",
    }
)


class FinalEvaluationAmendmentError(ValueError):
    """The requested final-evaluation continuation is not safely bound."""


@dataclass(frozen=True, slots=True)
class HfFinalEvaluationAmendmentPacket:
    """An immutable local packet; constructing it does not launch a job."""

    output_dir: Path
    manifest: Mapping[str, Any]


def build_amendment_authorization_evidence(
    source_statement: str,
) -> dict[str, object]:
    """Bind the exact UTF-8 user statement to the narrow, no-increase scope."""

    if not isinstance(source_statement, str) or not source_statement:
        raise FinalEvaluationAmendmentError("authorization source statement must be non-empty")
    return validate_amendment_authorization(
        {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_amendment_authorization",
            "authorized": True,
            "authorizer": "user",
            "scope": "one_job_resume_existing_final_evaluation_only",
            "maximum_additional_jobs": 1,
            "flavor": AMENDMENT_FLAVOR,
            "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
            "maximum_additional_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
            "maximum_total_cost_usd": TOTAL_BUDGET_USD,
            "budget_increase_required": False,
            "training_steps_allowed": 0,
            "publication_allowed": False,
            "source_statement_sha256": _sha256(source_statement.encode("utf-8")),
        }
    )


def build_terminal_predecessor_evidence(
    *,
    job_id: str,
    last_observed_running_seconds: int,
    packet_tree_sha256: str,
) -> dict[str, object]:
    """Bind truthful CANCELED evidence when the terminal API omits duration."""

    return validate_terminal_predecessor(
        {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_terminal_predecessor",
            "job_id": job_id,
            "terminal_status": "CANCELED",
            "termination_reason": "operator_performance_recovery",
            "flavor": AMENDMENT_FLAVOR,
            "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
            "terminal_running_seconds": None,
            "terminal_duration_available": False,
            "last_running_observation": {
                "running_seconds": last_observed_running_seconds,
                "relationship": "strictly_before_cancel_acceptance",
                "source": "hf_jobs_inspect",
            },
            "actual_cost_status": "unknown",
            "budget_accounting": "full_timeout_reservation",
            "packet_tree_sha256": packet_tree_sha256,
            "remote_output_uri": REMOTE_RESULT_URI,
        },
        expected_packet_tree_sha256=packet_tree_sha256,
    )


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FinalEvaluationAmendmentError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise FinalEvaluationAmendmentError(f"{label} contains non-finite JSON value {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalEvaluationAmendmentError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise FinalEvaluationAmendmentError(f"{label} must contain an object")
    return value


def _canonical_file(path: str | Path, label: str) -> tuple[bytes, dict[str, Any]]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FinalEvaluationAmendmentError(f"{label} must not be a symlink")
    resolved = candidate.resolve()
    try:
        payload = _stable_bytes(resolved, label)
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    value = _strict_json_object(payload, label)
    if payload != canonical_json_bytes(value):
        raise FinalEvaluationAmendmentError(f"{label} must be canonical JSON")
    return payload, value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FinalEvaluationAmendmentError(
            f"{label} fields differ (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )


def validate_amendment_authorization(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the user's narrow one-job authorization."""

    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_amendment_authorization",
        "authorized": True,
        "authorizer": "user",
        "scope": "one_job_resume_existing_final_evaluation_only",
        "maximum_additional_jobs": 1,
        "flavor": AMENDMENT_FLAVOR,
        "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
        "maximum_additional_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": TOTAL_BUDGET_USD,
        "budget_increase_required": False,
        "training_steps_allowed": 0,
        "publication_allowed": False,
    }
    _require_exact_keys(
        value,
        {*expected, "source_statement_sha256"},
        "final-evaluation amendment authorization",
    )
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise FinalEvaluationAmendmentError("final-evaluation amendment authorization is broader than allowed")
    statement_hash = value.get("source_statement_sha256")
    if not isinstance(statement_hash, str) or _HASH.fullmatch(statement_hash) is None:
        raise FinalEvaluationAmendmentError("authorization source statement hash is invalid")
    return dict(value)


def validate_terminal_predecessor(
    value: Mapping[str, object],
    *,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    """Accept only a terminal original launch, including explicit operator recovery."""

    _require_exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "job_id",
            "terminal_status",
            "termination_reason",
            "flavor",
            "timeout_minutes",
            "terminal_running_seconds",
            "terminal_duration_available",
            "last_running_observation",
            "actual_cost_status",
            "budget_accounting",
            "packet_tree_sha256",
            "remote_output_uri",
        },
        "terminal predecessor evidence",
    )
    job_id = value.get("job_id")
    observation = value.get("last_running_observation")
    observed_seconds = observation.get("running_seconds") if isinstance(observation, Mapping) else None
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "scriber_hf_final_evaluation_terminal_predecessor"
        or not isinstance(job_id, str)
        or _JOB_ID.fullmatch(job_id) is None
        or value.get("terminal_status") != "CANCELED"
        or value.get("termination_reason") != "operator_performance_recovery"
        or value.get("flavor") != AMENDMENT_FLAVOR
        or value.get("timeout_minutes") != PRIMARY_TIMEOUT_MINUTES
        or value.get("terminal_running_seconds") is not None
        or value.get("terminal_duration_available") is not False
        or not isinstance(observation, Mapping)
        or set(observation) != {"running_seconds", "relationship", "source"}
        or isinstance(observed_seconds, bool)
        or not isinstance(observed_seconds, int)
        or not 1 <= observed_seconds <= PRIMARY_TIMEOUT_MINUTES * 60
        or observation.get("relationship") != "strictly_before_cancel_acceptance"
        or observation.get("source") != "hf_jobs_inspect"
        or value.get("actual_cost_status") != "unknown"
        or value.get("budget_accounting") != "full_timeout_reservation"
        or value.get("packet_tree_sha256") != expected_packet_tree_sha256
        or value.get("remote_output_uri") != REMOTE_RESULT_URI
    ):
        raise FinalEvaluationAmendmentError("terminal predecessor evidence differs from the original launch")
    return dict(value)


def _amendment_budget() -> dict[str, object]:
    actual_before = Decimal(ACTUAL_BEFORE_EVALUATION_USD)
    primary = Decimal(PRIMARY_MAXIMUM_COST_USD)
    amendment = Decimal(AMENDMENT_MAXIMUM_COST_USD)
    total = Decimal(TOTAL_BUDGET_USD)
    maximum = actual_before + primary + amendment
    if maximum != Decimal(MAXIMUM_TOTAL_COST_USD) or maximum >= total:
        raise FinalEvaluationAmendmentError("final-evaluation amendment budget arithmetic is invalid")
    return {
        "total_budget_usd": TOTAL_BUDGET_USD,
        "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
        "primary_timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
        "primary_maximum_cost_usd": PRIMARY_MAXIMUM_COST_USD,
        "amendment_timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
        "amendment_maximum_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": format(maximum, ".6f"),
        "remaining_after_reservations_usd": format(total - maximum, ".6f"),
        "strictly_below_total_budget": True,
        "budget_increase_required": False,
    }


def _inventory_tree(root: Path) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise FinalEvaluationAmendmentError(f"partial output root must be a regular directory: {root}")
    files: list[dict[str, object]] = []
    directories: list[str] = []
    candidates = [(path.relative_to(root).as_posix(), path) for path in root.rglob("*")]
    for relative, path in sorted(candidates, key=lambda item: item[0]):
        if path.is_symlink():
            raise FinalEvaluationAmendmentError(f"partial output contains a symlink: {relative}")
        if path.is_dir():
            directories.append(relative)
            continue
        if not path.is_file():
            raise FinalEvaluationAmendmentError(f"partial output contains a special file: {relative}")
        payload = _stable_bytes(path, f"partial output file {relative}")
        if not payload:
            raise FinalEvaluationAmendmentError(f"partial output contains an empty file: {relative}")
        files.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    identity = {"directories": directories, "files": files}
    return {
        **identity,
        "file_count": len(files),
        "byte_count": sum(int(item["bytes"]) for item in files),
        "tree_sha256": _sha256(canonical_json_bytes(identity)),
    }


def _expected_preflight_reports(core: Mapping[str, object]) -> dict[str, dict[str, object]]:
    final_model = core["final_model"]
    base_model = core["base_model"]
    final = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_model_preflight",
        "status": "passed",
        "cumulative_updates": 1100,
        "model_tree_sha256": final_model["final_model_tree_sha256"],
        "model_file_count": final_model["final_model_file_count"],
        "model_total_bytes": final_model["final_model_total_bytes"],
        "preprocessing_policy": final_model["preprocessing_policy"],
    }
    base_inventory = {
        key: base_model[key]
        for key in (
            "tree_sha256",
            "config_sha256",
            "file_count",
            "total_bytes",
        )
    }
    base = {
        "schema_version": 1,
        "kind": "scriber_hf_base_evaluation_model_preflight",
        "status": "passed",
        "model_id": base_model["model_id"],
        "revision": base_model["revision"],
        "remote_model_uri": base_model["remote_model_uri"],
        "inventory": base_inventory,
        "preprocessing_policy": "implicit_legacy_default",
        "scriber_policy_artifact_present": False,
    }
    return {
        "preflight-remote.json": final,
        "preflight-local.json": final,
        "preflight-base-remote.json": base,
        "preflight-base-local.json": base,
    }


def _verify_root_evidence(
    *,
    root: Path,
    primary_packet_root: Path,
    core: Mapping[str, object],
) -> tuple[bytes, dict[str, object]]:
    project = primary_packet_root / "repo/ml/scriber_polishing"
    source_map = {
        "final-evaluation-manifest.json": project / "job-input/final-evaluation-manifest.json",
        "job-manifest.json": primary_packet_root / "job-manifest.json",
        "hf-cost-audit.json": project / "job-input/hf-cost-audit.json",
        "continuation-result.json": project / "job-input/continuation-result.json",
        "training-report.json": project / "job-input/training-report.json",
    }
    if isinstance(core["continuation_evidence"].get("recovery_chain"), Mapping):
        for name in (
            "attempt9-finalization-source-contract.json",
            "attempt9-finalization-verification.json",
            "finalization-launch-guard.json",
        ):
            source_map[name] = project / "job-input" / name
    else:
        source_map["continuation-manifest.json"] = project / "job-input/continuation-manifest.json"
        source_map["continuation-packet-verification.json"] = (
            project / "job-input/continuation-packet-verification.json"
        )
    for name, source in source_map.items():
        if _stable_bytes(root / name, f"partial output {name}") != _stable_bytes(
            source,
            f"primary packet {name}",
        ):
            raise FinalEvaluationAmendmentError(f"partial output {name} differs from the primary packet")
    for name, expected in _expected_preflight_reports(core).items():
        payload, actual = _canonical_file(root / name, f"partial output {name}")
        if payload != canonical_json_bytes(expected) or actual != expected:
            raise FinalEvaluationAmendmentError(f"partial output {name} differs from the exact model preflight")
    required_top = {*source_map, *_expected_preflight_reports(core), "final-evaluation-launch-ledger.json"}
    actual_top = {path.name for path in root.iterdir() if path.is_file()}
    if actual_top != required_top:
        raise FinalEvaluationAmendmentError(
            f"partial output top-level inventory differs (missing={sorted(required_top - actual_top)}, "
            f"extra={sorted(actual_top - required_top)})"
        )
    ledger_path = root / "final-evaluation-launch-ledger.json"
    ledger_payload = _stable_bytes(ledger_path, "primary launch ledger v1")
    retry = core.get("retry_evidence")
    ledger = _load_launch_ledger(
        ledger_path,
        continuation_evidence=core["continuation_evidence"],
        budget=core["budget"],
        retry_evidence=retry if isinstance(retry, Mapping) else None,
        evaluation_flavor=str(core["flavor"]),
    )
    if ledger is None or ledger.get("evaluation_launch_count") != 1:
        raise FinalEvaluationAmendmentError("primary V1 ledger must contain exactly one launch")
    return ledger_payload, ledger


def _expected_model_binding(core: Mapping[str, object], candidate: str) -> dict[str, object]:
    root = _MODEL_WORK_ROOTS[candidate]
    if candidate == "final":
        model = core["final_model"]
        binding = {
            "kind": "local_tree",
            "model_reference": root,
            "resolved_path": root,
            "requested_revision": None,
            "resolved_revision": None,
            "tree_sha256": model["final_model_tree_sha256"],
            "config_sha256": None,
            "file_count": model["final_model_file_count"],
            "total_bytes": model["final_model_total_bytes"],
        }
    else:
        model = core["base_model"]
        binding = {
            "kind": "local_tree",
            "model_reference": root,
            "resolved_path": root,
            "requested_revision": None,
            "resolved_revision": None,
            "tree_sha256": model["tree_sha256"],
            "config_sha256": model["config_sha256"],
            "file_count": model["file_count"],
            "total_bytes": model["total_bytes"],
        }
    return binding


def _verify_journal_binding(
    binding: Mapping[str, object],
    *,
    references_path: Path,
    references: Sequence[Mapping[str, Any]],
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
) -> None:
    expected_model = _expected_model_binding(core, candidate)
    model = binding.get("model")
    if not isinstance(model, Mapping):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal has no model binding")
    model_without_identity = {key: value for key, value in model.items() if key != "identity_sha256"}
    if candidate == "final":
        expected_config = model_without_identity.get("config_sha256")
        if not isinstance(expected_config, str) or _HASH.fullmatch(expected_config) is None:
            raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal config hash is invalid")
        expected_model["config_sha256"] = expected_config
    if model_without_identity != expected_model:
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal model differs from the frozen model")
    if model.get("identity_sha256") != _sha256(_canonical_json(model_without_identity)):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal model identity hash is invalid")
    case_ids = [str(reference["case_id"]) for reference in references]
    lane_root = f"{OUTPUT_ROOT}/{candidate}/{suite_id}"
    expected_common = {
        "reference_sha256": _sha256(_stable_bytes(references_path, f"{suite_id} references")),
        "reference_case_ids_sha256": _sha256(_canonical_json(case_ids)),
        "reference_count": len(references),
        "candidate": candidate,
        "quantization": "none",
        "device": "cuda",
        "max_new_tokens": 512,
        "batch_size": 8,
        "predictions_output_path": f"{lane_root}/predictions.jsonl",
        "manifest_output_path": f"{lane_root}/predictions.manifest.json",
    }
    if any(binding.get(key) != expected for key, expected in expected_common.items()):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal request binding changed")
    policy = binding.get("protection_policy")
    if not isinstance(policy, Mapping):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal policy binding is absent")
    if candidate == "final":
        frozen = core["final_model"]["preprocessing_policy"]
        expected_policy_subset = {
            "policy_id": frozen["policy_id"],
            "policy_sha256": frozen["policy_sha256"],
            "source": "explicit_artifact",
            "artifact_sha256": frozen["artifact_sha256"],
        }
    else:
        legacy = legacy_policy_evidence()
        expected_policy_subset = {
            "policy_id": legacy["policy_id"],
            "policy_sha256": legacy["policy_sha256"],
            "source": "implicit_legacy_default",
            "artifact_sha256": None,
        }
    if any(policy.get(key) != expected for key, expected in expected_policy_subset.items()):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal policy changed")
    evidence_hash = policy.get("evidence_sha256")
    if not isinstance(evidence_hash, str) or _HASH.fullmatch(evidence_hash) is None:
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal policy evidence hash is invalid")


def _read_journal(
    path: Path,
    *,
    references_path: Path,
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
) -> tuple[int, dict[str, Any]]:
    payload, journal = _canonical_file(path, f"{candidate}/{suite_id} resume journal")
    if payload != _canonical_json(journal):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} resume journal is not canonical")
    references = load_evaluation_references(references_path)
    binding = journal.get("binding")
    if not isinstance(binding, Mapping):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} journal has no request binding")
    _verify_journal_binding(
        binding,
        references_path=references_path,
        references=references,
        candidate=candidate,
        suite_id=suite_id,
        core=core,
    )
    try:
        validated = validate_batch_resume_journal(
            journal,
            references=references,
            expected_binding=binding,
        )
    except ValueError as error:
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} resume journal is invalid: {error}") from error
    return len(validated["entries"]), validated


def _verify_completed_journal_artifacts(
    *,
    journal: Mapping[str, Any],
    lane_root: Path,
    references_path: Path,
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
    has_predictions: bool,
    has_manifest: bool,
) -> None:
    if has_manifest and not has_predictions:
        raise FinalEvaluationAmendmentError(
            f"{candidate}/{suite_id} completed journal has a manifest without predictions"
        )
    if has_predictions:
        expected_predictions = b"".join(_canonical_json(entry["prediction"]) for entry in journal["entries"])
        actual_predictions = _stable_bytes(
            lane_root / "predictions.jsonl",
            f"{candidate}/{suite_id} predictions beside completed journal",
        )
        if actual_predictions != expected_predictions:
            raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} predictions differ from the completed journal")
    if has_manifest:
        _verify_predictions(
            lane_root=lane_root,
            references_path=references_path,
            candidate=candidate,
            suite_id=suite_id,
            core=core,
        )


def _verify_predictions(
    *,
    lane_root: Path,
    references_path: Path,
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
) -> tuple[int, bool]:
    predictions_path = lane_root / "predictions.jsonl"
    manifest_path = lane_root / "predictions.manifest.json"
    predictions_payload = _stable_bytes(predictions_path, f"{candidate}/{suite_id} predictions")
    manifest_payload = _stable_bytes(manifest_path, f"{candidate}/{suite_id} prediction manifest")
    manifest = _strict_json_object(manifest_payload, f"{candidate}/{suite_id} prediction manifest")
    try:
        manifest = validate_batch_prediction_manifest(manifest)
        references = load_evaluation_references(references_path)
        predictions = load_candidate_predictions(predictions_path)
    except ValueError as error:
        raise FinalEvaluationAmendmentError(
            f"{candidate}/{suite_id} completed predictions are invalid: {error}"
        ) from error
    reference_ids = [item["case_id"] for item in references]
    prediction_ids = [item["case_id"] for item in predictions]
    expected_model_path = _MODEL_WORK_ROOTS[candidate]
    expected = {
        "candidate": candidate,
        "record_count": len(references),
        "prediction_sha256": hashlib.sha256(predictions_payload).hexdigest(),
        "batch_size": 8,
        "reference_sha256": hashlib.sha256(_stable_bytes(references_path, "references")).hexdigest(),
        "quantization": "none",
        "device": "cuda",
        "model": expected_model_path,
        "revision": None,
    }
    if reference_ids != prediction_ids or any(manifest.get(key) != value for key, value in expected.items()):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} predictions differ from the frozen request")
    try:
        _verify_prediction_policy(manifest, core, candidate)
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    identity = manifest.get("final_evaluation_model_identity")
    if identity is not None and identity != _prediction_model_identity(core, candidate):
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} prediction model identity changed")
    return len(predictions), identity is not None


def _verify_pending_prediction_binding(
    *,
    lane_root: Path,
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
) -> None:
    manifest_payload = _stable_bytes(
        lane_root / "predictions.manifest.json",
        f"{candidate}/{suite_id} prediction manifest",
    )
    manifest = _strict_json_object(
        manifest_payload,
        f"{candidate}/{suite_id} prediction manifest",
    )
    pending_payload, pending = _canonical_file(
        lane_root / "predictions.manifest.json.tmp",
        f"{candidate}/{suite_id} pending model binding",
    )
    expected = {
        **manifest,
        "final_evaluation_model_identity": _prediction_model_identity(core, candidate),
    }
    if pending_payload != canonical_json_bytes(expected) or pending != expected:
        raise FinalEvaluationAmendmentError(
            f"{candidate}/{suite_id} pending model binding differs from the exact preflight"
        )


def _lane_files(lane_root: Path) -> set[str]:
    if lane_root.is_symlink():
        raise FinalEvaluationAmendmentError(f"lane must not be a symlink: {lane_root}")
    if not lane_root.exists():
        return set()
    if not lane_root.is_dir():
        raise FinalEvaluationAmendmentError(f"lane must be a directory: {lane_root}")
    result: set[str] = set()
    for path in lane_root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise FinalEvaluationAmendmentError(f"lane contains an invalid entry: {path}")
        if path.name in _LANE_FILENAMES or _KNOWN_INFERENCE_TEMP.fullmatch(path.name):
            result.add(path.name)
            continue
        raise FinalEvaluationAmendmentError(f"lane contains an unexpected file: {path.name}")
    return result


def _classify_lane_read_only(
    *,
    lane_root: Path,
    references_path: Path,
    manifest_path: Path,
    candidate: str,
    suite_id: str,
    core: Mapping[str, object],
) -> tuple[str, int]:
    files = _lane_files(lane_root)
    durable = {name for name in files if not _KNOWN_INFERENCE_TEMP.fullmatch(name)}
    has_journal = "predictions.resume.json" in durable
    has_predictions = "predictions.jsonl" in durable
    has_manifest = "predictions.manifest.json" in durable
    has_pending_binding = "predictions.manifest.json.tmp" in durable
    has_evaluation = "evaluation.json" in durable
    has_exit = "evaluation-exit.json" in durable
    if has_journal:
        if has_pending_binding or has_evaluation or has_exit:
            raise FinalEvaluationAmendmentError(
                f"{candidate}/{suite_id} resume journal coexists with evaluation artifacts"
            )
        count, journal = _read_journal(
            lane_root / "predictions.resume.json",
            references_path=references_path,
            candidate=candidate,
            suite_id=suite_id,
            core=core,
        )
        reference_count = int(journal["binding"]["reference_count"])
        if count < reference_count and (has_predictions or has_manifest):
            raise FinalEvaluationAmendmentError(
                f"{candidate}/{suite_id} incomplete journal coexists with final prediction artifacts"
            )
        if count == reference_count:
            _verify_completed_journal_artifacts(
                journal=journal,
                lane_root=lane_root,
                references_path=references_path,
                candidate=candidate,
                suite_id=suite_id,
                core=core,
                has_predictions=has_predictions,
                has_manifest=has_manifest,
            )
        return (
            "journal",
            count,
        )
    if has_predictions != has_manifest:
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} has an orphaned prediction artifact")
    if not has_predictions:
        if has_pending_binding or has_evaluation or has_exit:
            raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} evaluation has no predictions")
        return "empty", 0
    count, bound = _verify_predictions(
        lane_root=lane_root,
        references_path=references_path,
        candidate=candidate,
        suite_id=suite_id,
        core=core,
    )
    if has_pending_binding:
        _verify_pending_prediction_binding(
            lane_root=lane_root,
            candidate=candidate,
            suite_id=suite_id,
            core=core,
        )
    if not has_evaluation:
        if has_exit:
            raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} exit has no evaluation")
        return ("predictions_bound" if bound else "predictions_unbound"), count
    if not bound:
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} evaluated predictions are not model-bound")
    if (lane_root / "evaluation.json.tmp").exists():
        raise FinalEvaluationAmendmentError(f"{candidate}/{suite_id} has a mutable evaluation temporary")
    try:
        if has_exit:
            verify_final_evaluation_lane(
                manifest_path=manifest_path,
                candidate=candidate,
                suite_id=suite_id,
                lane_root=lane_root,
            )
            return "complete", count
        _validate_final_evaluation_lane_without_exit(
            manifest_path=manifest_path,
            candidate=candidate,
            suite_id=suite_id,
            lane_root=lane_root,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    return "evaluated_pending_exit", count


def _validate_snapshot_structure(
    *,
    root: Path,
    primary_packet_root: Path,
    core: Mapping[str, object],
) -> list[dict[str, object]]:
    expected_directories = {candidate: set(core["references"]) for candidate in ("final", "base_model")}
    for path in root.iterdir():
        if path.is_symlink():
            raise FinalEvaluationAmendmentError(f"partial output contains a symlink: {path}")
        if path.is_dir():
            if path.name not in expected_directories:
                raise FinalEvaluationAmendmentError(f"partial output contains an unexpected directory: {path.name}")
            for child in path.iterdir():
                if child.is_symlink() or not child.is_dir() or child.name not in expected_directories[path.name]:
                    raise FinalEvaluationAmendmentError(
                        f"partial output contains an unexpected lane directory: {child}"
                    )
    manifest_path = primary_packet_root / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    project = primary_packet_root / "repo/ml/scriber_polishing"
    lanes: list[dict[str, object]] = []
    sequence = 0
    saw_incomplete = False
    for suite_id in sorted(core["references"]):
        references_path = project / "evaluation-input" / str(core["references"][suite_id]["records_filename"])
        for candidate in ("final", "base_model"):
            sequence += 1
            state, count = _classify_lane_read_only(
                lane_root=root / candidate / suite_id,
                references_path=references_path,
                manifest_path=manifest_path,
                candidate=candidate,
                suite_id=suite_id,
                core=core,
            )
            if saw_incomplete and state != "empty":
                raise FinalEvaluationAmendmentError(
                    "partial output lanes are not the exact deterministic runner prefix"
                )
            if state != "complete":
                saw_incomplete = True
            lanes.append(
                {
                    "sequence": sequence,
                    "candidate": candidate,
                    "suite": suite_id,
                    "state": state,
                    "durable_prediction_records": count,
                }
            )
    durable_records = sum(int(lane["durable_prediction_records"]) for lane in lanes)
    if durable_records <= 0:
        raise FinalEvaluationAmendmentError("amendment refuses to restart the final evaluation from zero")
    if all(lane["state"] == "complete" for lane in lanes):
        raise FinalEvaluationAmendmentError("all final-evaluation lanes are already complete")
    return lanes


def validate_partial_output_snapshot(
    *,
    output_root: str | Path,
    primary_packet_root: str | Path,
    primary_manifest: Mapping[str, object],
    terminal_predecessor: Mapping[str, object],
) -> dict[str, object]:
    """Read-only validation and byte inventory of one terminal partial output."""

    try:
        root = _bound_final_evaluation_output_root(output_root)
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    primary = Path(primary_packet_root).expanduser().resolve()
    core_path = primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    try:
        _, core = _load_core_manifest(core_path)
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    if (root.parent / WRITER_LOCK_SIBLING).exists():
        raise FinalEvaluationAmendmentError("primary final-evaluation writer lock is still active")
    for sibling in (AMENDMENT_LEDGER_SIBLING, RUNTIME_COMPLETION_SIBLING):
        if (root.parent / sibling).exists():
            raise FinalEvaluationAmendmentError(f"amendment sibling already exists: {sibling}")
    if (root / "final-evaluation-result.json").exists():
        raise FinalEvaluationAmendmentError("partial output already contains a final result")
    ledger_payload, ledger = _verify_root_evidence(
        root=root,
        primary_packet_root=primary,
        core=core,
    )
    predecessor_launch_id = hashlib.sha256(str(terminal_predecessor["job_id"]).encode("utf-8")).hexdigest()[:32]
    launches = ledger["evaluation_launches"]
    if launches != [
        {
            "sequence": 1,
            "launch_id": predecessor_launch_id,
            "maximum_cost_usd": PRIMARY_MAXIMUM_COST_USD,
        }
    ]:
        raise FinalEvaluationAmendmentError("primary V1 ledger is not bound to the terminal predecessor")
    budget = primary_manifest.get("budget")
    if (
        not isinstance(budget, Mapping)
        or budget.get("actual_before_evaluation_usd") != ACTUAL_BEFORE_EVALUATION_USD
        or budget.get("evaluation_timeout_minutes") != PRIMARY_TIMEOUT_MINUTES
        or budget.get("evaluation_maximum_cost_usd") != PRIMARY_MAXIMUM_COST_USD
        or budget.get("total_budget_usd") != TOTAL_BUDGET_USD
    ):
        raise FinalEvaluationAmendmentError("primary packet budget differs from the approved amendment basis")
    lanes = _validate_snapshot_structure(
        root=root,
        primary_packet_root=primary,
        core=core,
    )
    inventory = _inventory_tree(root)
    return {
        **inventory,
        "lanes": lanes,
        "durable_prediction_records": sum(int(lane["durable_prediction_records"]) for lane in lanes),
        "all_lanes_complete": False,
        "writer_lock_absent": True,
        "final_result_absent": True,
        "primary_launch_ledger_v1_sha256": _sha256(ledger_payload),
    }


def _packet_inventory(root: Path) -> tuple[list[dict[str, object]], str]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise FinalEvaluationAmendmentError(f"amendment packet contains a symlink: {relative}")
        if not path.is_file() or relative == "job-manifest.json":
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            raise FinalEvaluationAmendmentError(f"amendment packet contains bytecode: {relative}")
        payload = _stable_bytes(path, f"amendment packet file {relative}")
        files.append({"path": relative, "bytes": len(payload), "sha256": _sha256(payload)})
    if not files:
        raise FinalEvaluationAmendmentError("amendment packet inventory is empty")
    return files, _sha256(canonical_json_bytes(files))


def _validate_manifest_schema(value: Mapping[str, object]) -> dict[str, object]:
    if not _SCHEMA.is_file():
        raise FinalEvaluationAmendmentError(f"amendment manifest schema is missing: {_SCHEMA}")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise FinalEvaluationAmendmentError(f"amendment manifest schema failed at {location}: {errors[0].message}")
    return dict(value)


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise FinalEvaluationAmendmentError("cannot bind the amendment source Git revision")
    return value


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def amendment_runner_payload(primary_runner: bytes) -> bytes:
    """Derive one reviewed runner from the exact original runner bytes."""

    try:
        script = primary_runner.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FinalEvaluationAmendmentError("primary final-evaluation runner is not UTF-8") from error

    def replace_once(old: str, new: str, label: str) -> None:
        nonlocal script
        if script.count(old) != 1:
            raise FinalEvaluationAmendmentError(f"primary runner anchor changed: {label}")
        script = script.replace(old, new)

    replace_once(
        'PACKET_ROOT=/input/repo\nSOURCE_REPO="$PACKET_ROOT/repo"\n',
        (
            "PACKET_ROOT=/input/primary\n"
            'SOURCE_REPO="$PACKET_ROOT/repo"\n'
            "AMENDMENT_ROOT=/input/amendment\n"
            'AMENDMENT_PROJECT="$AMENDMENT_ROOT/repo/ml/scriber_polishing"\n'
        ),
        "packet roots",
    )
    replace_once(
        'EXPECTED_PACKET_TREE_SHA256="${1:-}"\nRUN_MODE="${2:-gpu-evaluation}"\n',
        ('EXPECTED_PACKET_TREE_SHA256="${1:-}"\nEXPECTED_AMENDMENT_TREE_SHA256="${2:-}"\nRUN_MODE=gpu-evaluation\n'),
        "arguments",
    )
    replace_once(
        'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ]; then\n'
        '  echo "expected packet tree SHA-256 argument is required" >&2\n'
        "  exit 1\n"
        "fi\n",
        (
            'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_AMENDMENT_TREE_SHA256" ]; then\n'
            '  echo "expected primary and amendment packet tree SHA-256 arguments are required" >&2\n'
            "  exit 1\n"
            "fi\n"
        ),
        "argument validation",
    )
    replace_once(
        'if [ ! -d "$SOURCE_REPO" ]; then\n  echo "immutable packet mount is missing" >&2\n  exit 1\nfi\n',
        (
            'if [ ! -d "$SOURCE_REPO" ] || [ ! -d "$AMENDMENT_PROJECT" ]; then\n'
            '  echo "immutable primary or amendment packet mount is missing" >&2\n'
            "  exit 1\n"
            "fi\n"
        ),
        "mount validation",
    )
    replace_once(
        'export PYTHONPATH="$SOURCE_REPO/ml/scriber_polishing/src"\n'
        'python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \\\n'
        "  --mode packet \\\n",
        (
            'export PYTHONPATH="$AMENDMENT_PROJECT/src"\n'
            'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
            "  --mode packet \\\n"
            '  --packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256"\n'
            'export PYTHONPATH="$SOURCE_REPO/ml/scriber_polishing/src"\n'
            'python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \\\n'
            "  --mode packet \\\n"
        ),
        "packet verification",
    )
    replace_once(
        "release_writer() {\n"
        '  python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \\\n'
        "    --mode release-writer \\\n"
        '    --output-root "$OUTPUT_ROOT" \\\n'
        '    --launch-id "$LAUNCH_ID" \\\n'
        '    --owner-token "$OWNER_TOKEN" || true\n'
        "}\n",
        (
            "release_writer() {\n"
            '  PYTHONPATH="$AMENDMENT_PROJECT/src" '
            'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
            "    --mode release-writer \\\n"
            '    --output-root "$OUTPUT_ROOT" \\\n'
            '    --launch-id "$LAUNCH_ID" \\\n'
            '    --owner-token "$OWNER_TOKEN" || true\n'
            "}\n"
        ),
        "writer release",
    )
    replace_once(
        'python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \\\n'
        "  --mode claim-launch \\\n"
        '  --manifest "$SOURCE_REPO/ml/scriber_polishing/job-input/final-evaluation-manifest.json" \\\n'
        '  --output-root "$OUTPUT_ROOT" \\\n'
        '  --launch-id "$LAUNCH_ID" \\\n'
        '  --owner-token "$OWNER_TOKEN"\n',
        (
            'PYTHONPATH="$AMENDMENT_PROJECT/src" '
            'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
            "  --mode claim-launch \\\n"
            '  --packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n'
            '  --output-root "$OUTPUT_ROOT" \\\n'
            '  --job-id "$JOB_ID" \\\n'
            '  --launch-id "$LAUNCH_ID" \\\n'
            '  --owner-token "$OWNER_TOKEN"\n'
        ),
        "writer claim",
    )
    replace_once(
        'cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"\ncd "$PROJECT_ROOT"\n',
        (
            'cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"\n'
            'cp "$AMENDMENT_PROJECT/src/scriber_polishing/inference.py" '
            '"$PROJECT_ROOT/src/scriber_polishing/inference.py"\n'
            'cd "$PROJECT_ROOT"\n'
        ),
        "batch-durable inference overlay",
    )
    replace_once(
        "evaluate_lane() {\n",
        (
            'PYTHONPATH="$AMENDMENT_PROJECT/src" '
            'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
            "  --mode recover-unbound \\\n"
            '  --packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n'
            '  --output-root "$OUTPUT_ROOT" \\\n'
            '  --final-model "$LOCAL_FINAL_MODEL" \\\n'
            '  --base-model "$LOCAL_BASE_MODEL"\n\n'
            "evaluate_lane() {\n"
        ),
        "unbound prediction recovery",
    )
    replace_once(
        "python scripts/verify_hf_final_evaluation.py \\\n"
        "  --mode finalize \\\n"
        "  --manifest job-input/final-evaluation-manifest.json \\\n"
        '  --output-root "$OUTPUT_ROOT" \\\n'
        '  --output "$OUTPUT_ROOT/final-evaluation-result.json"\n\n'
        'echo "cloud_final_evaluation=complete training_steps_executed=0" >&2\n',
        (
            "python scripts/verify_hf_final_evaluation.py \\\n"
            "  --mode finalize \\\n"
            "  --manifest job-input/final-evaluation-manifest.json \\\n"
            '  --output-root "$OUTPUT_ROOT" \\\n'
            '  --output "$OUTPUT_ROOT/final-evaluation-result.json"\n\n'
            'PYTHONPATH="$AMENDMENT_PROJECT/src" '
            'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
            "  --mode runtime-complete \\\n"
            '  --packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n'
            '  --output-root "$OUTPUT_ROOT" \\\n'
            '  --job-id "$JOB_ID" \\\n'
            '  --launch-id "$LAUNCH_ID"\n\n'
            'echo "cloud_final_evaluation=complete training_steps_executed=0 amendment_v2=complete" >&2\n'
        ),
        "runtime completion",
    )
    return script.replace("\r\n", "\n").encode("utf-8")


def build_hf_final_evaluation_amendment_packet(
    *,
    primary_packet_root: str | Path,
    expected_primary_packet_tree_sha256: str,
    output_snapshot_root: str | Path,
    terminal_predecessor_path: str | Path,
    authorization_path: str | Path,
    output_dir: str | Path,
) -> HfFinalEvaluationAmendmentPacket:
    """Build the immutable continuation packet without remote access or launch."""

    if _HASH.fullmatch(expected_primary_packet_tree_sha256) is None:
        raise FinalEvaluationAmendmentError("expected primary packet tree hash is invalid")
    primary = Path(primary_packet_root).expanduser().resolve()
    try:
        primary_manifest = verify_hf_final_evaluation_packet(
            primary,
            expected_packet_tree_sha256=expected_primary_packet_tree_sha256,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    if (
        primary_manifest.get("flavor") != AMENDMENT_FLAVOR
        or primary_manifest.get("batch_size") != 8
        or primary_manifest.get("max_new_tokens") != 512
    ):
        raise FinalEvaluationAmendmentError("primary packet is not the exact L4 batch8 final evaluation")
    predecessor_payload, predecessor_raw = _canonical_file(
        terminal_predecessor_path,
        "terminal predecessor evidence",
    )
    predecessor = validate_terminal_predecessor(
        predecessor_raw,
        expected_packet_tree_sha256=expected_primary_packet_tree_sha256,
    )
    authorization_payload, authorization_raw = _canonical_file(
        authorization_path,
        "amendment authorization",
    )
    authorization = validate_amendment_authorization(authorization_raw)
    snapshot = validate_partial_output_snapshot(
        output_root=output_snapshot_root,
        primary_packet_root=primary,
        primary_manifest=primary_manifest,
        terminal_predecessor=predecessor,
    )
    ledger_hash = str(snapshot.pop("primary_launch_ledger_v1_sha256"))
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise FinalEvaluationAmendmentError(f"refusing to overwrite amendment packet: {target}")
    primary_runner = _stable_bytes(primary / "run-final-evaluation.sh", "primary runner")
    runner = amendment_runner_payload(primary_runner)
    core_path = primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    inference_path = _PROJECT_ROOT / "src/scriber_polishing/inference.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        project = temporary / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        _copy_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_tree(_PROJECT_ROOT / "contracts", project / "contracts")
        (project / "scripts").mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "scripts/verify_hf_final_evaluation_amendment.py",
            project / "scripts/verify_hf_final_evaluation_amendment.py",
        )
        job_input = project / "job-input"
        job_input.mkdir()
        (job_input / "terminal-predecessor.json").write_bytes(predecessor_payload)
        (job_input / "authorization.json").write_bytes(authorization_payload)
        (temporary / "run-final-evaluation-amendment.sh").write_bytes(runner)
        core = {
            "schema_version": 2,
            "kind": "scriber_hf_final_evaluation_amendment",
            "source_git_head": _git_head(),
            "image": primary_manifest["image"],
            "flavor": AMENDMENT_FLAVOR,
            "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
            "batch_size": 8,
            "max_new_tokens": 512,
            "primary_packet": {
                "packet_tree_sha256": expected_primary_packet_tree_sha256,
                "job_manifest_sha256": _sha256(_stable_bytes(primary / "job-manifest.json", "primary manifest")),
                "core_manifest_sha256": _sha256(_stable_bytes(core_path, "primary core manifest")),
                "runner_sha256": _sha256(primary_runner),
            },
            "terminal_predecessor": predecessor,
            "terminal_predecessor_sha256": _sha256(predecessor_payload),
            "authorization": authorization,
            "authorization_sha256": _sha256(authorization_payload),
            "output": {
                "bucket_root": OUTPUT_BUCKET,
                "relative_path": "attempt8/scriber-1100-final-evaluation-v1",
                "container_path": OUTPUT_ROOT,
                "primary_launch_ledger_v1_sha256": ledger_hash,
                "amendment_ledger_v2_sibling": AMENDMENT_LEDGER_SIBLING,
                "runtime_completion_v2_sibling": RUNTIME_COMPLETION_SIBLING,
                "snapshot": snapshot,
            },
            "budget": _amendment_budget(),
            "durability_patch": {
                "kind": "scriber_batch_durable_resume_v1",
                "inference_module_sha256": _sha256(_stable_bytes(inference_path, "batch-durable inference")),
                "journal_commit_granularity": 8,
                "maximum_replayed_records_after_interruption": 8,
                "legacy_per_record_journal_compatible": True,
                "progress_after_durable_commit": True,
            },
            "training": {
                "training_steps_executed": 0,
                "reference_usage": "inference_and_evaluation_only",
                "train_split_allowed": False,
            },
            "publication": {
                "allowed": False,
                "destination_kind": "private_hf_bucket",
            },
        }
        (job_input / "amendment-manifest.json").write_bytes(canonical_json_bytes(core))
        inventory, packet_tree = _packet_inventory(temporary)
        manifest = _validate_manifest_schema(
            {
                **core,
                "runner_sha256": _sha256(runner),
                "packet_tree_sha256": packet_tree,
                "file_count": len(inventory),
                "byte_count": sum(int(item["bytes"]) for item in inventory),
            }
        )
        (temporary / "job-manifest.json").write_bytes(canonical_json_bytes(manifest))
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HfFinalEvaluationAmendmentPacket(
        output_dir=target,
        manifest=MappingProxyType(manifest),
    )


def _load_amendment_manifest(packet_root: Path) -> tuple[bytes, dict[str, object]]:
    payload, manifest = _canonical_file(packet_root / "job-manifest.json", "amendment job manifest")
    return payload, _validate_manifest_schema(manifest)


def verify_hf_final_evaluation_amendment_packet(
    packet_root: str | Path,
    *,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    """Verify every amendment byte and the unchanged original packet."""

    packet = Path(packet_root).expanduser().resolve()
    if packet.is_symlink() or not packet.is_dir():
        raise FinalEvaluationAmendmentError("amendment packet is missing or symlinked")
    _, manifest = _load_amendment_manifest(packet)
    inventory, tree = _packet_inventory(packet)
    if (
        tree != expected_packet_tree_sha256
        or manifest["packet_tree_sha256"] != tree
        or manifest["file_count"] != len(inventory)
        or manifest["byte_count"] != sum(int(item["bytes"]) for item in inventory)
    ):
        raise FinalEvaluationAmendmentError("amendment packet differs from its launch-bound inventory")
    primary_binding = manifest["primary_packet"]
    primary = Path(primary_packet_root).expanduser().resolve()
    try:
        primary_manifest = verify_hf_final_evaluation_packet(
            primary,
            expected_packet_tree_sha256=str(primary_binding["packet_tree_sha256"]),
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    primary_runner = _stable_bytes(primary / "run-final-evaluation.sh", "primary runner")
    primary_core = primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    if primary_binding != {
        "packet_tree_sha256": primary_manifest["packet_tree_sha256"],
        "job_manifest_sha256": _sha256(_stable_bytes(primary / "job-manifest.json", "primary manifest")),
        "core_manifest_sha256": _sha256(_stable_bytes(primary_core, "primary core")),
        "runner_sha256": _sha256(primary_runner),
    }:
        raise FinalEvaluationAmendmentError("primary packet byte binding changed")
    runner = _stable_bytes(packet / "run-final-evaluation-amendment.sh", "amendment runner")
    if manifest["runner_sha256"] != _sha256(runner) or runner != amendment_runner_payload(primary_runner):
        raise FinalEvaluationAmendmentError("amendment runner differs from the reviewed transformation")
    project = packet / "repo/ml/scriber_polishing"
    inference_payload = _stable_bytes(
        project / "src/scriber_polishing/inference.py",
        "batch-durable inference module",
    )
    if manifest["durability_patch"]["inference_module_sha256"] != _sha256(inference_payload):
        raise FinalEvaluationAmendmentError("batch-durable inference module differs from its binding")
    for filename, field, validator in (
        ("terminal-predecessor.json", "terminal_predecessor", validate_terminal_predecessor),
        ("authorization.json", "authorization", validate_amendment_authorization),
    ):
        payload, raw = _canonical_file(project / "job-input" / filename, filename)
        if filename == "terminal-predecessor.json":
            validated = validator(
                raw,
                expected_packet_tree_sha256=str(primary_binding["packet_tree_sha256"]),
            )
            hash_field = "terminal_predecessor_sha256"
        else:
            validated = validator(raw)
            hash_field = "authorization_sha256"
        if validated != manifest[field] or _sha256(payload) != manifest[hash_field]:
            raise FinalEvaluationAmendmentError(f"{filename} differs from the amendment binding")
    if manifest["budget"] != _amendment_budget():
        raise FinalEvaluationAmendmentError("amendment budget changed")
    return manifest


def build_hf_final_evaluation_amendment_launch_contract(
    packet_root: str | Path,
    *,
    primary_packet_root: str | Path,
) -> dict[str, object]:
    """Build, but never execute, the one 180-minute private L4 launch."""

    packet = Path(packet_root).expanduser().resolve()
    primary = Path(primary_packet_root).expanduser().resolve()
    _, manifest = _load_amendment_manifest(packet)
    verified = verify_hf_final_evaluation_amendment_packet(
        packet,
        primary_packet_root=primary,
        expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
    )
    primary_manifest = json.loads((primary / "job-manifest.json").read_text(encoding="utf-8"))
    model_volume = f"{primary_manifest['final_model']['remote_model_uri']}:/final-model:ro"
    base_volume = f"{primary_manifest['base_model']['remote_model_uri']}:/base-model:ro"
    amendment_volume = f"{packet}:/input/amendment:ro"
    primary_volume = f"{primary}:/input/primary:ro"
    output_volume = f"{OUTPUT_BUCKET}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        AMENDMENT_FLAVOR,
        "--timeout",
        f"{AMENDMENT_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=final-evaluation-1100-amendment-v2",
        "-v",
        amendment_volume,
        "-v",
        primary_volume,
        "-v",
        model_volume,
        "-v",
        base_volume,
        "-v",
        output_volume,
        str(verified["image"]),
        "bash",
        "/input/amendment/run-final-evaluation-amendment.sh",
        str(verified["primary_packet"]["packet_tree_sha256"]),
        str(verified["packet_tree_sha256"]),
    ]
    return {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_amendment_launch_contract",
        "flavor": AMENDMENT_FLAVOR,
        "timeout": f"{AMENDMENT_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "strictly_below_total_budget": True,
        "budget_increase_required": False,
        "primary_packet_tree_sha256": verified["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": verified["packet_tree_sha256"],
        "terminal_predecessor_job_id": verified["terminal_predecessor"]["job_id"],
        "authorization_sha256": verified["authorization_sha256"],
        "primary_launch_ledger_v1_sha256": verified["output"]["primary_launch_ledger_v1_sha256"],
        "partial_output_tree_sha256": verified["output"]["snapshot"]["tree_sha256"],
        "batch_size": 8,
        "max_new_tokens": 512,
        "training_steps_executed": 0,
        "publication_allowed": False,
        "external_job_started": False,
        "amendment_volume": amendment_volume,
        "primary_packet_volume": primary_volume,
        "model_volume": model_volume,
        "base_model_volume": base_volume,
        "output_volume": output_volume,
        "output_root": OUTPUT_ROOT,
        "remote_result_uri": REMOTE_RESULT_URI,
        "argv": argv,
    }


def _current_inventory_matches(root: Path, expected: Mapping[str, object]) -> None:
    actual = _inventory_tree(root)
    selected = {key: expected[key] for key in ("files", "directories", "file_count", "byte_count", "tree_sha256")}
    if actual != selected:
        raise FinalEvaluationAmendmentError("remote partial output differs from the launch-bound snapshot")


def _validate_v2_ledger(
    value: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    current_job_id: str | None = None,
    current_launch_id: str | None = None,
) -> dict[str, object]:
    predecessor = manifest["terminal_predecessor"]
    predecessor_job_id = str(predecessor["job_id"])
    predecessor_launch_id = hashlib.sha256(predecessor_job_id.encode("utf-8")).hexdigest()[:32]
    launches = value.get("evaluation_launches")
    if not isinstance(launches, list) or len(launches) != 2:
        raise FinalEvaluationAmendmentError("amendment V2 ledger must bind exactly two heterogeneous launches")
    second_job_id = launches[1].get("job_id") if isinstance(launches[1], Mapping) else None
    second_launch_id = launches[1].get("launch_id") if isinstance(launches[1], Mapping) else None
    if current_job_id is not None and second_job_id != current_job_id:
        raise FinalEvaluationAmendmentError("amendment V2 ledger names a different runtime job")
    if current_launch_id is not None and second_launch_id != current_launch_id:
        raise FinalEvaluationAmendmentError("amendment V2 ledger names a different launch")
    expected = {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_launch_ledger",
        "total_budget_usd": TOTAL_BUDGET_USD,
        "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "authorization_sha256": manifest["authorization_sha256"],
        "terminal_predecessor_sha256": manifest["terminal_predecessor_sha256"],
        "partial_output_tree_sha256": manifest["output"]["snapshot"]["tree_sha256"],
        "evaluation_launches": [
            {
                "sequence": 1,
                "role": "primary",
                "job_id": predecessor_job_id,
                "launch_id": predecessor_launch_id,
                "terminal_status": predecessor["terminal_status"],
                "termination_reason": predecessor["termination_reason"],
                "flavor": AMENDMENT_FLAVOR,
                "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
                "maximum_cost_usd": PRIMARY_MAXIMUM_COST_USD,
                "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
            },
            {
                "sequence": 2,
                "role": "amendment",
                "job_id": second_job_id,
                "launch_id": second_launch_id,
                "claim_status": "running",
                "flavor": AMENDMENT_FLAVOR,
                "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
                "maximum_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
                "packet_tree_sha256": manifest["packet_tree_sha256"],
            },
        ],
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
        "strictly_below_total_budget": True,
    }
    if (
        not isinstance(second_job_id, str)
        or _JOB_ID.fullmatch(second_job_id) is None
        or second_job_id == predecessor_job_id
        or not isinstance(second_launch_id, str)
        or _LAUNCH_ID.fullmatch(second_launch_id) is None
        or second_launch_id != hashlib.sha256(second_job_id.encode("utf-8")).hexdigest()[:32]
        or dict(value) != expected
    ):
        raise FinalEvaluationAmendmentError("amendment V2 ledger differs from the approved two-job budget")
    return expected


def claim_final_evaluation_amendment(
    *,
    packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    job_id: str,
    launch_id: str,
    owner_token: str,
) -> dict[str, object]:
    """Acquire the writer and exclusively create the job-ID-bound V2 ledger."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or _LAUNCH_ID.fullmatch(launch_id) is None
        or _LAUNCH_ID.fullmatch(owner_token) is None
        or launch_id != hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
    ):
        raise FinalEvaluationAmendmentError("amendment runtime job/launch/owner identity is invalid")
    manifest = verify_hf_final_evaluation_amendment_packet(
        packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    _current_inventory_matches(root, manifest["output"]["snapshot"])
    ledger_path = root.parent / AMENDMENT_LEDGER_SIBLING
    completion_path = root.parent / RUNTIME_COMPLETION_SIBLING
    if ledger_path.exists() or completion_path.exists():
        raise FinalEvaluationAmendmentError("amendment V2 ledger or completion already exists")
    try:
        _acquire_final_evaluation_writer(
            root,
            launch_id=launch_id,
            owner_token=owner_token,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    try:
        _current_inventory_matches(root, manifest["output"]["snapshot"])
        ledger = _validate_v2_ledger(
            {
                "schema_version": 2,
                "kind": "scriber_hf_final_evaluation_launch_ledger",
                "total_budget_usd": TOTAL_BUDGET_USD,
                "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
                "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
                "authorization_sha256": manifest["authorization_sha256"],
                "terminal_predecessor_sha256": manifest["terminal_predecessor_sha256"],
                "partial_output_tree_sha256": manifest["output"]["snapshot"]["tree_sha256"],
                "evaluation_launches": [
                    {
                        "sequence": 1,
                        "role": "primary",
                        "job_id": manifest["terminal_predecessor"]["job_id"],
                        "launch_id": hashlib.sha256(
                            str(manifest["terminal_predecessor"]["job_id"]).encode("utf-8")
                        ).hexdigest()[:32],
                        "terminal_status": manifest["terminal_predecessor"]["terminal_status"],
                        "termination_reason": manifest["terminal_predecessor"]["termination_reason"],
                        "flavor": AMENDMENT_FLAVOR,
                        "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
                        "maximum_cost_usd": PRIMARY_MAXIMUM_COST_USD,
                        "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
                    },
                    {
                        "sequence": 2,
                        "role": "amendment",
                        "job_id": job_id,
                        "launch_id": launch_id,
                        "claim_status": "running",
                        "flavor": AMENDMENT_FLAVOR,
                        "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
                        "maximum_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
                        "packet_tree_sha256": manifest["packet_tree_sha256"],
                    },
                ],
                "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
                "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
                "strictly_below_total_budget": True,
            },
            manifest=manifest,
            current_job_id=job_id,
            current_launch_id=launch_id,
        )
        _write_new_durable_payload(ledger_path, canonical_json_bytes(ledger))
        _fsync_parent(ledger_path)
    except Exception:
        with suppress(FinalEvaluationError):
            release_final_evaluation_writer(
                output_root=root,
                launch_id=launch_id,
                owner_token=owner_token,
            )
        raise
    return {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_amendment_launch_claim",
        "job_id": job_id,
        "launch_id": launch_id,
        "writer_acquired": True,
        "ledger_path": AMENDMENT_LEDGER_SIBLING,
        "maximum_job_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
    }


def recover_unbound_prediction_manifests(
    *,
    packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    final_model_root: str | Path,
    base_model_root: str | Path,
) -> list[dict[str, str]]:
    """Repair only the journal-delete/model-bind crash gap after model preflight."""

    manifest = verify_hf_final_evaluation_amendment_packet(
        packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    _current_inventory_matches(root, manifest["output"]["snapshot"])
    primary = Path(primary_packet_root).expanduser().resolve()
    core_path = primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    _, core = _load_core_manifest(core_path)
    repairs: list[dict[str, str]] = []
    model_roots = {"final": Path(final_model_root), "base_model": Path(base_model_root)}
    for lane in manifest["output"]["snapshot"]["lanes"]:
        if lane["state"] != "predictions_unbound":
            continue
        candidate = str(lane["candidate"])
        suite_id = str(lane["suite"])
        lane_root = root / candidate / suite_id
        references_path = (
            primary
            / "repo/ml/scriber_polishing/evaluation-input"
            / str(core["references"][suite_id]["records_filename"])
        )
        _verify_predictions(
            lane_root=lane_root,
            references_path=references_path,
            candidate=candidate,
            suite_id=suite_id,
            core=core,
        )
        try:
            bind_final_evaluation_prediction_manifest(
                manifest_path=core_path,
                prediction_manifest_path=lane_root / "predictions.manifest.json",
                candidate=candidate,
                model_root=model_roots[candidate],
                allow_create=True,
            )
        except FinalEvaluationError as error:
            raise FinalEvaluationAmendmentError(str(error)) from error
        repairs.append({"candidate": candidate, "suite": suite_id})
    return repairs


def record_runtime_completion(
    *,
    packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    job_id: str,
    launch_id: str,
) -> dict[str, object]:
    """Write a job-bound runtime envelope; terminal status is finalized later."""

    manifest = verify_hf_final_evaluation_amendment_packet(
        packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    ledger_path = root.parent / AMENDMENT_LEDGER_SIBLING
    ledger_payload, ledger_raw = _canonical_file(ledger_path, "amendment V2 ledger")
    ledger = _validate_v2_ledger(
        ledger_raw,
        manifest=manifest,
        current_job_id=job_id,
        current_launch_id=launch_id,
    )
    primary = Path(primary_packet_root).expanduser().resolve()
    core_path = primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    result_payload, result = _canonical_file(root / "final-evaluation-result.json", "final-evaluation result")
    try:
        result = _validate_result(result)
        provenance = verify_final_evaluation_output_provenance(
            manifest_path=core_path,
            output_root=root,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    if (
        result.get("status") != "complete"
        or result.get("packet") != provenance
        or provenance.get("packet_tree_sha256") != manifest["primary_packet"]["packet_tree_sha256"]
    ):
        raise FinalEvaluationAmendmentError("primary result does not complete the bound evaluation")
    completion = {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_amendment_runtime_completion",
        "status": "runtime_complete_pending_terminal_evidence",
        "job_id": job_id,
        "launch_id": launch_id,
        "primary_packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": manifest["packet_tree_sha256"],
        "primary_result_sha256": _sha256(result_payload),
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "amendment_launch_ledger_v2_sha256": _sha256(ledger_payload),
        "maximum_total_cost_usd": ledger["maximum_total_cost_usd"],
        "training_steps_executed": 0,
        "publication_performed": False,
    }
    _write_new_durable_payload(
        root.parent / RUNTIME_COMPLETION_SIBLING,
        canonical_json_bytes(completion),
    )
    _fsync_parent(root.parent / RUNTIME_COMPLETION_SIBLING)
    return completion


def validate_amendment_terminal_completion_evidence(
    value: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    expected_job_id: str,
) -> dict[str, object]:
    """Validate the external terminal observation for the amendment job."""

    _require_exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "job_id",
            "terminal_status",
            "termination_reason",
            "flavor",
            "timeout_minutes",
            "terminal_running_seconds",
            "terminal_duration_available",
            "actual_cost_status",
            "budget_accounting",
            "packet_tree_sha256",
            "remote_output_uri",
        },
        "amendment terminal completion evidence",
    )
    running_seconds = value.get("terminal_running_seconds")
    duration_available = value.get("terminal_duration_available")
    valid_duration = (duration_available is False and running_seconds is None) or (
        duration_available is True
        and not isinstance(running_seconds, bool)
        and isinstance(running_seconds, int)
        and 1 <= running_seconds <= AMENDMENT_TIMEOUT_MINUTES * 60
    )
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "scriber_hf_final_evaluation_amendment_terminal_evidence"
        or value.get("job_id") != expected_job_id
        or value.get("terminal_status") != "COMPLETED"
        or value.get("termination_reason") != "completed"
        or value.get("flavor") != AMENDMENT_FLAVOR
        or value.get("timeout_minutes") != AMENDMENT_TIMEOUT_MINUTES
        or not valid_duration
        or value.get("actual_cost_status") != "unknown"
        or value.get("budget_accounting") != "full_timeout_reservation"
        or value.get("packet_tree_sha256") != manifest["packet_tree_sha256"]
        or value.get("remote_output_uri") != REMOTE_RESULT_URI
    ):
        raise FinalEvaluationAmendmentError("amendment terminal completion evidence is invalid")
    return dict(value)


def _validate_final_root_against_primary_result(
    root: Path,
    result: Mapping[str, object],
) -> dict[str, object]:
    inventory = _inventory_tree(root)
    expected_files = result.get("files")
    if not isinstance(expected_files, Mapping):
        raise FinalEvaluationAmendmentError("primary result contains no exact output inventory")
    actual_without_result = {
        str(item["path"]): {
            "bytes": item["bytes"],
            "sha256": str(item["sha256"]).removeprefix("sha256:"),
        }
        for item in inventory["files"]
        if item["path"] != "final-evaluation-result.json"
    }
    if actual_without_result != dict(expected_files):
        raise FinalEvaluationAmendmentError("completed output differs from the primary result inventory")
    if any(
        str(item["path"]).endswith(".tmp") or str(item["path"]).endswith(".resume.json") for item in inventory["files"]
    ):
        raise FinalEvaluationAmendmentError("completed output still contains an incomplete artifact")
    return inventory


def _validate_completion_schema(value: Mapping[str, object]) -> dict[str, object]:
    if not _COMPLETION_SCHEMA.is_file():
        raise FinalEvaluationAmendmentError(f"amendment completion schema is missing: {_COMPLETION_SCHEMA}")
    schema = json.loads(_COMPLETION_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise FinalEvaluationAmendmentError(f"amendment completion schema failed at {location}: {errors[0].message}")
    return dict(value)


def finalize_terminal_amendment_completion(
    *,
    packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    amendment_terminal_evidence_path: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Finalize authoritative V2 provenance only after the HF job is terminal."""

    manifest = verify_hf_final_evaluation_amendment_packet(
        packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    if (root.parent / WRITER_LOCK_SIBLING).exists():
        raise FinalEvaluationAmendmentError("cannot finalize while the output writer lock exists")
    ledger_payload, ledger_raw = _canonical_file(
        root.parent / AMENDMENT_LEDGER_SIBLING,
        "amendment V2 ledger",
    )
    ledger = _validate_v2_ledger(ledger_raw, manifest=manifest)
    current = ledger["evaluation_launches"][1]
    current_job_id = str(current["job_id"])
    terminal_payload, terminal_raw = _canonical_file(
        amendment_terminal_evidence_path,
        "amendment terminal completion evidence",
    )
    terminal = validate_amendment_terminal_completion_evidence(
        terminal_raw,
        manifest=manifest,
        expected_job_id=current_job_id,
    )
    runtime_payload, runtime = _canonical_file(
        root.parent / RUNTIME_COMPLETION_SIBLING,
        "amendment runtime completion",
    )
    expected_runtime = {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_amendment_runtime_completion",
        "status": "runtime_complete_pending_terminal_evidence",
        "job_id": current_job_id,
        "launch_id": current["launch_id"],
        "primary_packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": manifest["packet_tree_sha256"],
        "primary_result_sha256": runtime.get("primary_result_sha256"),
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "amendment_launch_ledger_v2_sha256": _sha256(ledger_payload),
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "training_steps_executed": 0,
        "publication_performed": False,
    }
    if runtime != expected_runtime:
        raise FinalEvaluationAmendmentError("runtime completion differs from the exact V2 launch")
    result_payload, result = _canonical_file(
        root / "final-evaluation-result.json",
        "primary final-evaluation result",
    )
    try:
        result = _validate_result(result)
    except FinalEvaluationError as error:
        raise FinalEvaluationAmendmentError(str(error)) from error
    result_hash = _sha256(result_payload)
    if runtime["primary_result_sha256"] != result_hash:
        raise FinalEvaluationAmendmentError("runtime completion names a different primary result")
    inventory = _validate_final_root_against_primary_result(root, result)
    primary_ledger_payload = _stable_bytes(
        root / "final-evaluation-launch-ledger.json",
        "primary V1 ledger",
    )
    if _sha256(primary_ledger_payload) != manifest["output"]["primary_launch_ledger_v1_sha256"]:
        raise FinalEvaluationAmendmentError("primary V1 ledger changed after amendment preparation")
    predecessor = manifest["terminal_predecessor"]
    completion = _validate_completion_schema(
        {
            "schema_version": 2,
            "kind": "scriber_hf_final_evaluation_amendment_terminal_completion",
            "status": "complete",
            "training_steps_executed": 0,
            "publication_performed": False,
            "primary_packet": {
                "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
                "runner_sha256": manifest["primary_packet"]["runner_sha256"],
            },
            "amendment_packet": {
                "packet_tree_sha256": manifest["packet_tree_sha256"],
                "runner_sha256": manifest["runner_sha256"],
            },
            "jobs": [
                {
                    "sequence": 1,
                    "role": "primary",
                    "job_id": predecessor["job_id"],
                    "terminal_status": predecessor["terminal_status"],
                    "termination_reason": predecessor["termination_reason"],
                    "flavor": AMENDMENT_FLAVOR,
                    "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
                    "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
                    "terminal_evidence_sha256": manifest["terminal_predecessor_sha256"],
                },
                {
                    "sequence": 2,
                    "role": "amendment",
                    "job_id": terminal["job_id"],
                    "terminal_status": terminal["terminal_status"],
                    "termination_reason": terminal["termination_reason"],
                    "flavor": AMENDMENT_FLAVOR,
                    "timeout_minutes": AMENDMENT_TIMEOUT_MINUTES,
                    "packet_tree_sha256": manifest["packet_tree_sha256"],
                    "terminal_evidence_sha256": _sha256(terminal_payload),
                },
            ],
            "budget": {
                "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
                "primary_actual_cost_status": "unknown",
                "amendment_actual_cost_status": "unknown",
                "budget_accounting": "full_timeout_reservations",
                "primary_maximum_cost_usd": PRIMARY_MAXIMUM_COST_USD,
                "amendment_maximum_cost_usd": AMENDMENT_MAXIMUM_COST_USD,
                "reserved_maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
                "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
                "total_budget_usd": TOTAL_BUDGET_USD,
                "reserved_strictly_below_total_budget": True,
            },
            "output": {
                "remote_result_uri": REMOTE_RESULT_URI,
                "inventory_tree_sha256": inventory["tree_sha256"],
                "file_count": inventory["file_count"],
                "byte_count": inventory["byte_count"],
                "primary_result_sha256": result_hash,
                "primary_launch_ledger_v1_sha256": _sha256(primary_ledger_payload),
                "amendment_launch_ledger_v2_sha256": _sha256(ledger_payload),
                "runtime_completion_v2_sha256": _sha256(runtime_payload),
            },
            "writer_lock_absent": True,
        }
    )
    destination = Path(output_path).expanduser()
    if destination.is_symlink() or destination.exists():
        raise FinalEvaluationAmendmentError(f"refusing to overwrite terminal amendment completion: {destination}")
    resolved = destination.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _write_new_durable_payload(resolved, canonical_json_bytes(completion))
    _fsync_parent(resolved)
    return completion

"""Fail-closed Hugging Face package for the cumulative-1100 final evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import canonical_json_bytes, sha256_bytes
from .evaluation_io import load_evaluation_references
from .hf_marker_repair_job import HF_JOB_HOURLY_RATES_USD, HF_MARKER_REPAIR_IMAGE
from .hf_training_continuation import (
    CONTINUATION_ADDITIONAL_UPDATES,
    CONTINUATION_START_UPDATES,
    CONTINUATION_TARGET_UPDATES,
    TrainingContinuationError,
    build_hf_cost_audit,
    reserve_hf_budget,
    verify_continuation_training_report,
)
from .hf_training_continuation_finalization import (
    ATTEMPT9_FINAL_MODEL_URI,
    ATTEMPT9_JOB_ID,
    ATTEMPT9_SOURCE_URI,
    FINALIZATION_FLAVOR,
    FINALIZATION_REMOTE_OUTPUT_PATH,
    FINALIZATION_TIMEOUT_MINUTES,
    ContinuationFinalizationError,
    attempt9_finalization_source_contract,
    verify_completed_finalization_output,
)
from .hf_training_continuation_recovery import (
    ATTEMPT8_JOB_ID,
    ATTEMPT8_SOURCE_URI,
    attempt8_recovery_source_contract,
)
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .policy_artifact import frozen_lexical_policy_requirement

_EVALUATION_TIMEOUT_MINUTES = 120
_DEFAULT_FINAL_EVALUATION_FLAVOR = "l4x1"
_FINAL_EVALUATION_GPU_FLAVORS = frozenset({"l4x1", "a10g-small"})
_ACCEPTED_REPAIR_DIRECTORY_TREE_SHA256 = "sha256:aa2e12b3366363ed6efe728fff0423d2f19c1147704a1ede1f9b1c6ba65f6c28"
_PRIVATE_CAMPAIGN_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
_FINAL_EVALUATION_BATCH_SIZE = 8
_FINAL_EVALUATION_OUTPUT_ROOT = "/outputs/attempt8/scriber-1100-final-evaluation-v1"
_FINAL_EVALUATION_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
_ATTEMPT11_JOB_ID = "6a6ba10023ed89c748ec83f1"
_ATTEMPT11_PACKET_TREE_SHA256 = "sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"
_ATTEMPT12_JOB_ID = "6a6ba85b23ed89c748ec8491"
_ATTEMPT12_PACKET_TREE_SHA256 = "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"
_DIAGNOSTIC_JOB1_ID = "6a6ba991b36a6516e96a31bc"
_DIAGNOSTIC_JOB2_ID = "6a6ba9d8b36a6516e96a31be"
_CONTINUATION_CAMPAIGN_COST_USD = Decimal("0.893167")
_CPU_SELF_VERIFY_TIMEOUT_MINUTES = 10
_BASE_MODEL_VOLUME_URI = "hf://models/google/gemma-3-270m-it@" + BASE_MODEL_REVISION
_BASE_MODEL_INVENTORY = MappingProxyType(
    {
        "tree_sha256": ("sha256:0d610d30384d641366232e4bd8ad556c6a28470781e68e93757fe9e4ae1a0f57"),
        "config_sha256": ("sha256:8983343e5bfbf8e336d222a522a1d781443491e9de2d081b8856f8fd26ab6046"),
        "file_count": 11,
        "total_bytes": 575485707,
    }
)
_PROJECT_ROOT = Path(__file__).parents[2]
_REPOSITORY_ROOT = _PROJECT_ROOT.parents[1]
_JOB_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_job_manifest_schema.json"
_RESULT_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_result_schema.json"
_SUITE_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")
_OUTPUT_DIRECTORY_NAME = "scriber-1100-final-evaluation-v1"
_OUTPUT_ATTEMPT_DIRECTORY = "attempt8"
_LAUNCH_LEDGER_FILENAME = "final-evaluation-launch-ledger.json"
_WRITER_LOCK_DIRECTORY = f".{_OUTPUT_DIRECTORY_NAME}.writer-lock"
_WRITER_OWNER_FILENAME = "owner.json"
_REFERENCE_SUITES = MappingProxyType(
    {
        "ai_gold": MappingProxyType(
            {
                "label": "AI-Gold",
                "records_filename": "ai_gold_automatic_test_challenge.references.jsonl",
                "manifest_filename": "ai_gold_automatic_test_challenge.references.manifest.json",
                "record_count": 1750,
                "split_counts": {"challenge": 750, "test": 1000},
                "records_sha256": "e12c672e45fccf8beccc1fc06c36d8a45540c7ee7c0149bbce773a1136c6c664",
                "manifest_sha256": "20b375a4584ff91468f6482f074004f72cecd3c2b70c7dc140f3dacd25f9248a",
            }
        ),
        "critical_regression": MappingProxyType(
            {
                "label": "critical-regression",
                "records_filename": "critical_regression_as_challenge.references.jsonl",
                "manifest_filename": "critical_regression_as_challenge.references.manifest.json",
                "record_count": 500,
                "split_counts": {"challenge": 500},
                "records_sha256": "5b5bffe762e718b0f8d2dd0905a732d71f5ff21256c9c8d6cc06bb396ff2fd14",
                "manifest_sha256": "a980d4d84510c5de3d3cb647322f0af4d36e23b333b33940c7d41c7b399fd52f",
            }
        ),
        "regular_test": MappingProxyType(
            {
                "label": "regular-test",
                "manifest_object_key": "output",
                "records_filename": "regular_test_1000.references.jsonl",
                "manifest_filename": "regular_test_1000.references.manifest.json",
                "record_count": 1000,
                "split_counts": {"test": 1000},
                "records_sha256": "01e92d42516a5356f1b3607e15fc7faee2e47ddf717d0781aca60c2933a276fb",
                "manifest_sha256": "ee6ee04a72234adfb336d7907a1e5e4143c58ddee3b78dc79a03d2b401af3ca2",
            }
        ),
    }
)


class FinalEvaluationError(ValueError):
    """The final-evaluation identity, evidence, or budget is not trustworthy."""


@dataclass(frozen=True, slots=True)
class HfFinalEvaluationPacket:
    """One immutable local packet for a later private Hugging Face Job."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _validated_final_evaluation_flavor(value: object) -> str:
    if not isinstance(value, str) or value not in _FINAL_EVALUATION_GPU_FLAVORS:
        raise FinalEvaluationError("final-evaluation flavor must be l4x1 or a10g-small")
    return value


def _job_cost(timeout_minutes: int, *, flavor: str) -> Decimal:
    validated_flavor = _validated_final_evaluation_flavor(flavor)
    hourly_rate = Decimal(str(HF_JOB_HOURLY_RATES_USD[validated_flavor]))
    return (hourly_rate * Decimal(timeout_minutes) / Decimal(60)).quantize(Decimal("0.001"))


def _evidence_cost(
    evidence: Mapping[str, object],
    field: str,
    *,
    places: int,
) -> Decimal:
    value = evidence.get(field)
    pattern = rf"(?:0|[1-9][0-9]*)\.[0-9]{{{places}}}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise FinalEvaluationError(
            f"continuation evidence {field} must be a canonical USD string with {places} decimal places"
        )
    parsed = Decimal(value)
    quantum = Decimal(1).scaleb(-places)
    if parsed < 0 or parsed.quantize(quantum) != parsed:
        raise FinalEvaluationError(f"continuation evidence {field} must have at most {places} decimal places")
    return parsed


def bind_final_evaluation_retry_evidence(
    *,
    failed_evaluations: Sequence[Mapping[str, object]],
    auxiliary_diagnostic_jobs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind ordered failed launches and zero-work CPU diagnostics."""

    def job_identity(raw: Mapping[str, object], *, expected_status: str, expected_flavor: str) -> tuple[str, int, str]:
        job_id = raw.get("job_id")
        running_seconds = raw.get("running_seconds")
        packet_tree_sha256 = raw.get("packet_tree_sha256")
        if (
            set(raw)
            != {
                "job_id",
                "flavor",
                "terminal_status",
                "running_seconds",
                "packet_tree_sha256",
            }
            or not isinstance(job_id, str)
            or re.fullmatch(r"[0-9a-f]{24}", job_id) is None
            or raw.get("flavor") != expected_flavor
            or raw.get("terminal_status") != expected_status
            or isinstance(running_seconds, bool)
            or not isinstance(running_seconds, int)
            or running_seconds <= 0
            or running_seconds > _EVALUATION_TIMEOUT_MINUTES * 60
            or not isinstance(packet_tree_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", packet_tree_sha256) is None
        ):
            raise FinalEvaluationError("prior HF job identity, status, runtime, or packet hash is invalid")
        return job_id, running_seconds, packet_tree_sha256

    if len(failed_evaluations) < 2 or len(auxiliary_diagnostic_jobs) < 2:
        raise FinalEvaluationError("Attempt 11, Attempt 12, and both completed CPU diagnostics are required")
    failed: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    seen_job_ids: set[str] = set()
    failed_cost = Decimal("0")
    diagnostic_cost = Decimal("0")
    for sequence, raw in enumerate(failed_evaluations, start=1):
        job_id, running_seconds, packet_tree_sha256 = job_identity(
            raw,
            expected_status="ERROR",
            expected_flavor="l4x1",
        )
        if job_id in seen_job_ids:
            raise FinalEvaluationError("prior HF jobs must have unique identities")
        seen_job_ids.add(job_id)
        billed_minutes = (running_seconds + 59) // 60
        actual_cost = (Decimal(str(HF_JOB_HOURLY_RATES_USD["l4x1"])) * Decimal(billed_minutes) / Decimal(60)).quantize(
            Decimal("0.000001")
        )
        failed_cost += actual_cost
        failed.append(
            {
                "sequence": sequence,
                "job_id": job_id,
                "terminal_status": "ERROR",
                "flavor": "l4x1",
                "running_seconds": running_seconds,
                "billed_minutes": billed_minutes,
                "hourly_rate_usd": "0.80",
                "actual_job_cost_usd": format(actual_cost, ".6f"),
                "packet_tree_sha256": packet_tree_sha256,
                "failure_stage": "packet_self_verification",
                "packet_verification_completed": False,
                "output_claim_created": False,
                "prediction_records_written": 0,
                "evaluation_reports_written": 0,
                "remote_output_prefix_empty": True,
            }
        )
    for sequence, raw in enumerate(auxiliary_diagnostic_jobs, start=1):
        job_id, running_seconds, packet_tree_sha256 = job_identity(
            raw,
            expected_status="COMPLETED",
            expected_flavor="cpu-basic",
        )
        if job_id in seen_job_ids:
            raise FinalEvaluationError("prior HF jobs must have unique identities")
        seen_job_ids.add(job_id)
        billed_minutes = (running_seconds + 59) // 60
        actual_cost = (Decimal("0.01") * Decimal(billed_minutes) / Decimal(60)).quantize(Decimal("0.000001"))
        diagnostic_cost += actual_cost
        diagnostics.append(
            {
                "sequence": sequence,
                "job_id": job_id,
                "terminal_status": "COMPLETED",
                "flavor": "cpu-basic",
                "running_seconds": running_seconds,
                "billed_minutes": billed_minutes,
                "hourly_rate_usd": "0.01",
                "actual_job_cost_usd": format(actual_cost, ".6f"),
                "packet_tree_sha256": packet_tree_sha256,
                "purpose": "packet_mount_inventory",
                "training_steps_executed": 0,
                "prediction_records_written": 0,
                "evaluation_reports_written": 0,
            }
        )
    attempt11 = failed[0]
    attempt12 = failed[1]
    if (
        attempt11["job_id"] != _ATTEMPT11_JOB_ID
        or attempt11["running_seconds"] != 63
        or attempt11["billed_minutes"] != 2
        or attempt11["actual_job_cost_usd"] != "0.026667"
        or attempt11["packet_tree_sha256"] != _ATTEMPT11_PACKET_TREE_SHA256
        or attempt12["job_id"] != _ATTEMPT12_JOB_ID
        or attempt12["running_seconds"] != 45
        or attempt12["billed_minutes"] != 1
        or attempt12["actual_job_cost_usd"] != "0.013333"
        or attempt12["packet_tree_sha256"] != _ATTEMPT12_PACKET_TREE_SHA256
    ):
        raise FinalEvaluationError("ordered Attempt 11 or Attempt 12 evidence differs from the observed jobs")
    diagnostic1 = diagnostics[0]
    diagnostic2 = diagnostics[1]
    if (
        diagnostic1["job_id"] != _DIAGNOSTIC_JOB1_ID
        or diagnostic1["running_seconds"] != 23
        or diagnostic1["actual_job_cost_usd"] != "0.000167"
        or diagnostic1["packet_tree_sha256"] != _ATTEMPT12_PACKET_TREE_SHA256
        or diagnostic2["job_id"] != _DIAGNOSTIC_JOB2_ID
        or diagnostic2["running_seconds"] != 24
        or diagnostic2["actual_job_cost_usd"] != "0.000167"
        or diagnostic2["packet_tree_sha256"] != _ATTEMPT12_PACKET_TREE_SHA256
    ):
        raise FinalEvaluationError("ordered CPU diagnostic evidence differs from the observed jobs")
    prior_cost = failed_cost + diagnostic_cost
    campaign_cost = _CONTINUATION_CAMPAIGN_COST_USD + prior_cost
    return {
        "schema_version": 2,
        "kind": "scriber_hf_final_evaluation_retry_evidence",
        "failed_evaluations": failed,
        "auxiliary_diagnostic_jobs": diagnostics,
        "failed_evaluation_total_cost_usd": format(failed_cost, ".6f"),
        "auxiliary_diagnostic_total_cost_usd": format(diagnostic_cost, ".6f"),
        "total_prior_job_cost_usd": format(prior_cost, ".6f"),
        "campaign_cost_before_next_job_usd": format(campaign_cost, ".6f"),
    }


def _validate_final_evaluation_retry_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    failed = value.get("failed_evaluations")
    diagnostics = value.get("auxiliary_diagnostic_jobs")
    if not isinstance(failed, list) or not isinstance(diagnostics, list):
        raise FinalEvaluationError("embedded retry evidence must contain ordered prior-job lists")
    failed_inputs = [
        {
            "job_id": item.get("job_id"),
            "flavor": item.get("flavor"),
            "terminal_status": item.get("terminal_status"),
            "running_seconds": item.get("running_seconds"),
            "packet_tree_sha256": item.get("packet_tree_sha256"),
        }
        for item in failed
        if isinstance(item, Mapping)
    ]
    diagnostic_inputs = [
        {
            "job_id": item.get("job_id"),
            "flavor": item.get("flavor"),
            "terminal_status": item.get("terminal_status"),
            "running_seconds": item.get("running_seconds"),
            "packet_tree_sha256": item.get("packet_tree_sha256"),
        }
        for item in diagnostics
        if isinstance(item, Mapping)
    ]
    if len(failed_inputs) != len(failed) or len(diagnostic_inputs) != len(diagnostics):
        raise FinalEvaluationError("embedded retry evidence contains a non-object job")
    expected = bind_final_evaluation_retry_evidence(
        failed_evaluations=failed_inputs,
        auxiliary_diagnostic_jobs=diagnostic_inputs,
    )
    if dict(value) != expected:
        raise FinalEvaluationError("embedded failed-evaluation retry evidence differs from Attempt 11")
    return expected


def build_final_evaluation_budget(
    *,
    continuation_evidence: Mapping[str, object],
    total_budget_usd: str,
    retry_evidence: Mapping[str, object] | None = None,
    evaluation_flavor: str = _DEFAULT_FINAL_EVALUATION_FLAVOR,
) -> dict[str, object]:
    """Reserve one evaluation launch after the continuation's actual billed cost."""

    evaluation_flavor = _validated_final_evaluation_flavor(evaluation_flavor)
    historical = _evidence_cost(
        continuation_evidence,
        "historical_cost_usd",
        places=3,
    )
    continuation = _evidence_cost(
        continuation_evidence,
        "actual_job_cost_usd",
        places=6,
    )
    recovery_chain = continuation_evidence.get("recovery_chain")
    if recovery_chain is not None:
        if not isinstance(recovery_chain, Mapping):
            raise FinalEvaluationError("continuation recovery_chain must be an object")
        recovery = _evidence_cost(
            recovery_chain,
            "recovery_job_cost_usd",
            places=6,
        )
        finalization = _evidence_cost(
            recovery_chain,
            "finalization_job_cost_usd",
            places=6,
        )
        continuation += recovery + finalization
        campaign = _evidence_cost(
            continuation_evidence,
            "actual_campaign_cost_usd",
            places=6,
        )
        if campaign != historical + continuation:
            raise FinalEvaluationError("continuation recovery-chain cost arithmetic is invalid")
    budget_limit = _evidence_cost(
        {"total_budget_usd": total_budget_usd},
        "total_budget_usd",
        places=3,
    )
    failed_evaluation_cost = Decimal("0")
    diagnostic_cost = Decimal("0")
    if retry_evidence is not None:
        retry = _validate_final_evaluation_retry_evidence(retry_evidence)
        failed_evaluation_cost = _evidence_cost(
            retry,
            "failed_evaluation_total_cost_usd",
            places=6,
        )
        diagnostic_cost = _evidence_cost(
            retry,
            "auxiliary_diagnostic_total_cost_usd",
            places=6,
        )
        if _evidence_cost(retry, "campaign_cost_before_next_job_usd", places=6) != (
            historical + continuation + failed_evaluation_cost + diagnostic_cost
        ):
            raise FinalEvaluationError("prior-job campaign cost differs from continuation and retry evidence")
    before_evaluation = historical + continuation + failed_evaluation_cost + diagnostic_cost
    evaluation = _job_cost(
        _EVALUATION_TIMEOUT_MINUTES,
        flavor=evaluation_flavor,
    )
    maximum_total = before_evaluation + evaluation
    if maximum_total >= budget_limit:
        raise FinalEvaluationError(
            f"maximum aggregate HF cost ${maximum_total:.3f} must remain strictly below the ${budget_limit:.3f} budget"
        )
    budget = {
        "total_budget_usd": format(budget_limit, ".3f"),
        "historical_cost_usd": format(historical, ".3f"),
        "continuation_actual_cost_usd": format(continuation, ".6f"),
        "actual_before_evaluation_usd": format(before_evaluation, ".6f"),
        "evaluation_timeout_minutes": _EVALUATION_TIMEOUT_MINUTES,
        "evaluation_maximum_cost_usd": format(evaluation, ".3f"),
        "maximum_total_cost_usd": format(maximum_total, ".6f"),
        "remaining_after_reservation_usd": format(
            budget_limit - maximum_total,
            ".6f",
        ),
        "strictly_below_total_budget": True,
    }
    if retry_evidence is not None:
        budget["failed_evaluation_actual_cost_usd"] = format(
            failed_evaluation_cost,
            ".6f",
        )
        budget["auxiliary_diagnostic_actual_cost_usd"] = format(
            diagnostic_cost,
            ".6f",
        )
    return budget


def _bound_final_evaluation_output_root(output_root: str | Path) -> Path:
    candidate = Path(output_root).expanduser()
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise FinalEvaluationError("final-evaluation output root and parent must not be symlinks")
    root = candidate.resolve()
    if root.name != _OUTPUT_DIRECTORY_NAME or root.parent.name != _OUTPUT_ATTEMPT_DIRECTORY:
        raise FinalEvaluationError("final-evaluation output root differs from the unique attempt8 binding")
    return root


def _validated_launch_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or _LAUNCH_ID.fullmatch(value) is None:
        raise FinalEvaluationError(f"{label} must be 32 lowercase hexadecimal characters")
    return value


def _launch_ledger_total(
    launch_count: int,
    budget: Mapping[str, object],
) -> Decimal:
    return Decimal(str(budget["actual_before_evaluation_usd"])) + (
        Decimal(str(budget["evaluation_maximum_cost_usd"])) * launch_count
    )


def _validate_launch_ledger(
    value: Mapping[str, object],
    *,
    continuation_evidence: Mapping[str, object],
    budget: Mapping[str, object],
    retry_evidence: Mapping[str, object] | None = None,
    evaluation_flavor: str = _DEFAULT_FINAL_EVALUATION_FLAVOR,
) -> dict[str, object]:
    evaluation_flavor = _validated_final_evaluation_flavor(evaluation_flavor)
    continuation = _validate_embedded_continuation_evidence(continuation_evidence)
    expected_budget = build_final_evaluation_budget(
        continuation_evidence=continuation,
        total_budget_usd=str(budget.get("total_budget_usd")),
        retry_evidence=retry_evidence,
        evaluation_flavor=evaluation_flavor,
    )
    if dict(budget) != expected_budget:
        raise FinalEvaluationError("final-evaluation launch ledger budget is invalid")
    launches = value.get("evaluation_launches")
    if not isinstance(launches, list):
        raise FinalEvaluationError("final-evaluation launch ledger has no launch list")
    expected_launches: list[dict[str, object]] = []
    maximum_cost_per_launch = str(expected_budget["evaluation_maximum_cost_usd"])
    seen: set[str] = set()
    for index, raw in enumerate(launches, start=1):
        if not isinstance(raw, Mapping):
            raise FinalEvaluationError("final-evaluation launch ledger contains an invalid launch")
        launch_id = _validated_launch_identity(
            raw.get("launch_id"),  # type: ignore[arg-type]
            "final-evaluation launch ID",
        )
        if launch_id in seen:
            raise FinalEvaluationError("final-evaluation launch ledger contains a duplicate launch ID")
        seen.add(launch_id)
        expected_launches.append(
            {
                "sequence": index,
                "launch_id": launch_id,
                "maximum_cost_usd": maximum_cost_per_launch,
            }
        )
    count = len(expected_launches)
    maximum_total = _launch_ledger_total(count, expected_budget)
    total_budget = Decimal(str(expected_budget["total_budget_usd"]))
    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_launch_ledger",
        "total_budget_usd": expected_budget["total_budget_usd"],
        "continuation_evidence_sha256": ("sha256:" + _sha256(canonical_json_bytes(continuation))),
        "actual_before_evaluation_usd": expected_budget["actual_before_evaluation_usd"],
        "evaluation_maximum_cost_per_launch_usd": maximum_cost_per_launch,
        "evaluation_launch_count": count,
        "evaluation_launches": expected_launches,
        "maximum_total_cost_usd": format(maximum_total, ".6f"),
        "strictly_below_total_budget": maximum_total < total_budget,
    }
    if dict(value) != expected or maximum_total >= total_budget:
        raise FinalEvaluationError("final-evaluation launch ledger violates the approved budget")
    return expected


def _load_launch_ledger(
    path: Path,
    *,
    continuation_evidence: Mapping[str, object],
    budget: Mapping[str, object],
    retry_evidence: Mapping[str, object] | None = None,
    evaluation_flavor: str = _DEFAULT_FINAL_EVALUATION_FLAVOR,
) -> dict[str, object] | None:
    if not path.exists():
        return None
    payload = _stable_bytes(path, "final-evaluation launch ledger")
    ledger = _json_object(payload, "final-evaluation launch ledger")
    if payload != canonical_json_bytes(ledger):
        raise FinalEvaluationError("final-evaluation launch ledger is not canonical JSON")
    return _validate_launch_ledger(
        ledger,
        continuation_evidence=continuation_evidence,
        budget=budget,
        retry_evidence=retry_evidence,
        evaluation_flavor=evaluation_flavor,
    )


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_durable_payload(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _commit_durable_temporary(temporary: Path, destination: Path) -> None:
    with temporary.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    _fsync_parent(destination)


def _replace_mutable_canonical_json(path: Path, value: Mapping[str, object]) -> None:
    payload = canonical_json_bytes(value)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise FinalEvaluationError(f"mutable JSON temporary must not be a symlink: {temporary}")
    if temporary.exists():
        existing = _stable_bytes(temporary, "final-evaluation mutable JSON temporary")
        if existing != payload:
            raise FinalEvaluationError(f"refusing an inconsistent mutable JSON temporary: {temporary}")
        _commit_durable_temporary(temporary, path)
        return
    _write_new_durable_payload(temporary, payload)
    _commit_durable_temporary(temporary, path)


def _writer_lock_paths(root: Path) -> tuple[Path, Path]:
    lock = root.parent / _WRITER_LOCK_DIRECTORY
    return lock, lock / _WRITER_OWNER_FILENAME


def _writer_owner(launch_id: str, owner_token: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_writer_owner",
        "launch_id": launch_id,
        "owner_token": owner_token,
    }


def _acquire_final_evaluation_writer(
    root: Path,
    *,
    launch_id: str,
    owner_token: str,
) -> bool:
    lock, owner_path = _writer_lock_paths(root)
    expected_owner = _writer_owner(launch_id, owner_token)
    try:
        lock.mkdir()
        _fsync_parent(lock)
    except FileExistsError:
        try:
            _json_object(
                _stable_bytes(owner_path, "active final-evaluation writer owner"),
                "active final-evaluation writer owner",
            )
        except (OSError, FinalEvaluationError) as error:
            raise FinalEvaluationError("final-evaluation output already has an active or incomplete writer") from error
        raise FinalEvaluationError("final-evaluation output already has an active writer") from None
    try:
        _write_new_durable_payload(
            owner_path,
            canonical_json_bytes(expected_owner),
        )
        _fsync_parent(owner_path)
    except Exception:
        lock.rmdir()
        _fsync_parent(lock)
        raise
    return True


def claim_final_evaluation_launch(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    launch_id: str,
    owner_token: str,
) -> dict[str, object]:
    """Acquire the exclusive writer and reserve one full bounded HF launch."""

    launch_id = _validated_launch_identity(launch_id, "final-evaluation launch ID")
    owner_token = _validated_launch_identity(owner_token, "final-evaluation owner token")
    _, manifest = _load_core_manifest(manifest_path)
    continuation_evidence = manifest["continuation_evidence"]
    retry_evidence = manifest.get("retry_evidence")
    budget = manifest["budget"]
    evaluation_flavor = _validated_final_evaluation_flavor(manifest.get("flavor"))
    root = _bound_final_evaluation_output_root(output_root)
    root.mkdir(parents=True, exist_ok=True)
    acquired = _acquire_final_evaluation_writer(
        root,
        launch_id=launch_id,
        owner_token=owner_token,
    )
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise FinalEvaluationError(f"final-evaluation output contains a preexisting symlink: {path}")
        final_result_path = root / "final-evaluation-result.json"
        if final_result_path.exists():
            _stable_bytes(
                final_result_path,
                "preexisting final-evaluation result",
            )
            raise FinalEvaluationError("final-evaluation output already contains a final result")
        ledger_path = root / _LAUNCH_LEDGER_FILENAME
        ledger = _load_launch_ledger(
            ledger_path,
            continuation_evidence=continuation_evidence,
            budget=budget,
            retry_evidence=retry_evidence,
            evaluation_flavor=evaluation_flavor,
        )
        launches = [] if ledger is None else list(ledger["evaluation_launches"])
        existing = next(
            (item for item in launches if item["launch_id"] == launch_id),
            None,
        )
        new_reservation = existing is None
        maximum_cost_per_launch = str(budget["evaluation_maximum_cost_usd"])
        if new_reservation:
            launches.append(
                {
                    "sequence": len(launches) + 1,
                    "launch_id": launch_id,
                    "maximum_cost_usd": maximum_cost_per_launch,
                }
            )
        maximum_total = _launch_ledger_total(len(launches), budget)
        total_budget = Decimal(str(budget["total_budget_usd"]))
        if maximum_total >= total_budget:
            raise FinalEvaluationError("new final-evaluation launch would exceed the approved budget")
        candidate = _validate_launch_ledger(
            {
                "schema_version": 1,
                "kind": "scriber_hf_final_evaluation_launch_ledger",
                "total_budget_usd": budget["total_budget_usd"],
                "continuation_evidence_sha256": ("sha256:" + _sha256(canonical_json_bytes(continuation_evidence))),
                "actual_before_evaluation_usd": budget["actual_before_evaluation_usd"],
                "evaluation_maximum_cost_per_launch_usd": maximum_cost_per_launch,
                "evaluation_launch_count": len(launches),
                "evaluation_launches": launches,
                "maximum_total_cost_usd": format(maximum_total, ".6f"),
                "strictly_below_total_budget": True,
            },
            continuation_evidence=continuation_evidence,
            budget=budget,
            retry_evidence=retry_evidence,
            evaluation_flavor=evaluation_flavor,
        )
        if ledger != candidate:
            _replace_mutable_canonical_json(ledger_path, candidate)
        return {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_launch_claim",
            "launch_id": launch_id,
            "new_reservation": new_reservation,
            "writer_acquired": True,
            "ledger": candidate,
        }
    except Exception:
        if acquired:
            release_final_evaluation_writer(
                output_root=root,
                launch_id=launch_id,
                owner_token=owner_token,
            )
        raise


def release_final_evaluation_writer(
    *,
    output_root: str | Path,
    launch_id: str,
    owner_token: str,
) -> dict[str, object]:
    """Release only the exact output writer owned by this invocation."""

    launch_id = _validated_launch_identity(launch_id, "final-evaluation launch ID")
    owner_token = _validated_launch_identity(owner_token, "final-evaluation owner token")
    root = _bound_final_evaluation_output_root(output_root)
    lock, owner_path = _writer_lock_paths(root)
    if not lock.exists():
        return {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_writer_release",
            "released": False,
        }
    existing = _json_object(
        _stable_bytes(owner_path, "active final-evaluation writer owner"),
        "active final-evaluation writer owner",
    )
    if existing != _writer_owner(launch_id, owner_token):
        raise FinalEvaluationError("refusing to release a final-evaluation writer owned by another invocation")
    owner_path.unlink()
    _fsync_parent(owner_path)
    lock.rmdir()
    _fsync_parent(lock)
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_writer_release",
        "released": True,
    }


def _stable_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FinalEvaluationError(f"{label} must be a regular file: {path}")
    before = path.stat()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise FinalEvaluationError(f"cannot read {label}: {error}") from error
    after = path.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise FinalEvaluationError(f"{label} changed while it was read")
    return payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_final_evaluation_references(
    reference_root: str | Path,
) -> dict[str, dict[str, object]]:
    """Bind the exact three evaluation-only suites."""

    root = Path(reference_root).expanduser().resolve()
    if not root.is_dir():
        raise FinalEvaluationError(f"final-evaluation reference root is missing: {root}")
    validated: dict[str, dict[str, object]] = {}
    for suite_id, raw_expected in _REFERENCE_SUITES.items():
        expected = dict(raw_expected)
        label = str(expected.pop("label"))
        manifest_object_key = expected.pop("manifest_object_key", None)
        records_path = root / str(expected["records_filename"])
        manifest_path = root / str(expected["manifest_filename"])
        records_payload = _stable_bytes(
            records_path,
            f"frozen {label} references",
        )
        manifest_payload = _stable_bytes(
            manifest_path,
            f"frozen {label} reference manifest",
        )
        if (
            _sha256(records_payload) != expected["records_sha256"]
            or _sha256(manifest_payload) != expected["manifest_sha256"]
        ):
            raise FinalEvaluationError(f"frozen {label} references differ from the reviewed holdout")
        try:
            manifest = json.loads(manifest_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FinalEvaluationError(f"frozen {label} reference manifest is invalid JSON") from error
        if not isinstance(manifest, Mapping):
            raise FinalEvaluationError(f"frozen {label} reference manifest must be an object")
        manifest_fields = manifest.get(manifest_object_key) if isinstance(manifest_object_key, str) else manifest
        if not isinstance(manifest_fields, Mapping):
            raise FinalEvaluationError(f"frozen {label} reference manifest has no bound output")
        expected_manifest_fields = {
            ("filename" if manifest_object_key is not None else "output_filename"): expected["records_filename"],
            "record_count": expected["record_count"],
            "records_sha256": expected["records_sha256"],
            "split_counts": expected["split_counts"],
        }
        if any(manifest_fields.get(key) != value for key, value in expected_manifest_fields.items()):
            raise FinalEvaluationError(f"frozen {label} reference manifest differs from its binding")
        try:
            references = load_evaluation_references(records_path)
        except (OSError, ValueError) as error:
            raise FinalEvaluationError(f"frozen {label} references are invalid: {error}") from error
        splits = dict(sorted(Counter(row["split"] for row in references).items()))
        if len(references) != expected["record_count"] or splits != expected["split_counts"] or "train" in splits:
            raise FinalEvaluationError(f"frozen {label} references are not the exact test/challenge holdout")
        validated[suite_id] = {
            **expected,
            "split_counts": dict(expected["split_counts"]),
            "usage": "inference_and_evaluation_only",
        }
    return validated


def _frozen_reference_bindings() -> dict[str, dict[str, object]]:
    bindings: dict[str, dict[str, object]] = {}
    for suite_id, raw in _REFERENCE_SUITES.items():
        value = dict(raw)
        value.pop("label")
        value.pop("manifest_object_key", None)
        value["split_counts"] = dict(value["split_counts"])
        value["usage"] = "inference_and_evaluation_only"
        bindings[suite_id] = value
    return bindings


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalEvaluationError(f"{label} is invalid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise FinalEvaluationError(f"{label} must contain a JSON object")
    return value


def bind_final_continuation_model(
    *,
    continuation_result_path: str | Path,
    training_report_path: str | Path,
    remote_model_uri: str,
    remote_result_uri: str,
) -> dict[str, object]:
    """Bind one completed 846+254 model to its exact private read-only mount."""

    result_file = Path(continuation_result_path).expanduser().resolve()
    report_file = Path(training_report_path).expanduser().resolve()
    result_payload = _stable_bytes(
        result_file,
        "continuation result",
    )
    report_payload = _stable_bytes(
        report_file,
        "continuation training report",
    )
    result = _json_object(result_payload, "continuation result")
    try:
        verified_report = verify_continuation_training_report(
            report_file,
            expected_parent_tree_sha256=(_ACCEPTED_REPAIR_DIRECTORY_TREE_SHA256),
        )
    except TrainingContinuationError as error:
        raise FinalEvaluationError(f"continuation training report is not accepted: {error}") from error
    final_directory = verified_report["final_model_directory"]
    final_tree = verified_report["final_model_tree_sha256"]
    final_file_count = result.get("final_model_file_count")
    final_total_bytes = result.get("final_model_total_bytes")
    training_report_sha256 = "sha256:" + _sha256(report_payload)
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_training_continuation_result"
        or result.get("status") != "complete"
        or result.get("flavor") not in {"a10g-small", "l4x1"}
        or result.get("start_updates") != CONTINUATION_START_UPDATES
        or result.get("additional_updates") != CONTINUATION_ADDITIONAL_UPDATES
        or result.get("target_updates") != CONTINUATION_TARGET_UPDATES
        or result.get("cumulative_updates") != CONTINUATION_TARGET_UPDATES
        or result.get("replayed_updates") != 0
        or result.get("parent_initialization") != "warm_start_weights_only"
        or result.get("same_run_resume") != "identity_bound_recovery_only"
        or result.get("final_model_directory") != final_directory
        or result.get("final_model_tree_sha256") != final_tree
        or isinstance(final_file_count, bool)
        or not isinstance(final_file_count, int)
        or final_file_count <= 0
        or isinstance(final_total_bytes, bool)
        or not isinstance(final_total_bytes, int)
        or final_total_bytes <= 0
        or result.get("training_report_sha256") != training_report_sha256
        or result.get("quantization_performed") is not False
        or result.get("publication_performed") is not False
    ):
        raise FinalEvaluationError("continuation result does not prove the exact cumulative-1100 model")
    no_retrain_recovery = result.get("no_retrain_recovery")
    if no_retrain_recovery is None:
        expected_uri = remote_result_uri.rstrip("/") + "/training/" + str(final_directory)
    else:
        if (
            not isinstance(no_retrain_recovery, Mapping)
            or result.get("remote_result_uri") != ATTEMPT9_SOURCE_URI
            or remote_result_uri != ATTEMPT9_SOURCE_URI
            or result.get("final_model_remote_uri") != ATTEMPT9_FINAL_MODEL_URI
        ):
            raise FinalEvaluationError("continuation result does not bind the exact no-retrain recovery model")
        expected_uri = ATTEMPT9_FINAL_MODEL_URI
    if not isinstance(remote_model_uri, str) or remote_model_uri != expected_uri:
        raise FinalEvaluationError("remote model URI is not the exact private continuation model")
    return {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_model_binding",
        "start_updates": CONTINUATION_START_UPDATES,
        "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
        "cumulative_updates": CONTINUATION_TARGET_UPDATES,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "final_model_directory": final_directory,
        "final_model_tree_sha256": final_tree,
        "final_model_file_count": final_file_count,
        "final_model_total_bytes": final_total_bytes,
        "remote_model_uri": remote_model_uri,
        "mount_mode": "read_only",
        "continuation_result_sha256": "sha256:" + _sha256(result_payload),
        "training_report_sha256": training_report_sha256,
    }


def _private_continuation_result_uri(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(_PRIVATE_CAMPAIGN_BUCKET + "/"):
        raise FinalEvaluationError("continuation result URI must use the private campaign bucket")
    suffix = value.removeprefix(_PRIVATE_CAMPAIGN_BUCKET + "/")
    segments = suffix.split("/")
    if not segments or any(
        not segment or segment in {".", ".."} or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", segment) is None
        for segment in segments
    ):
        raise FinalEvaluationError("continuation result URI contains an unsafe or ambiguous path")
    return value


def _canonical_evidence_object(
    path: Path,
    label: str,
) -> tuple[bytes, dict[str, object]]:
    payload = _stable_bytes(path, label)
    value = _json_object(payload, label)
    if payload != canonical_json_bytes(value):
        raise FinalEvaluationError(f"{label} is not canonical immutable JSON")
    return payload, value


def _bind_finalized_continuation_evidence(
    *,
    result_file: Path,
    result_payload: bytes,
    result: Mapping[str, object],
    continuation_job_id: str,
    continuation_running_seconds: int,
    expected_packet_tree_sha256: str,
    remote_model_uri: str,
    total_budget_usd: str,
    finalization_job_id: str | None,
    finalization_running_seconds: int | None,
    expected_finalization_packet_tree_sha256: str | None,
) -> dict[str, object]:
    if (
        continuation_job_id != ATTEMPT8_JOB_ID
        or continuation_running_seconds != 912
        or expected_packet_tree_sha256 != str(attempt8_recovery_source_contract()["source_packet_tree_sha256"])
    ):
        raise FinalEvaluationError("original Attempt-8 job, 912-second runtime, or packet binding differs")
    if (
        not isinstance(finalization_job_id, str)
        or re.fullmatch(r"[0-9a-f]{24}", finalization_job_id) is None
        or isinstance(finalization_running_seconds, bool)
        or not isinstance(finalization_running_seconds, int)
        or finalization_running_seconds <= 0
        or finalization_running_seconds > FINALIZATION_TIMEOUT_MINUTES * 60
        or not isinstance(expected_finalization_packet_tree_sha256, str)
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            expected_finalization_packet_tree_sha256,
        )
        is None
    ):
        raise FinalEvaluationError("Attempt-10 finalization identity, runtime, or packet binding is invalid")
    root = result_file.parent
    report_payload, _ = _canonical_evidence_object(
        root / "training-report.json",
        "corrected Attempt-9 training report",
    )
    source_contract_payload, source_contract = _canonical_evidence_object(
        root / "attempt9-finalization-source-contract.json",
        "Attempt-9 finalization source contract",
    )
    verification_payload, verification = _canonical_evidence_object(
        root / "attempt9-finalization-verification.json",
        "Attempt-9 finalization verification",
    )
    guard_payload, guard = _canonical_evidence_object(
        root / "finalization-launch-guard.json",
        "Attempt-10 finalization launch guard",
    )
    try:
        verified_result = verify_completed_finalization_output(root)
    except ContinuationFinalizationError as error:
        raise FinalEvaluationError(f"Attempt-10 finalization output is not accepted: {error}") from error
    if (
        result_payload != canonical_json_bytes(result)
        or dict(result) != verified_result
        or source_contract != attempt9_finalization_source_contract()
    ):
        raise FinalEvaluationError("Attempt-10 result does not prove exact zero-step recovery finalization")
    source_contract_sha256 = "sha256:" + _sha256(source_contract_payload)
    verification_sha256 = "sha256:" + _sha256(verification_payload)
    corrected_report_sha256 = "sha256:" + _sha256(report_payload)
    original_contract = attempt8_recovery_source_contract()
    if (
        source_contract.get("source_recovery_contract_sha256")
        != "sha256:" + _sha256(canonical_json_bytes(original_contract))
        or source_contract.get("source_job_id") != ATTEMPT9_JOB_ID
        or source_contract.get("source_job_running_seconds") != 93
        or source_contract.get("source_job_billed_minutes") != 2
        or source_contract.get("source_job_cost_usd") != "0.033333"
        or source_contract.get("training_steps_executed") != 0
        or guard.get("packet_tree_sha256") != expected_finalization_packet_tree_sha256
        or guard.get("source_contract_sha256") != source_contract_sha256
        or guard.get("source_job_id") != ATTEMPT9_JOB_ID
        or guard.get("training_steps_executed") != 0
        or guard.get("validation_reexecuted") is not False
        or guard.get("model_copied") is not False
        or result.get("training_steps_executed") != 0
        or result.get("evaluation_executed") is not False
        or result.get("model_copied") is not False
        or result.get("optimizer_state_loaded") is not False
        or result.get("validation_reexecuted") is not False
        or result.get("remote_model_uri") != ATTEMPT9_FINAL_MODEL_URI
        or result.get("corrected_training_report_sha256") != corrected_report_sha256
        or result.get("source_verification_sha256") != verification_sha256
    ):
        raise FinalEvaluationError("Attempt-8, Attempt-9, and Attempt-10 recovery identities are not cross-bound")
    try:
        verified_report = verify_continuation_training_report(
            root / "training-report.json",
            expected_parent_tree_sha256=(_ACCEPTED_REPAIR_DIRECTORY_TREE_SHA256),
        )
    except TrainingContinuationError as error:
        raise FinalEvaluationError(f"corrected Attempt-9 training report is not accepted: {error}") from error
    if (
        verified_report.get("final_model_directory") != result.get("final_model_directory")
        or verified_report.get("final_model_tree_sha256") != result.get("final_model_tree_sha256")
        or verified_report.get("cumulative_updates") != CONTINUATION_TARGET_UPDATES
        or remote_model_uri != ATTEMPT9_FINAL_MODEL_URI
    ):
        raise FinalEvaluationError("finalized continuation does not bind the exact private cumulative-1100 model")
    final_model = {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_model_binding",
        "start_updates": CONTINUATION_START_UPDATES,
        "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
        "cumulative_updates": CONTINUATION_TARGET_UPDATES,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "final_model_directory": result["final_model_directory"],
        "final_model_tree_sha256": result["final_model_tree_sha256"],
        "final_model_file_count": result["final_model_file_count"],
        "final_model_total_bytes": result["final_model_total_bytes"],
        "remote_model_uri": ATTEMPT9_FINAL_MODEL_URI,
        "mount_mode": "read_only",
        "continuation_result_sha256": "sha256:" + _sha256(result_payload),
        "training_report_sha256": corrected_report_sha256,
    }
    audit = build_hf_cost_audit()
    audit_payload = canonical_json_bytes(audit)
    if audit.get("conservative_prior_cost_usd") != "0.893":
        raise FinalEvaluationError("reviewed HF ledger does not include Attempt-8 and Attempt-9")
    billed_minutes = (finalization_running_seconds + 59) // 60
    finalization_cost = (Decimal("0.01") * Decimal(billed_minutes) / Decimal(60)).quantize(Decimal("0.000001"))
    historical = (
        Decimal(str(audit["conservative_prior_cost_usd"]))
        - Decimal(str(original_contract["job_cost_usd"]))
        - Decimal(str(source_contract["source_job_cost_usd"]))
    )
    actual_campaign_cost = Decimal(str(audit["conservative_prior_cost_usd"])) + finalization_cost
    recovery_chain = {
        "original_source_contract_sha256": ("sha256:" + _sha256(canonical_json_bytes(original_contract))),
        "recovery_job_id": ATTEMPT9_JOB_ID,
        "recovery_terminal_status": "ERROR",
        "recovery_flavor": "a10g-small",
        "recovery_running_seconds": 93,
        "recovery_billed_minutes": 2,
        "recovery_hourly_rate_usd": "1.00",
        "recovery_job_cost_usd": "0.033333",
        "recovery_remote_result_uri": ATTEMPT9_SOURCE_URI,
        "finalization_job_id": finalization_job_id,
        "finalization_terminal_status": "COMPLETED",
        "finalization_flavor": FINALIZATION_FLAVOR,
        "finalization_timeout_minutes": FINALIZATION_TIMEOUT_MINUTES,
        "finalization_running_seconds": finalization_running_seconds,
        "finalization_billed_minutes": billed_minutes,
        "finalization_hourly_rate_usd": "0.01",
        "finalization_job_cost_usd": format(finalization_cost, ".6f"),
        "finalization_remote_result_uri": (
            _PRIVATE_CAMPAIGN_BUCKET.rstrip("/") + "/" + FINALIZATION_REMOTE_OUTPUT_PATH
        ),
        "finalization_packet_tree_sha256": (expected_finalization_packet_tree_sha256),
        "source_contract_sha256": source_contract_sha256,
        "source_verification_sha256": verification_sha256,
        "finalization_guard_sha256": "sha256:" + _sha256(guard_payload),
        "remote_model_uri": ATTEMPT9_FINAL_MODEL_URI,
        "final_model_tree_sha256": result["final_model_tree_sha256"],
        "training_steps_executed": 0,
        "model_copied": False,
        "model_reloaded": False,
        "validation_reexecuted": False,
        "source_validation_reexecuted": True,
    }
    continuation_evidence = {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_recovered_continuation_evidence",
        "job_id": ATTEMPT8_JOB_ID,
        "terminal_status": "ERROR",
        "owner": "Buttermilk03",
        "flavor": "a10g-small",
        "timeout_minutes": 30,
        "running_seconds": 912,
        "billed_minutes": 16,
        "hourly_rate_usd": "1.00",
        "actual_job_cost_usd": "0.266667",
        "historical_cost_usd": format(historical, ".3f"),
        "actual_campaign_cost_usd": format(actual_campaign_cost, ".6f"),
        "remote_result_uri": ATTEMPT8_SOURCE_URI,
        "continuation_packet_tree_sha256": expected_packet_tree_sha256,
        "continuation_manifest_sha256": original_contract["source_continuation_manifest_sha256"],
        "packet_verification_sha256": verification_sha256,
        "cost_audit_sha256": "sha256:" + _sha256(audit_payload),
        "continuation_result_sha256": "sha256:" + _sha256(result_payload),
        "training_report_sha256": corrected_report_sha256,
        "recovery_chain": recovery_chain,
    }
    budget = build_final_evaluation_budget(
        continuation_evidence=continuation_evidence,
        total_budget_usd=total_budget_usd,
    )
    return {
        "final_model": final_model,
        "continuation_evidence": continuation_evidence,
        "budget": budget,
        "evidence_sources": {
            "attempt9-finalization-source-contract.json": (root / "attempt9-finalization-source-contract.json"),
            "attempt9-finalization-verification.json": (root / "attempt9-finalization-verification.json"),
            "finalization-launch-guard.json": (root / "finalization-launch-guard.json"),
            "continuation-result.json": result_file,
            "training-report.json": root / "training-report.json",
        },
    }


def bind_final_continuation_evidence(
    *,
    continuation_result_path: str | Path,
    continuation_job_id: str,
    continuation_running_seconds: int,
    expected_packet_tree_sha256: str,
    remote_model_uri: str,
    total_budget_usd: str,
    finalization_job_id: str | None = None,
    finalization_running_seconds: int | None = None,
    expected_finalization_packet_tree_sha256: str | None = None,
) -> dict[str, object]:
    """Bind one completed HF job, its actual billing duration, and its exact model."""

    if re.fullmatch(r"[0-9a-f]{24}", continuation_job_id) is None:
        raise FinalEvaluationError("continuation HF job ID must be 24 lowercase hexadecimal characters")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_packet_tree_sha256) is None:
        raise FinalEvaluationError("expected continuation packet tree SHA-256 is invalid")

    result_file = Path(continuation_result_path).expanduser().resolve()
    if result_file.name != "continuation-result.json":
        raise FinalEvaluationError("continuation result must retain its authoritative filename")
    evidence_root = result_file.parent
    result_probe_payload = _stable_bytes(result_file, "continuation result")
    result_probe = _json_object(result_probe_payload, "continuation result")
    if result_probe.get("kind") == "scriber_hf_training_continuation_finalization_result":
        return _bind_finalized_continuation_evidence(
            result_file=result_file,
            result_payload=result_probe_payload,
            result=result_probe,
            continuation_job_id=continuation_job_id,
            continuation_running_seconds=continuation_running_seconds,
            expected_packet_tree_sha256=expected_packet_tree_sha256,
            remote_model_uri=remote_model_uri,
            total_budget_usd=total_budget_usd,
            finalization_job_id=finalization_job_id,
            finalization_running_seconds=finalization_running_seconds,
            expected_finalization_packet_tree_sha256=(expected_finalization_packet_tree_sha256),
        )
    report_file = evidence_root / "training-report.json"
    manifest_file = evidence_root / "continuation-manifest.json"
    audit_file = evidence_root / "hf-cost-audit.json"
    verification_file = evidence_root / "scriber-1100-packet-verification.json"

    result_payload = _stable_bytes(result_file, "continuation result")
    report_payload = _stable_bytes(report_file, "continuation training report")
    manifest_payload = _stable_bytes(manifest_file, "continuation manifest")
    audit_payload = _stable_bytes(audit_file, "HF cost audit")
    verification_payload = _stable_bytes(
        verification_file,
        "continuation packet verification",
    )
    manifest = _json_object(manifest_payload, "continuation manifest")
    audit = _json_object(audit_payload, "HF cost audit")
    verification = _json_object(
        verification_payload,
        "continuation packet verification",
    )
    if manifest_payload != canonical_json_bytes(manifest):
        raise FinalEvaluationError("continuation manifest is not canonical immutable JSON")
    if audit_payload != canonical_json_bytes(audit) or audit != build_hf_cost_audit():
        raise FinalEvaluationError("HF cost audit differs from the reviewed historical ledger")
    if verification_payload != canonical_json_bytes(verification):
        raise FinalEvaluationError("continuation packet verification is not canonical immutable JSON")

    historical_cost = audit.get("conservative_prior_cost_usd")
    if not isinstance(historical_cost, str):
        raise FinalEvaluationError("HF cost audit has no conservative historical total")
    historical = _evidence_cost(
        {"historical_cost_usd": historical_cost},
        "historical_cost_usd",
        places=3,
    )
    arithmetic = manifest.get("arithmetic")
    data = manifest.get("data")
    training = manifest.get("training")
    manifest_budget = manifest.get("budget")
    storage = manifest.get("storage")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "scriber_hf_training_continuation"
        or manifest.get("status") != "ready"
        or not isinstance(arithmetic, Mapping)
        or arithmetic.get("start_updates") != CONTINUATION_START_UPDATES
        or arithmetic.get("additional_updates") != CONTINUATION_ADDITIONAL_UPDATES
        or arithmetic.get("target_updates") != CONTINUATION_TARGET_UPDATES
        or arithmetic.get("replayed_updates") != 0
        or arithmetic.get("parent_initialization") != "warm_start_weights_only"
        or arithmetic.get("same_run_resume") != "identity_bound_recovery_only"
        or not isinstance(data, Mapping)
        or data.get("test_inputs") is not False
        or data.get("challenge_inputs") is not False
        or not isinstance(training, Mapping)
        or training.get("quantization") is not None
        or training.get("publication") is not False
        or not isinstance(manifest_budget, Mapping)
        or not isinstance(storage, Mapping)
    ):
        raise FinalEvaluationError("continuation manifest does not prove an inference-safe cumulative-1100 run")

    flavor = manifest_budget.get("flavor")
    timeout_minutes = manifest_budget.get("timeout_minutes")
    if (
        not isinstance(flavor, str)
        or flavor not in {"a10g-small", "l4x1"}
        or isinstance(timeout_minutes, bool)
        or not isinstance(timeout_minutes, int)
        or timeout_minutes <= 0
    ):
        raise FinalEvaluationError("continuation manifest has an unsupported job flavor or timeout")
    expected_audit_sha256 = "sha256:" + _sha256(audit_payload)
    expected_reservation = {
        **reserve_hf_budget(
            prior_cost_usd=historical_cost,
            flavor=flavor,
            timeout_minutes=timeout_minutes,
        ),
        "flavor": flavor,
        "cost_audit_sha256": expected_audit_sha256,
    }
    if dict(manifest_budget) != expected_reservation:
        raise FinalEvaluationError("continuation manifest budget differs from its historical cost audit")

    continuation_manifest_sha256 = "sha256:" + _sha256(manifest_payload)
    if (
        set(verification)
        != {
            "schema_version",
            "kind",
            "packet_tree_sha256",
            "continuation_manifest_sha256",
            "file_count",
            "byte_count",
            "flavor",
            "parent_model_embedded",
            "credential_material_present",
        }
        or verification.get("schema_version") != 1
        or verification.get("kind") != "scriber_hf_training_continuation_packet_verification"
        or verification.get("packet_tree_sha256") != expected_packet_tree_sha256
        or verification.get("continuation_manifest_sha256") != continuation_manifest_sha256
        or verification.get("flavor") != flavor
        or verification.get("parent_model_embedded") is not False
        or verification.get("credential_material_present") is not False
        or isinstance(verification.get("file_count"), bool)
        or not isinstance(verification.get("file_count"), int)
        or int(verification["file_count"]) <= 0
        or isinstance(verification.get("byte_count"), bool)
        or not isinstance(verification.get("byte_count"), int)
        or int(verification["byte_count"]) <= 0
    ):
        raise FinalEvaluationError("continuation packet verification does not bind the expected launch packet")

    if (
        isinstance(continuation_running_seconds, bool)
        or not isinstance(continuation_running_seconds, int)
        or continuation_running_seconds <= 0
        or continuation_running_seconds > timeout_minutes * 60
    ):
        raise FinalEvaluationError("continuation running seconds must be within the packet timeout")
    billed_minutes = (continuation_running_seconds + 59) // 60
    hourly_rate = Decimal(str(HF_JOB_HOURLY_RATES_USD[flavor]))
    actual_cost = (hourly_rate * Decimal(billed_minutes) / Decimal(60)).quantize(Decimal("0.000001"))
    remote_result_uri = _private_continuation_result_uri(storage.get("remote_result_uri"))
    final_model = bind_final_continuation_model(
        continuation_result_path=result_file,
        training_report_path=report_file,
        remote_model_uri=remote_model_uri,
        remote_result_uri=remote_result_uri,
    )
    result = _json_object(result_payload, "continuation result")
    if result.get("flavor") != flavor:
        raise FinalEvaluationError("continuation result flavor differs from the launch manifest")
    actual_campaign_cost = historical + actual_cost
    continuation_evidence = {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_continuation_evidence",
        "job_id": continuation_job_id,
        "terminal_status": "COMPLETED",
        "owner": "Buttermilk03",
        "flavor": flavor,
        "timeout_minutes": timeout_minutes,
        "running_seconds": continuation_running_seconds,
        "billed_minutes": billed_minutes,
        "hourly_rate_usd": format(hourly_rate, ".2f"),
        "actual_job_cost_usd": format(actual_cost, ".6f"),
        "historical_cost_usd": format(historical, ".3f"),
        "actual_campaign_cost_usd": format(actual_campaign_cost, ".6f"),
        "remote_result_uri": remote_result_uri,
        "continuation_packet_tree_sha256": expected_packet_tree_sha256,
        "continuation_manifest_sha256": continuation_manifest_sha256,
        "packet_verification_sha256": "sha256:" + _sha256(verification_payload),
        "cost_audit_sha256": expected_audit_sha256,
        "continuation_result_sha256": "sha256:" + _sha256(result_payload),
        "training_report_sha256": "sha256:" + _sha256(report_payload),
    }
    if (
        final_model["continuation_result_sha256"] != continuation_evidence["continuation_result_sha256"]
        or final_model["training_report_sha256"] != continuation_evidence["training_report_sha256"]
    ):
        raise FinalEvaluationError("continuation job and model evidence are not cross-bound")
    budget = build_final_evaluation_budget(
        continuation_evidence=continuation_evidence,
        total_budget_usd=total_budget_usd,
    )
    return {
        "final_model": final_model,
        "continuation_evidence": continuation_evidence,
        "budget": budget,
    }


def _validate_embedded_continuation_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    if value.get("kind") == "scriber_final_evaluation_recovered_continuation_evidence":
        return _validate_embedded_recovered_continuation_evidence(value)
    expected_fields = {
        "schema_version",
        "kind",
        "job_id",
        "terminal_status",
        "owner",
        "flavor",
        "timeout_minutes",
        "running_seconds",
        "billed_minutes",
        "hourly_rate_usd",
        "actual_job_cost_usd",
        "historical_cost_usd",
        "actual_campaign_cost_usd",
        "remote_result_uri",
        "continuation_packet_tree_sha256",
        "continuation_manifest_sha256",
        "packet_verification_sha256",
        "cost_audit_sha256",
        "continuation_result_sha256",
        "training_report_sha256",
    }
    job_id = value.get("job_id")
    flavor = value.get("flavor")
    timeout_minutes = value.get("timeout_minutes")
    running_seconds = value.get("running_seconds")
    billed_minutes = value.get("billed_minutes")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != 1
        or value.get("kind") != "scriber_final_evaluation_continuation_evidence"
        or not isinstance(job_id, str)
        or re.fullmatch(r"[0-9a-f]{24}", job_id) is None
        or value.get("terminal_status") != "COMPLETED"
        or value.get("owner") != "Buttermilk03"
        or not isinstance(flavor, str)
        or flavor not in {"a10g-small", "l4x1"}
        or isinstance(timeout_minutes, bool)
        or not isinstance(timeout_minutes, int)
        or timeout_minutes <= 0
        or isinstance(running_seconds, bool)
        or not isinstance(running_seconds, int)
        or running_seconds <= 0
        or running_seconds > timeout_minutes * 60
        or isinstance(billed_minutes, bool)
        or not isinstance(billed_minutes, int)
        or billed_minutes != (running_seconds + 59) // 60
    ):
        raise FinalEvaluationError("embedded continuation job evidence is invalid")
    hourly_rate = Decimal(str(HF_JOB_HOURLY_RATES_USD[flavor]))
    expected_actual = (hourly_rate * Decimal(billed_minutes) / Decimal(60)).quantize(Decimal("0.000001"))
    actual = _evidence_cost(value, "actual_job_cost_usd", places=6)
    historical = _evidence_cost(value, "historical_cost_usd", places=3)
    campaign = _evidence_cost(value, "actual_campaign_cost_usd", places=6)
    if (
        value.get("hourly_rate_usd") != format(hourly_rate, ".2f")
        or actual != expected_actual
        or campaign != historical + actual
        or _private_continuation_result_uri(value.get("remote_result_uri")) != value.get("remote_result_uri")
    ):
        raise FinalEvaluationError("embedded continuation cost arithmetic is invalid")
    for field in (
        "continuation_packet_tree_sha256",
        "continuation_manifest_sha256",
        "packet_verification_sha256",
        "cost_audit_sha256",
        "continuation_result_sha256",
        "training_report_sha256",
    ):
        digest = value.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise FinalEvaluationError(f"embedded continuation evidence {field} is invalid")
    return dict(value)


def _validate_embedded_recovered_continuation_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    expected_fields = {
        "schema_version",
        "kind",
        "job_id",
        "terminal_status",
        "owner",
        "flavor",
        "timeout_minutes",
        "running_seconds",
        "billed_minutes",
        "hourly_rate_usd",
        "actual_job_cost_usd",
        "historical_cost_usd",
        "actual_campaign_cost_usd",
        "remote_result_uri",
        "continuation_packet_tree_sha256",
        "continuation_manifest_sha256",
        "packet_verification_sha256",
        "cost_audit_sha256",
        "continuation_result_sha256",
        "training_report_sha256",
        "recovery_chain",
    }
    chain_fields = {
        "original_source_contract_sha256",
        "recovery_job_id",
        "recovery_terminal_status",
        "recovery_flavor",
        "recovery_running_seconds",
        "recovery_billed_minutes",
        "recovery_hourly_rate_usd",
        "recovery_job_cost_usd",
        "recovery_remote_result_uri",
        "finalization_job_id",
        "finalization_terminal_status",
        "finalization_flavor",
        "finalization_timeout_minutes",
        "finalization_running_seconds",
        "finalization_billed_minutes",
        "finalization_hourly_rate_usd",
        "finalization_job_cost_usd",
        "finalization_remote_result_uri",
        "finalization_packet_tree_sha256",
        "source_contract_sha256",
        "source_verification_sha256",
        "finalization_guard_sha256",
        "remote_model_uri",
        "final_model_tree_sha256",
        "training_steps_executed",
        "model_copied",
        "model_reloaded",
        "validation_reexecuted",
        "source_validation_reexecuted",
    }
    chain = value.get("recovery_chain")
    original = attempt8_recovery_source_contract()
    source = attempt9_finalization_source_contract()
    source_final_model = source["final_model"]
    if (
        set(value) != expected_fields
        or value.get("schema_version") != 1
        or value.get("job_id") != ATTEMPT8_JOB_ID
        or value.get("terminal_status") != "ERROR"
        or value.get("owner") != "Buttermilk03"
        or value.get("flavor") != "a10g-small"
        or value.get("timeout_minutes") != 30
        or value.get("running_seconds") != 912
        or value.get("billed_minutes") != 16
        or value.get("hourly_rate_usd") != "1.00"
        or value.get("actual_job_cost_usd") != "0.266667"
        or value.get("historical_cost_usd") != "0.593"
        or value.get("remote_result_uri") != ATTEMPT8_SOURCE_URI
        or value.get("continuation_packet_tree_sha256") != original["source_packet_tree_sha256"]
        or value.get("continuation_manifest_sha256") != original["source_continuation_manifest_sha256"]
        or not isinstance(chain, Mapping)
        or set(chain) != chain_fields
        or chain.get("original_source_contract_sha256") != "sha256:" + _sha256(canonical_json_bytes(original))
        or chain.get("recovery_job_id") != ATTEMPT9_JOB_ID
        or chain.get("recovery_terminal_status") != "ERROR"
        or chain.get("recovery_flavor") != "a10g-small"
        or chain.get("recovery_running_seconds") != 93
        or chain.get("recovery_billed_minutes") != 2
        or chain.get("recovery_hourly_rate_usd") != "1.00"
        or chain.get("recovery_job_cost_usd") != "0.033333"
        or chain.get("recovery_remote_result_uri") != ATTEMPT9_SOURCE_URI
        or chain.get("finalization_terminal_status") != "COMPLETED"
        or chain.get("finalization_flavor") != FINALIZATION_FLAVOR
        or chain.get("finalization_timeout_minutes") != FINALIZATION_TIMEOUT_MINUTES
        or chain.get("finalization_hourly_rate_usd") != "0.01"
        or chain.get("finalization_remote_result_uri")
        != _PRIVATE_CAMPAIGN_BUCKET.rstrip("/") + "/" + FINALIZATION_REMOTE_OUTPUT_PATH
        or chain.get("remote_model_uri") != ATTEMPT9_FINAL_MODEL_URI
        or chain.get("final_model_tree_sha256") != source_final_model["tree_sha256"]
        or chain.get("training_steps_executed") != 0
        or chain.get("model_copied") is not False
        or chain.get("model_reloaded") is not False
        or chain.get("validation_reexecuted") is not False
        or chain.get("source_validation_reexecuted") is not True
    ):
        raise FinalEvaluationError("embedded no-retrain continuation recovery evidence is invalid")
    finalization_job_id = chain.get("finalization_job_id")
    finalization_running_seconds = chain.get("finalization_running_seconds")
    finalization_billed_minutes = chain.get("finalization_billed_minutes")
    if (
        not isinstance(finalization_job_id, str)
        or re.fullmatch(r"[0-9a-f]{24}", finalization_job_id) is None
        or isinstance(finalization_running_seconds, bool)
        or not isinstance(finalization_running_seconds, int)
        or finalization_running_seconds <= 0
        or finalization_running_seconds > FINALIZATION_TIMEOUT_MINUTES * 60
        or isinstance(finalization_billed_minutes, bool)
        or not isinstance(finalization_billed_minutes, int)
        or finalization_billed_minutes != (finalization_running_seconds + 59) // 60
    ):
        raise FinalEvaluationError("embedded Attempt-10 finalization billing evidence is invalid")
    finalization_cost = _evidence_cost(
        chain,
        "finalization_job_cost_usd",
        places=6,
    )
    expected_finalization_cost = (Decimal("0.01") * Decimal(finalization_billed_minutes) / Decimal(60)).quantize(
        Decimal("0.000001")
    )
    campaign = _evidence_cost(
        value,
        "actual_campaign_cost_usd",
        places=6,
    )
    if finalization_cost != expected_finalization_cost or campaign != Decimal("0.893000") + finalization_cost:
        raise FinalEvaluationError("embedded no-retrain recovery cost arithmetic is invalid")
    for field in (
        "continuation_packet_tree_sha256",
        "continuation_manifest_sha256",
        "packet_verification_sha256",
        "cost_audit_sha256",
        "continuation_result_sha256",
        "training_report_sha256",
    ):
        digest = value.get(field)
        if (
            not isinstance(digest, str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                digest,
            )
            is None
        ):
            raise FinalEvaluationError(f"embedded continuation evidence {field} is invalid")
    for field in (
        "original_source_contract_sha256",
        "finalization_packet_tree_sha256",
        "source_contract_sha256",
        "source_verification_sha256",
        "finalization_guard_sha256",
        "final_model_tree_sha256",
    ):
        digest = chain.get(field)
        if (
            not isinstance(digest, str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                digest,
            )
            is None
        ):
            raise FinalEvaluationError(f"embedded recovery-chain evidence {field} is invalid")
    return dict(value)


def final_evaluation_runner_payload() -> bytes:
    """Return the bounded, resumable, inference-only cloud runner."""

    script = r"""#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

PACKET_ROOT=/input/repo
SOURCE_REPO="$PACKET_ROOT/repo"
REMOTE_FINAL_MODEL=/final-model
REMOTE_BASE_MODEL=/base-model
WORKSPACE_ROOT=/workspace/scriber-1100-final-evaluation
WORK_ROOT="$WORKSPACE_ROOT/repo"
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
LOCAL_FINAL_MODEL="$WORKSPACE_ROOT/final-model"
LOCAL_BASE_MODEL="$WORKSPACE_ROOT/base-model"
OUTPUT_ROOT=__OUTPUT_ROOT__
BASE_MODEL_ID=__BASE_MODEL_ID__
BASE_MODEL_REVISION=__BASE_MODEL_REVISION__
BASE_POLICY_MODE=implicit_legacy_default
EXPECTED_PACKET_TREE_SHA256="${1:-}"
RUN_MODE="${2:-gpu-evaluation}"

if [ -z "$EXPECTED_PACKET_TREE_SHA256" ]; then
  echo "expected packet tree SHA-256 argument is required" >&2
  exit 1
fi
if [ "$RUN_MODE" != gpu-evaluation ] && [ "$RUN_MODE" != packet-self-verify ]; then
  echo "runner mode must be gpu-evaluation or packet-self-verify" >&2
  exit 1
fi
if [ ! -d "$SOURCE_REPO" ]; then
  echo "immutable packet mount is missing" >&2
  exit 1
fi
if [ "$RUN_MODE" = gpu-evaluation ] && \
   { [ ! -d "$REMOTE_FINAL_MODEL" ] || [ ! -d "$REMOTE_BASE_MODEL" ]; }; then
  echo "read-only model mount is missing" >&2
  exit 1
fi
if [ "$RUN_MODE" = gpu-evaluation ] && [ -e "$WORKSPACE_ROOT" ]; then
  echo "refusing a non-empty final-evaluation workspace" >&2
  exit 1
fi

python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 \
  jsonschema==4.26.0 \
  numpy==2.5.1 \
  pyyaml==6.0.3 \
  safetensors==0.8.0 \
  sentencepiece==0.2.2 \
  transformers==4.57.6

export PYTHONPATH="$SOURCE_REPO/ml/scriber_polishing/src"
python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \
  --mode packet \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256"

if [ "$RUN_MODE" = packet-self-verify ]; then
  echo "cloud_packet_self_verification=complete training_steps_executed=0 predictions=0 evaluations=0" >&2
  exit 0
fi

read -r LAUNCH_ID OWNER_TOKEN < <(
  python - <<'PY'
import hashlib
import os
import uuid

job_id = os.environ.get("JOB_ID", "")
if not job_id or len(job_id) > 512:
    raise SystemExit("Hugging Face JOB_ID is required")
launch_id = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
print(launch_id, uuid.uuid4().hex)
PY
)
release_writer() {
  python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \
    --mode release-writer \
    --output-root "$OUTPUT_ROOT" \
    --launch-id "$LAUNCH_ID" \
    --owner-token "$OWNER_TOKEN" || true
}
trap release_writer EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py" \
  --mode claim-launch \
  --manifest "$SOURCE_REPO/ml/scriber_polishing/job-input/final-evaluation-manifest.json" \
  --output-root "$OUTPUT_ROOT" \
  --launch-id "$LAUNCH_ID" \
  --owner-token "$OWNER_TOKEN"

mkdir -p "$WORKSPACE_ROOT"
cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"

python - <<'PY'
import json
import platform
import torch
import transformers

runtime = {
    "python": platform.python_version(),
    "torch": torch.__version__,
    "cuda_build": torch.version.cuda,
    "transformers": transformers.__version__,
    "cuda_available": torch.cuda.is_available(),
    "bf16_supported": (
        torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    ),
    "gpu": (
        torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    ),
}
print("cloud_runtime=" + json.dumps(runtime, sort_keys=True), flush=True)
if not runtime["python"].startswith("3.12."):
    raise SystemExit("unexpected Python runtime")
if runtime["torch"] != "2.11.0+cu128":
    raise SystemExit("unexpected PyTorch runtime")
if runtime["transformers"] != "4.57.6":
    raise SystemExit("unexpected Transformers runtime")
if not runtime["cuda_available"] or not runtime["bf16_supported"]:
    raise SystemExit("CUDA BF16 runtime is unavailable")
PY

python scripts/verify_hf_final_evaluation.py \
  --mode model \
  --manifest job-input/final-evaluation-manifest.json \
  --model "$REMOTE_FINAL_MODEL" \
  --output "$WORKSPACE_ROOT/preflight-remote.json"

copy_complete=false
for copy_attempt in 1 2 3; do
  rm -rf "$LOCAL_FINAL_MODEL"
  if cp -a --no-preserve=ownership "$REMOTE_FINAL_MODEL" "$LOCAL_FINAL_MODEL"; then
    copy_complete=true
    break
  fi
  sleep "$copy_attempt"
done
if [ "$copy_complete" != true ]; then
  echo "failed to copy the bound final model from private storage" >&2
  exit 1
fi

python scripts/verify_hf_final_evaluation.py \
  --mode model \
  --manifest job-input/final-evaluation-manifest.json \
  --model "$LOCAL_FINAL_MODEL" \
  --output "$WORKSPACE_ROOT/preflight-local.json"

python scripts/verify_hf_final_evaluation.py \
  --mode base-model \
  --manifest job-input/final-evaluation-manifest.json \
  --model "$REMOTE_BASE_MODEL" \
  --output "$WORKSPACE_ROOT/preflight-base-remote.json"

base_copy_complete=false
for copy_attempt in 1 2 3; do
  rm -rf "$LOCAL_BASE_MODEL"
  if cp -a --no-preserve=ownership "$REMOTE_BASE_MODEL" "$LOCAL_BASE_MODEL"; then
    base_copy_complete=true
    break
  fi
  sleep "$copy_attempt"
done
if [ "$base_copy_complete" != true ]; then
  echo "failed to copy the revision-pinned base model" >&2
  exit 1
fi

python scripts/verify_hf_final_evaluation.py \
  --mode base-model \
  --manifest job-input/final-evaluation-manifest.json \
  --model "$LOCAL_BASE_MODEL" \
  --output "$WORKSPACE_ROOT/preflight-base-local.json"

python - "$LOCAL_FINAL_MODEL" <<'PY'
import json
import pathlib
import sys

from scriber_polishing.policy_artifact import (
    verify_frozen_lexical_policy_artifact,
)

binding = verify_frozen_lexical_policy_artifact(pathlib.Path(sys.argv[1]))
print("final_policy=" + json.dumps(binding, sort_keys=True), flush=True)
PY

mkdir -p "$OUTPUT_ROOT"
copy_immutable() {
  local source="$1"
  local destination="$2"
  if [ -L "$destination" ]; then
    echo "refusing symlinked immutable output: $destination" >&2
    exit 1
  fi
  if [ -e "$destination" ]; then
    cmp "$source" "$destination"
  else
    cp "$source" "$destination"
  fi
  sync "$destination"
}
copy_immutable \
  job-input/final-evaluation-manifest.json \
  "$OUTPUT_ROOT/final-evaluation-manifest.json"
copy_immutable \
  "$PACKET_ROOT/job-manifest.json" \
  "$OUTPUT_ROOT/job-manifest.json"
copy_immutable \
  job-input/hf-cost-audit.json \
  "$OUTPUT_ROOT/hf-cost-audit.json"
if [ -f job-input/continuation-manifest.json ]; then
  copy_immutable \
    job-input/continuation-manifest.json \
    "$OUTPUT_ROOT/continuation-manifest.json"
  copy_immutable \
    job-input/continuation-packet-verification.json \
    "$OUTPUT_ROOT/continuation-packet-verification.json"
else
  for evidence_name in \
    attempt9-finalization-source-contract.json \
    attempt9-finalization-verification.json \
    finalization-launch-guard.json; do
    copy_immutable \
      "job-input/$evidence_name" \
      "$OUTPUT_ROOT/$evidence_name"
  done
fi
copy_immutable \
  job-input/continuation-result.json \
  "$OUTPUT_ROOT/continuation-result.json"
copy_immutable \
  job-input/training-report.json \
  "$OUTPUT_ROOT/training-report.json"
copy_immutable \
  "$WORKSPACE_ROOT/preflight-remote.json" \
  "$OUTPUT_ROOT/preflight-remote.json"
copy_immutable \
  "$WORKSPACE_ROOT/preflight-local.json" \
  "$OUTPUT_ROOT/preflight-local.json"
copy_immutable \
  "$WORKSPACE_ROOT/preflight-base-remote.json" \
  "$OUTPUT_ROOT/preflight-base-remote.json"
copy_immutable \
  "$WORKSPACE_ROOT/preflight-base-local.json" \
  "$OUTPUT_ROOT/preflight-base-local.json"

evaluate_lane() {
  local candidate="$1"
  local suite_id="$2"
  local references="$3"
  local lane_root="$4"
  mkdir -p "$lane_root"
  if [ -e "$lane_root/evaluation.json" ]; then
    python scripts/verify_hf_final_evaluation.py \
      --mode recover-exit \
      --manifest job-input/final-evaluation-manifest.json \
      --candidate "$candidate" \
      --suite "$suite_id" \
      --lane-root "$lane_root"
  else
    set +e
    python scripts/evaluate_predictions.py \
      --references "$references" \
      --predictions "$lane_root/predictions.jsonl" \
      --candidate "$candidate" \
      --config configs/evaluation.yaml \
      --output "$lane_root/evaluation.json"
    local evaluation_exit=$?
    set -e
    if [ "$evaluation_exit" -ne 0 ] && [ "$evaluation_exit" -ne 2 ]; then
      exit "$evaluation_exit"
    fi
    python scripts/verify_hf_final_evaluation.py \
      --mode record-exit \
      --candidate "$candidate" \
      --suite "$suite_id" \
      --evaluation-exit "$evaluation_exit" \
      --output "$lane_root/evaluation-exit.json"
  fi
  sync "$lane_root/evaluation.json"
  python scripts/verify_hf_final_evaluation.py \
    --mode lane \
    --manifest job-input/final-evaluation-manifest.json \
    --candidate "$candidate" \
    --suite "$suite_id" \
    --lane-root "$lane_root"
}

run_final_suite() {
  local suite_id="$1"
  local references="$2"
  local lane_root="$OUTPUT_ROOT/final/$suite_id"
  local prediction_action
  prediction_action="$(
    python scripts/verify_hf_final_evaluation.py \
      --mode plan-predictions \
      --lane-root "$lane_root"
  )"
  if [ "$prediction_action" != verify ]; then
    python -m scriber_polishing.inference \
      --model "$LOCAL_FINAL_MODEL" \
      --references "$references" \
      --predictions-output "$lane_root/predictions.jsonl" \
      --manifest-output "$lane_root/predictions.manifest.json" \
      --candidate final \
      --resume-journal "$lane_root/predictions.resume.json" \
      --batch-size 8 \
      --max-new-tokens 512 \
      --device cuda \
      --quantization none \
      --local-files-only \
      --protection-policy-artifact "$LOCAL_FINAL_MODEL/scriber-protection-policy.json"
    python scripts/verify_hf_final_evaluation.py \
      --mode bind-prediction \
      --manifest job-input/final-evaluation-manifest.json \
      --candidate final \
      --model "$LOCAL_FINAL_MODEL" \
      --prediction-manifest "$lane_root/predictions.manifest.json" \
      --allow-create
  else
    python scripts/verify_hf_final_evaluation.py \
      --mode bind-prediction \
      --manifest job-input/final-evaluation-manifest.json \
      --candidate final \
      --model "$LOCAL_FINAL_MODEL" \
      --prediction-manifest "$lane_root/predictions.manifest.json"
  fi
  evaluate_lane final "$suite_id" "$references" "$lane_root"
}

run_base_suite() {
  local suite_id="$1"
  local references="$2"
  local lane_root="$OUTPUT_ROOT/base_model/$suite_id"
  local prediction_action
  prediction_action="$(
    python scripts/verify_hf_final_evaluation.py \
      --mode plan-predictions \
      --lane-root "$lane_root"
  )"
  if [ "$prediction_action" != verify ]; then
    python -m scriber_polishing.inference \
      --model "$LOCAL_BASE_MODEL" \
      --references "$references" \
      --predictions-output "$lane_root/predictions.jsonl" \
      --manifest-output "$lane_root/predictions.manifest.json" \
      --candidate base_model \
      --resume-journal "$lane_root/predictions.resume.json" \
      --batch-size 8 \
      --max-new-tokens 512 \
      --device cuda \
      --quantization none \
      --local-files-only
    python scripts/verify_hf_final_evaluation.py \
      --mode bind-prediction \
      --manifest job-input/final-evaluation-manifest.json \
      --candidate base_model \
      --model "$LOCAL_BASE_MODEL" \
      --prediction-manifest "$lane_root/predictions.manifest.json" \
      --allow-create
  else
    python scripts/verify_hf_final_evaluation.py \
      --mode bind-prediction \
      --manifest job-input/final-evaluation-manifest.json \
      --candidate base_model \
      --model "$LOCAL_BASE_MODEL" \
      --prediction-manifest "$lane_root/predictions.manifest.json"
  fi
  evaluate_lane base_model "$suite_id" "$references" "$lane_root"
}

while IFS=$'\t' read -r SUITE_ID REFERENCE_FILENAME; do
  REFERENCES="evaluation-input/$REFERENCE_FILENAME"
  run_final_suite "$SUITE_ID" "$REFERENCES"
  run_base_suite "$SUITE_ID" "$REFERENCES"
done < <(
  python scripts/verify_hf_final_evaluation.py \
    --mode list-suites \
    --manifest job-input/final-evaluation-manifest.json
)

python scripts/verify_hf_final_evaluation.py \
  --mode finalize \
  --manifest job-input/final-evaluation-manifest.json \
  --output-root "$OUTPUT_ROOT" \
  --output "$OUTPUT_ROOT/final-evaluation-result.json"

echo "cloud_final_evaluation=complete training_steps_executed=0" >&2
"""
    return (
        script.replace("__OUTPUT_ROOT__", _FINAL_EVALUATION_OUTPUT_ROOT)
        .replace("__BASE_MODEL_ID__", BASE_MODEL_ID)
        .replace("__BASE_MODEL_REVISION__", BASE_MODEL_REVISION)
        .replace("\r\n", "\n")
        .encode("utf-8")
    )


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
    if completed.returncode != 0 or len(value) != 40:
        raise FinalEvaluationError("cannot bind the source Git revision for final evaluation")
    return value


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise FinalEvaluationError(f"final-evaluation packet source is missing: {source}")
    shutil.copytree(
        source,
        destination,
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _packet_inventory(
    root: Path,
) -> tuple[list[dict[str, object]], str]:
    files: list[dict[str, object]] = []
    candidates = [(path.relative_to(root).as_posix(), path) for path in root.rglob("*")]
    for relative_string, path in sorted(candidates, key=lambda item: item[0]):
        if path.is_symlink():
            raise FinalEvaluationError(f"final-evaluation packet may not contain symlinks: {path}")
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            raise FinalEvaluationError(f"final-evaluation packet may not contain Python bytecode: {path}")
        if not path.is_file() or path == root / "job-manifest.json":
            continue
        payload = _stable_bytes(
            path,
            f"final-evaluation packet file {relative}",
        )
        files.append(
            {
                "path": relative_string,
                "bytes": len(payload),
                "sha256": sha256_bytes(payload),
            }
        )
    if not files:
        raise FinalEvaluationError("final-evaluation packet inventory is empty")
    tree = "sha256:" + sha256_bytes(canonical_json_bytes(files))
    return files, tree


def _validate_job_manifest(
    value: Mapping[str, object],
) -> dict[str, object]:
    if not _JOB_MANIFEST_SCHEMA.is_file():
        raise FinalEvaluationError(f"final-evaluation job schema is missing: {_JOB_MANIFEST_SCHEMA}")
    schema = json.loads(_JOB_MANIFEST_SCHEMA.read_bytes())
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise FinalEvaluationError(f"final-evaluation job manifest is invalid: {detail}")
    return dict(value)


def _evaluation_core_manifest(
    *,
    final_model: Mapping[str, object],
    continuation_evidence: Mapping[str, object],
    budget: Mapping[str, object],
    references: Mapping[str, Mapping[str, object]],
    retry_evidence: Mapping[str, object] | None = None,
    evaluation_flavor: str = _DEFAULT_FINAL_EVALUATION_FLAVOR,
) -> dict[str, object]:
    evaluation_flavor = _validated_final_evaluation_flavor(evaluation_flavor)
    validated_continuation = _validate_embedded_continuation_evidence(continuation_evidence)
    validated_retry = _validate_final_evaluation_retry_evidence(retry_evidence) if retry_evidence is not None else None
    expected_budget = build_final_evaluation_budget(
        continuation_evidence=validated_continuation,
        total_budget_usd=str(budget.get("total_budget_usd")),
        retry_evidence=validated_retry,
        evaluation_flavor=evaluation_flavor,
    )
    if dict(budget) != expected_budget:
        raise FinalEvaluationError("continuation evidence differs from the final-evaluation budget")
    policy = frozen_lexical_policy_requirement()
    manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation",
        "source_git_head": _git_head(),
        "image": HF_MARKER_REPAIR_IMAGE,
        "flavor": evaluation_flavor,
        "batch_size": _FINAL_EVALUATION_BATCH_SIZE,
        "max_new_tokens": 512,
        "base_model": {
            "model_id": BASE_MODEL_ID,
            "revision": BASE_MODEL_REVISION,
            "remote_model_uri": _BASE_MODEL_VOLUME_URI,
            "mount_mode": "read_only",
            **dict(_BASE_MODEL_INVENTORY),
            "preprocessing_policy": "implicit_legacy_default",
        },
        "final_model": {
            **dict(final_model),
            "preprocessing_policy": {
                "artifact_sha256": policy["artifact_sha256"],
                "policy_id": policy["policy_id"],
                "policy_sha256": policy["policy_sha256"],
            },
        },
        "continuation_evidence": validated_continuation,
        "references": {key: dict(value) for key, value in sorted(references.items())},
        "evaluation": {
            "config_filename": "evaluation.yaml",
            "config_sha256": ("656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998"),
            "candidates": ["final", "base_model"],
            "reports_per_candidate": len(references),
        },
        "budget": expected_budget,
        "training": {
            "training_steps_executed": 0,
            "reference_usage": "inference_and_evaluation_only",
            "train_split_allowed": False,
        },
        "output": {
            "bucket_root": _FINAL_EVALUATION_OUTPUT_BUCKET,
            "relative_path": ("attempt8/scriber-1100-final-evaluation-v1"),
            "container_path": _FINAL_EVALUATION_OUTPUT_ROOT,
            "unique": True,
        },
        "publication": {
            "allowed": False,
            "destination_kind": "private_hf_bucket",
        },
    }
    if validated_retry is not None:
        manifest["retry_evidence"] = validated_retry
    return manifest


def build_hf_final_evaluation_packet(
    *,
    continuation_result_path: str | Path,
    continuation_job_id: str,
    continuation_running_seconds: int,
    expected_continuation_packet_tree_sha256: str,
    remote_model_uri: str,
    total_budget_usd: str,
    reference_root: str | Path,
    output_dir: str | Path,
    finalization_job_id: str | None = None,
    finalization_running_seconds: int | None = None,
    expected_finalization_packet_tree_sha256: str | None = None,
    failed_evaluations: Sequence[Mapping[str, object]] = (),
    auxiliary_diagnostic_jobs: Sequence[Mapping[str, object]] = (),
    evaluation_flavor: str = _DEFAULT_FINAL_EVALUATION_FLAVOR,
) -> HfFinalEvaluationPacket:
    """Build the immutable packet without uploading or launching anything."""

    evaluation_flavor = _validated_final_evaluation_flavor(evaluation_flavor)
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise FinalEvaluationError(f"refusing to overwrite final-evaluation packet: {target}")
    bound_continuation = bind_final_continuation_evidence(
        continuation_result_path=continuation_result_path,
        continuation_job_id=continuation_job_id,
        continuation_running_seconds=continuation_running_seconds,
        expected_packet_tree_sha256=(expected_continuation_packet_tree_sha256),
        remote_model_uri=remote_model_uri,
        total_budget_usd=total_budget_usd,
        finalization_job_id=finalization_job_id,
        finalization_running_seconds=finalization_running_seconds,
        expected_finalization_packet_tree_sha256=(expected_finalization_packet_tree_sha256),
    )
    if not failed_evaluations and not auxiliary_diagnostic_jobs:
        retry_evidence = None
    else:
        retry_evidence = bind_final_evaluation_retry_evidence(
            failed_evaluations=failed_evaluations,
            auxiliary_diagnostic_jobs=auxiliary_diagnostic_jobs,
        )
    bound_continuation["budget"] = build_final_evaluation_budget(
        continuation_evidence=bound_continuation["continuation_evidence"],
        total_budget_usd=total_budget_usd,
        retry_evidence=retry_evidence,
        evaluation_flavor=evaluation_flavor,
    )
    references = validate_final_evaluation_references(reference_root)
    core = _evaluation_core_manifest(
        final_model=bound_continuation["final_model"],
        continuation_evidence=bound_continuation["continuation_evidence"],
        budget=bound_continuation["budget"],
        references=references,
        retry_evidence=retry_evidence,
        evaluation_flavor=evaluation_flavor,
    )
    continuation_root = Path(continuation_result_path).expanduser().resolve().parent
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    try:
        project = temporary / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        _copy_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_tree(_PROJECT_ROOT / "scripts", project / "scripts")
        _copy_tree(_PROJECT_ROOT / "contracts", project / "contracts")
        (project / "configs").mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "configs/evaluation.yaml",
            project / "configs/evaluation.yaml",
        )
        for filename in ("README.md", "pyproject.toml"):
            shutil.copy2(
                _PROJECT_ROOT / filename,
                project / filename,
            )
        input_root = project / "evaluation-input"
        input_root.mkdir()
        source_references = Path(reference_root).expanduser().resolve()
        for suite in references.values():
            for key in ("records_filename", "manifest_filename"):
                filename = str(suite[key])
                shutil.copy2(
                    source_references / filename,
                    input_root / filename,
                )
        job_input = project / "job-input"
        job_input.mkdir()
        evidence_sources = bound_continuation.get("evidence_sources")
        if isinstance(evidence_sources, Mapping):
            for filename, source in evidence_sources.items():
                shutil.copy2(Path(source), job_input / str(filename))
            (job_input / "hf-cost-audit.json").write_bytes(canonical_json_bytes(build_hf_cost_audit()))
        else:
            shutil.copy2(
                continuation_root / "continuation-result.json",
                job_input / "continuation-result.json",
            )
            shutil.copy2(
                continuation_root / "training-report.json",
                job_input / "training-report.json",
            )
            shutil.copy2(
                continuation_root / "continuation-manifest.json",
                job_input / "continuation-manifest.json",
            )
            shutil.copy2(
                continuation_root / "hf-cost-audit.json",
                job_input / "hf-cost-audit.json",
            )
            shutil.copy2(
                continuation_root / "scriber-1100-packet-verification.json",
                job_input / "continuation-packet-verification.json",
            )
        (job_input / "final-evaluation-manifest.json").write_bytes(canonical_json_bytes(core))
        runner = final_evaluation_runner_payload()
        (temporary / "run-final-evaluation.sh").write_bytes(runner)
        inventory, tree_sha256 = _packet_inventory(temporary)
        manifest = {
            **core,
            "runner_sha256": "sha256:" + sha256_bytes(runner),
            "packet_tree_sha256": tree_sha256,
            "file_count": len(inventory),
            "byte_count": sum(int(item["bytes"]) for item in inventory),
        }
        validated_manifest = _validate_job_manifest(manifest)
        (temporary / "job-manifest.json").write_bytes(canonical_json_bytes(validated_manifest))
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HfFinalEvaluationPacket(
        output_dir=target,
        manifest=MappingProxyType(validated_manifest),
    )


def _load_packet_manifest(packet: Path) -> dict[str, object]:
    payload = _stable_bytes(
        packet / "job-manifest.json",
        "final-evaluation job manifest",
    )
    manifest = _json_object(payload, "final-evaluation job manifest")
    if payload != canonical_json_bytes(manifest):
        raise FinalEvaluationError("final-evaluation job manifest is not canonical JSON")
    manifest = _validate_job_manifest(manifest)
    inventory, tree = _packet_inventory(packet)
    if (
        manifest.get("packet_tree_sha256") != tree
        or manifest.get("file_count") != len(inventory)
        or manifest.get("byte_count") != sum(int(item["bytes"]) for item in inventory)
    ):
        raise FinalEvaluationError("final-evaluation packet differs from its immutable inventory")
    return manifest


def build_hf_final_evaluation_launch_contract(
    packet_dir: str | Path,
    *,
    launch_kind: str = "gpu-evaluation",
) -> dict[str, object]:
    """Build, but do not execute, one CPU self-check or private GPU launch."""

    if launch_kind not in {"cpu-self-verify", "gpu-evaluation"}:
        raise FinalEvaluationError("launch kind must be cpu-self-verify or gpu-evaluation")
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise FinalEvaluationError(f"final-evaluation packet must not be a symlink: {candidate}")
    packet = candidate.resolve()
    if not packet.is_dir():
        raise FinalEvaluationError(f"final-evaluation packet is missing: {packet}")
    manifest = _load_packet_manifest(packet)
    final_model = manifest.get("final_model")
    continuation_evidence = manifest.get("continuation_evidence")
    retry_evidence = manifest.get("retry_evidence")
    budget = manifest.get("budget")
    output = manifest.get("output")
    evaluation_flavor = _validated_final_evaluation_flavor(manifest.get("flavor"))
    if (
        not isinstance(final_model, Mapping)
        or not isinstance(continuation_evidence, Mapping)
        or not isinstance(budget, Mapping)
        or not isinstance(output, Mapping)
    ):
        raise FinalEvaluationError("final-evaluation packet has no model/evidence/output binding")
    expected_budget = build_final_evaluation_budget(
        continuation_evidence=continuation_evidence,
        total_budget_usd=str(budget.get("total_budget_usd")),
        retry_evidence=(
            _validate_final_evaluation_retry_evidence(retry_evidence) if isinstance(retry_evidence, Mapping) else None
        ),
        evaluation_flavor=evaluation_flavor,
    )
    if retry_evidence is not None and not isinstance(retry_evidence, Mapping):
        raise FinalEvaluationError("final-evaluation retry evidence must be an object")
    if dict(budget) != expected_budget:
        raise FinalEvaluationError("final-evaluation packet budget differs from its actual continuation evidence")
    model_uri = final_model.get("remote_model_uri")
    if not isinstance(model_uri, str):
        raise FinalEvaluationError("final-evaluation packet has no remote model URI")
    packet_volume = f"{packet}:/input/repo:ro"
    packet_tree = str(manifest["packet_tree_sha256"])
    recovery_chain = continuation_evidence.get("recovery_chain")
    common = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_launch_contract",
        "launch_kind": launch_kind,
        "total_budget_usd": budget["total_budget_usd"],
        "continuation_job_id": continuation_evidence["job_id"],
        "continuation_packet_tree_sha256": continuation_evidence["continuation_packet_tree_sha256"],
        **(
            {
                "recovery_job_id": recovery_chain["recovery_job_id"],
                "finalization_job_id": recovery_chain["finalization_job_id"],
                "finalization_packet_tree_sha256": recovery_chain["finalization_packet_tree_sha256"],
            }
            if isinstance(recovery_chain, Mapping)
            else {}
        ),
        **({"retry_evidence": dict(retry_evidence)} if isinstance(retry_evidence, Mapping) else {}),
        "packet_volume": packet_volume,
        "external_job_started": False,
        "publication_allowed": False,
    }
    if launch_kind == "cpu-self-verify":
        maximum_job_cost = (Decimal("0.01") * Decimal(_CPU_SELF_VERIFY_TIMEOUT_MINUTES) / Decimal(60)).quantize(
            Decimal("0.000001")
        )
        maximum_total = Decimal(str(budget["actual_before_evaluation_usd"])) + maximum_job_cost
        return {
            **common,
            "flavor": "cpu-basic",
            "timeout": f"{_CPU_SELF_VERIFY_TIMEOUT_MINUTES}m",
            "maximum_job_cost_usd": format(maximum_job_cost, ".6f"),
            "maximum_total_cost_usd": format(maximum_total, ".6f"),
            "packet_self_verification_only": True,
            "training_steps_executed": 0,
            "prediction_records_allowed": 0,
            "evaluation_reports_allowed": 0,
            "output_claim_allowed": False,
            "argv": [
                "hf",
                "jobs",
                "run",
                "--flavor",
                "cpu-basic",
                "--timeout",
                f"{_CPU_SELF_VERIFY_TIMEOUT_MINUTES}m",
                "--label",
                "project=scriber-polishing",
                "--label",
                "run=final-evaluation-packet-self-verify",
                "-v",
                packet_volume,
                HF_MARKER_REPAIR_IMAGE,
                "bash",
                "/input/repo/run-final-evaluation.sh",
                packet_tree,
                "packet-self-verify",
            ],
        }
    model_volume = f"{model_uri}:/final-model:ro"
    base_model_volume = f"{_BASE_MODEL_VOLUME_URI}:/base-model:ro"
    output_volume = f"{_FINAL_EVALUATION_OUTPUT_BUCKET}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        evaluation_flavor,
        "--timeout",
        "120m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=final-evaluation-1100",
        "-v",
        packet_volume,
        "-v",
        model_volume,
        "-v",
        base_model_volume,
        "-v",
        output_volume,
        HF_MARKER_REPAIR_IMAGE,
        "bash",
        "/input/repo/run-final-evaluation.sh",
        packet_tree,
    ]
    return {
        **common,
        "flavor": evaluation_flavor,
        "timeout": "120m",
        "maximum_job_cost_usd": budget["evaluation_maximum_cost_usd"],
        "maximum_total_cost_usd": budget["maximum_total_cost_usd"],
        "packet_volume": packet_volume,
        "model_volume": model_volume,
        "base_model_volume": base_model_volume,
        "output_volume": output_volume,
        "output_root": _FINAL_EVALUATION_OUTPUT_ROOT,
        "remote_result_uri": (_FINAL_EVALUATION_OUTPUT_BUCKET + "/attempt8/scriber-1100-final-evaluation-v1"),
        "argv": argv,
        "packet_self_verification_only": False,
    }


def _load_core_manifest(
    manifest_path: str | Path,
) -> tuple[Path, dict[str, object]]:
    path = Path(manifest_path).expanduser().resolve()
    payload = _stable_bytes(path, "final-evaluation core manifest")
    manifest = _json_object(payload, "final-evaluation core manifest")
    if payload != canonical_json_bytes(manifest):
        raise FinalEvaluationError("final-evaluation core manifest is not canonical JSON")
    expected_base = {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "remote_model_uri": _BASE_MODEL_VOLUME_URI,
        "mount_mode": "read_only",
        **dict(_BASE_MODEL_INVENTORY),
        "preprocessing_policy": "implicit_legacy_default",
    }
    expected_training = {
        "training_steps_executed": 0,
        "reference_usage": "inference_and_evaluation_only",
        "train_split_allowed": False,
    }
    expected_publication = {
        "allowed": False,
        "destination_kind": "private_hf_bucket",
    }
    expected_output = {
        "bucket_root": _FINAL_EVALUATION_OUTPUT_BUCKET,
        "relative_path": "attempt8/scriber-1100-final-evaluation-v1",
        "container_path": _FINAL_EVALUATION_OUTPUT_ROOT,
        "unique": True,
    }
    final_model = manifest.get("final_model")
    continuation_evidence = manifest.get("continuation_evidence")
    retry_evidence = manifest.get("retry_evidence")
    budget = manifest.get("budget")
    references = manifest.get("references")
    evaluation = manifest.get("evaluation")
    evaluation_flavor = _validated_final_evaluation_flavor(manifest.get("flavor"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "scriber_hf_final_evaluation"
        or manifest.get("image") != HF_MARKER_REPAIR_IMAGE
        or manifest.get("batch_size") != _FINAL_EVALUATION_BATCH_SIZE
        or manifest.get("max_new_tokens") != 512
        or manifest.get("base_model") != expected_base
        or manifest.get("training") != expected_training
        or manifest.get("publication") != expected_publication
        or manifest.get("output") != expected_output
        or not isinstance(final_model, Mapping)
        or not isinstance(continuation_evidence, Mapping)
        or not isinstance(budget, Mapping)
        or not isinstance(references, Mapping)
        or not isinstance(evaluation, Mapping)
    ):
        raise FinalEvaluationError("final-evaluation core manifest differs from the frozen contract")
    validated_continuation = _validate_embedded_continuation_evidence(continuation_evidence)
    validated_retry = (
        _validate_final_evaluation_retry_evidence(retry_evidence) if isinstance(retry_evidence, Mapping) else None
    )
    if retry_evidence is not None and validated_retry is None:
        raise FinalEvaluationError("final-evaluation retry evidence must be an object")
    expected_budget = build_final_evaluation_budget(
        continuation_evidence=validated_continuation,
        total_budget_usd=str(budget.get("total_budget_usd")),
        retry_evidence=validated_retry,
        evaluation_flavor=evaluation_flavor,
    )
    if dict(budget) != expected_budget:
        raise FinalEvaluationError("continuation evidence and final-evaluation budget differ")
    policy = frozen_lexical_policy_requirement()
    if final_model.get("preprocessing_policy") != {
        "artifact_sha256": policy["artifact_sha256"],
        "policy_id": policy["policy_id"],
        "policy_sha256": policy["policy_sha256"],
    }:
        raise FinalEvaluationError("final model does not bind the frozen lexical policy")
    recovery_chain = validated_continuation.get("recovery_chain")
    expected_remote_model_uri = (
        recovery_chain.get("remote_model_uri")
        if isinstance(recovery_chain, Mapping)
        else (
            str(validated_continuation["remote_result_uri"]).rstrip("/")
            + "/training/"
            + str(final_model.get("final_model_directory"))
        )
    )
    if (
        final_model.get("cumulative_updates") != 1100
        or final_model.get("replayed_updates") != 0
        or final_model.get("mount_mode") != "read_only"
        or final_model.get("continuation_result_sha256") != validated_continuation["continuation_result_sha256"]
        or final_model.get("training_report_sha256") != validated_continuation["training_report_sha256"]
        or final_model.get("remote_model_uri") != expected_remote_model_uri
    ):
        raise FinalEvaluationError("final model does not bind the cumulative-1100 continuation")
    suite_ids = list(references)
    if dict(references) != _frozen_reference_bindings():
        raise FinalEvaluationError("final evaluation must contain the exact three frozen suites")
    if (
        len(suite_ids) != 3
        or any(not isinstance(suite_id, str) or _SUITE_ID.fullmatch(suite_id) is None for suite_id in suite_ids)
        or evaluation.get("config_filename") != "evaluation.yaml"
        or evaluation.get("config_sha256") != "656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998"
        or evaluation.get("candidates") != ["final", "base_model"]
        or evaluation.get("reports_per_candidate") != 3
    ):
        raise FinalEvaluationError("final-evaluation suite/report contract is invalid")
    for suite_id, raw_suite in references.items():
        if not isinstance(raw_suite, Mapping):
            raise FinalEvaluationError(f"reference suite {suite_id!r} must be an object")
        splits = raw_suite.get("split_counts")
        if (
            raw_suite.get("usage") != "inference_and_evaluation_only"
            or raw_suite.get("record_count") is None
            or not isinstance(splits, Mapping)
            or "train" in splits
            or any(split not in {"test", "challenge"} for split in splits)
        ):
            raise FinalEvaluationError(f"reference suite {suite_id!r} is not evaluation-only")
    return path, manifest


def verify_hf_final_evaluation_packet(
    packet_root: str | Path,
    *,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    """Verify the complete local packet against an out-of-band tree hash."""

    root = Path(packet_root).expanduser().resolve()
    manifest = _load_packet_manifest(root)
    if manifest.get("packet_tree_sha256") != expected_packet_tree_sha256:
        raise FinalEvaluationError("final-evaluation packet tree differs from the launch binding")
    core_path = root / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    _, core = _load_core_manifest(core_path)
    packet_fields = {
        "runner_sha256",
        "packet_tree_sha256",
        "file_count",
        "byte_count",
    }
    manifest_core = {key: value for key, value in manifest.items() if key not in packet_fields}
    if manifest_core != core:
        raise FinalEvaluationError("packet and core final-evaluation manifests differ")
    runner_payload = _stable_bytes(
        root / "run-final-evaluation.sh",
        "final-evaluation runner",
    )
    if manifest.get("runner_sha256") != ("sha256:" + _sha256(runner_payload)):
        raise FinalEvaluationError("final-evaluation runner differs from its binding")
    project = core_path.parents[1]
    references = validate_final_evaluation_references(project / "evaluation-input")
    if core.get("references") != references:
        raise FinalEvaluationError("packet references differ from the core manifest")
    evaluation_payload = _stable_bytes(
        project / "configs/evaluation.yaml",
        "frozen evaluation configuration",
    )
    if _sha256(evaluation_payload) != core["evaluation"]["config_sha256"]:
        raise FinalEvaluationError("packet evaluation configuration differs from its binding")
    audit_payload = _stable_bytes(
        project / "job-input/hf-cost-audit.json",
        "HF cost audit",
    )
    if audit_payload != canonical_json_bytes(build_hf_cost_audit()):
        raise FinalEvaluationError("packet HF cost audit differs from the reviewed ledger")
    continuation_evidence = core["continuation_evidence"]
    recovery_chain = continuation_evidence.get("recovery_chain")
    if isinstance(recovery_chain, Mapping):
        source_contract_payload = _stable_bytes(
            project / "job-input/attempt9-finalization-source-contract.json",
            "packet Attempt-9 finalization source contract",
        )
        source_verification_payload = _stable_bytes(
            project / "job-input/attempt9-finalization-verification.json",
            "packet Attempt-9 finalization verification",
        )
        guard_payload = _stable_bytes(
            project / "job-input/finalization-launch-guard.json",
            "packet Attempt-10 finalization guard",
        )
        if (
            source_contract_payload != canonical_json_bytes(attempt9_finalization_source_contract())
            or recovery_chain["source_contract_sha256"] != "sha256:" + _sha256(source_contract_payload)
            or recovery_chain["source_verification_sha256"] != "sha256:" + _sha256(source_verification_payload)
            or recovery_chain["finalization_guard_sha256"] != "sha256:" + _sha256(guard_payload)
            or continuation_evidence["packet_verification_sha256"] != "sha256:" + _sha256(source_verification_payload)
            or continuation_evidence["cost_audit_sha256"] != "sha256:" + _sha256(audit_payload)
        ):
            raise FinalEvaluationError("packet no-retrain recovery evidence differs from its binding")
    else:
        continuation_payload = _stable_bytes(
            project / "job-input/continuation-manifest.json",
            "packet continuation manifest",
        )
        packet_verification_payload = _stable_bytes(
            project / "job-input/continuation-packet-verification.json",
            "packet continuation packet verification",
        )
        if (
            continuation_evidence["continuation_manifest_sha256"] != "sha256:" + _sha256(continuation_payload)
            or continuation_evidence["packet_verification_sha256"] != "sha256:" + _sha256(packet_verification_payload)
            or continuation_evidence["cost_audit_sha256"] != "sha256:" + _sha256(audit_payload)
        ):
            raise FinalEvaluationError("packet continuation job evidence differs from its binding")
    final_model = core["final_model"]
    result_payload = _stable_bytes(
        project / "job-input/continuation-result.json",
        "packet continuation result",
    )
    report_payload = _stable_bytes(
        project / "job-input/training-report.json",
        "packet continuation training report",
    )
    if (
        final_model["continuation_result_sha256"] != "sha256:" + _sha256(result_payload)
        or final_model["training_report_sha256"] != "sha256:" + _sha256(report_payload)
        or continuation_evidence["continuation_result_sha256"] != final_model["continuation_result_sha256"]
        or continuation_evidence["training_report_sha256"] != final_model["training_report_sha256"]
    ):
        raise FinalEvaluationError("packet continuation evidence differs from the model binding")
    return manifest


def _write_immutable_json(
    path: str | Path,
    value: object,
) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FinalEvaluationError(f"immutable JSON output must not be a symlink: {candidate}")
    destination = candidate.resolve()
    payload = canonical_json_bytes(value)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.is_symlink():
        raise FinalEvaluationError(f"immutable JSON temporary must not be a symlink: {temporary}")
    if destination.exists():
        existing = _stable_bytes(destination, "immutable JSON output")
        if existing != payload:
            raise FinalEvaluationError(f"refusing to overwrite different output: {destination}")
        if temporary.exists():
            pending = _stable_bytes(temporary, "known immutable JSON temporary")
            if pending != payload:
                raise FinalEvaluationError(f"temporary output differs from immutable output: {temporary}")
            temporary.unlink()
            _fsync_parent(destination)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        pending = _stable_bytes(temporary, "known immutable JSON temporary")
        if pending != payload:
            raise FinalEvaluationError(f"temporary output differs from expected output: {temporary}")
        _commit_durable_temporary(temporary, destination)
        return destination
    _write_new_durable_payload(temporary, payload)
    _commit_durable_temporary(temporary, destination)
    return destination


def verify_final_evaluation_model(
    *,
    manifest_path: str | Path,
    model_root: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Verify final-model bytes and frozen lexical policy before inference."""

    _, manifest = _load_core_manifest(manifest_path)
    root = Path(model_root).expanduser().resolve()
    try:
        from .inference import _inventory_local_model_tree
        from .policy_artifact import verify_frozen_lexical_policy_artifact

        inventory = _inventory_local_model_tree(root)
        policy = verify_frozen_lexical_policy_artifact(root)
    except (OSError, ValueError) as error:
        raise FinalEvaluationError(f"final model preflight failed: {error}") from error
    final_model = manifest["final_model"]
    if (
        inventory.get("tree_sha256") != final_model["final_model_tree_sha256"]
        or inventory.get("file_count") != final_model["final_model_file_count"]
        or inventory.get("total_bytes") != final_model["final_model_total_bytes"]
    ):
        raise FinalEvaluationError("mounted final model differs from the continuation tree")
    expected_policy = final_model["preprocessing_policy"]
    if any(policy.get(key) != value for key, value in expected_policy.items()):
        raise FinalEvaluationError("mounted final model differs from the frozen lexical policy")
    report = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_model_preflight",
        "status": "passed",
        "cumulative_updates": 1100,
        "model_tree_sha256": inventory["tree_sha256"],
        "model_file_count": inventory["file_count"],
        "model_total_bytes": inventory["total_bytes"],
        "preprocessing_policy": expected_policy,
    }
    _write_immutable_json(output_path, report)
    return report


def verify_base_evaluation_model(
    *,
    manifest_path: str | Path,
    model_root: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Verify the exact revision-mounted base and absence of Scriber policy."""

    _, manifest = _load_core_manifest(manifest_path)
    root = Path(model_root).expanduser().resolve()
    try:
        from .inference import _inventory_local_model_tree

        inventory = _inventory_local_model_tree(root)
        config = _json_object(
            _stable_bytes(root / "config.json", "base model config"),
            "base model config",
        )
    except (OSError, ValueError) as error:
        raise FinalEvaluationError(f"base model preflight failed: {error}") from error
    base = manifest["base_model"]
    expected_inventory = {
        key: base[key]
        for key in (
            "tree_sha256",
            "config_sha256",
            "file_count",
            "total_bytes",
        )
    }
    if inventory != expected_inventory:
        raise FinalEvaluationError("mounted base model differs from the pinned revision inventory")
    if (
        (root / "scriber-protection-policy.json").exists()
        or config.get("scriber_protection_policy_id") is not None
        or config.get("scriber_protection_policy_sha256") is not None
        or config.get("model_type") != "gemma3_text"
        or config.get("architectures") != ["Gemma3ForCausalLM"]
    ):
        raise FinalEvaluationError("base model is not the unchanged implicit-legacy Gemma")
    report = {
        "schema_version": 1,
        "kind": "scriber_hf_base_evaluation_model_preflight",
        "status": "passed",
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "remote_model_uri": _BASE_MODEL_VOLUME_URI,
        "inventory": inventory,
        "preprocessing_policy": "implicit_legacy_default",
        "scriber_policy_artifact_present": False,
    }
    _write_immutable_json(output_path, report)
    return report


def list_final_evaluation_suites(
    manifest_path: str | Path,
) -> list[tuple[str, str]]:
    """List suite IDs and filenames in deterministic runner order."""

    _, manifest = _load_core_manifest(manifest_path)
    references = manifest["references"]
    return [(suite_id, str(references[suite_id]["records_filename"])) for suite_id in sorted(references)]


def plan_final_evaluation_prediction_lane(
    lane_root: str | Path,
) -> str:
    """Choose the only safe action for one resumable prediction lane."""

    raw_root = Path(lane_root).expanduser()
    if raw_root.is_symlink():
        raise FinalEvaluationError("final-evaluation prediction lane must not be a symlink")
    root = raw_root.resolve()
    removed_temporary = False
    for pattern in (
        ".predictions.jsonl.*.tmp",
        ".predictions.manifest.json.*.tmp",
        ".predictions.resume.json.*.tmp",
    ):
        for temporary in sorted(root.glob(pattern)):
            if temporary.is_symlink() or not temporary.is_file():
                raise FinalEvaluationError(
                    f"known final-evaluation prediction temporary is not a regular file: {temporary}"
                )
            temporary.unlink()
            removed_temporary = True
    if removed_temporary:
        _fsync_parent(root / "predictions.jsonl")
    predictions = root / "predictions.jsonl"
    manifest = root / "predictions.manifest.json"
    journal = root / "predictions.resume.json"
    for path in (predictions, manifest, journal):
        if path.is_symlink():
            raise FinalEvaluationError(f"final-evaluation prediction artifact must not be a symlink: {path}")
    if journal.exists():
        return "resume"
    predictions_exist = predictions.exists()
    manifest_exists = manifest.exists()
    if predictions_exist and manifest_exists:
        return "verify"
    if not predictions_exist and not manifest_exists:
        return "start"
    raise FinalEvaluationError("final-evaluation lane has an orphaned prediction artifact without a resume journal")


def _prediction_model_identity(
    manifest: Mapping[str, object],
    candidate: str,
) -> dict[str, object]:
    if candidate == "final":
        model = manifest["final_model"]
        return {
            "kind": "scriber_final_evaluation_prediction_model_identity",
            "candidate": "final",
            "cumulative_updates": 1100,
            "model_tree_sha256": model["final_model_tree_sha256"],
            "model_file_count": model["final_model_file_count"],
            "model_total_bytes": model["final_model_total_bytes"],
            "preprocessing_policy": model["preprocessing_policy"],
        }
    if candidate == "base_model":
        model = manifest["base_model"]
        return {
            "kind": "scriber_final_evaluation_prediction_model_identity",
            "candidate": "base_model",
            "model_id": model["model_id"],
            "revision": model["revision"],
            "tree_sha256": model["tree_sha256"],
            "config_sha256": model["config_sha256"],
            "file_count": model["file_count"],
            "total_bytes": model["total_bytes"],
            "preprocessing_policy": "implicit_legacy_default",
        }
    raise FinalEvaluationError("unknown final-evaluation candidate")


def _verify_prediction_policy(
    prediction_manifest: Mapping[str, object],
    manifest: Mapping[str, object],
    candidate: str,
) -> None:
    if candidate == "final":
        policy = prediction_manifest.get("protection_policy")
        expected = manifest["final_model"]["preprocessing_policy"]
        if (
            prediction_manifest.get("revision") is not None
            or not isinstance(policy, Mapping)
            or policy.get("policy_id") != expected["policy_id"]
            or policy.get("policy_sha256") != expected["policy_sha256"]
            or ("sha256:" + str(policy.get("artifact_sha256")) != expected["artifact_sha256"])
        ):
            raise FinalEvaluationError("final prediction lane does not use the frozen lexical policy")
    elif prediction_manifest.get("revision") is not None or "protection_policy" in prediction_manifest:
        raise FinalEvaluationError("base prediction lane is not pinned to implicit legacy preprocessing")


def bind_final_evaluation_prediction_manifest(
    *,
    manifest_path: str | Path,
    prediction_manifest_path: str | Path,
    candidate: str,
    model_root: str | Path,
    allow_create: bool,
) -> dict[str, object]:
    """Bind one produced prediction manifest to the exact preflight model."""

    prediction_candidate = Path(prediction_manifest_path).expanduser()
    if prediction_candidate.is_symlink():
        raise FinalEvaluationError("final-evaluation prediction manifest must not be a symlink")
    prediction_path = prediction_candidate.resolve()
    prediction_manifest = _json_object(
        _stable_bytes(prediction_path, "final-evaluation prediction manifest"),
        "final-evaluation prediction manifest",
    )
    prediction_temporary = prediction_path.with_name(prediction_path.name + ".tmp")
    if prediction_temporary.is_symlink():
        raise FinalEvaluationError("final-evaluation prediction manifest temporary must not be a symlink")
    pending_manifest: dict[str, object] | None = None
    if prediction_temporary.exists():
        pending_payload = _stable_bytes(
            prediction_temporary,
            "final-evaluation prediction manifest temporary",
        )
        pending_manifest = _json_object(
            pending_payload,
            "final-evaluation prediction manifest temporary",
        )
        if pending_payload != canonical_json_bytes(pending_manifest):
            raise FinalEvaluationError("final-evaluation prediction manifest temporary is not canonical JSON")
    existing_identity = prediction_manifest.get("final_evaluation_model_identity")
    if existing_identity is None and pending_manifest is None and not allow_create:
        raise FinalEvaluationError("preexisting final-evaluation prediction manifest has no exact model identity")
    _, manifest = _load_core_manifest(manifest_path)
    if prediction_manifest.get("candidate") != candidate:
        raise FinalEvaluationError("final-evaluation prediction candidate differs from its lane")
    raw_model_root = Path(model_root).expanduser()
    if raw_model_root.is_symlink():
        raise FinalEvaluationError("final-evaluation prediction model root must not be a symlink")
    root = raw_model_root.resolve()
    if not root.is_dir():
        raise FinalEvaluationError("final-evaluation prediction model root is missing")
    raw_declared_model = prediction_manifest.get("model")
    if not isinstance(raw_declared_model, str) or Path(raw_declared_model).expanduser().resolve() != root:
        raise FinalEvaluationError("final-evaluation prediction manifest names a different local model")
    _verify_prediction_policy(prediction_manifest, manifest, candidate)
    expected_identity = _prediction_model_identity(manifest, candidate)
    if existing_identity is not None and existing_identity != expected_identity:
        raise FinalEvaluationError("final-evaluation prediction model identity differs from the preflight")
    bound_manifest = {
        **prediction_manifest,
        "final_evaluation_model_identity": expected_identity,
    }
    if pending_manifest is not None:
        if pending_manifest != bound_manifest:
            raise FinalEvaluationError(
                "final-evaluation prediction manifest temporary differs from the exact model binding"
            )
        _replace_mutable_canonical_json(prediction_path, bound_manifest)
        return bound_manifest
    if existing_identity is not None:
        return prediction_manifest
    _replace_mutable_canonical_json(prediction_path, bound_manifest)
    return bound_manifest


def record_final_evaluation_exit(
    *,
    candidate: str,
    suite_id: str,
    evaluation_exit: int,
    output_path: str | Path,
) -> dict[str, object]:
    """Record only the two expected deterministic gate exits."""

    if candidate not in {"final", "base_model"}:
        raise FinalEvaluationError("final-evaluation candidate must be final or base_model")
    if _SUITE_ID.fullmatch(suite_id) is None:
        raise FinalEvaluationError("final-evaluation suite ID is invalid")
    if evaluation_exit not in {0, 2}:
        raise FinalEvaluationError("final-evaluation gate exit must be 0 or 2")
    value = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_gate_exit",
        "candidate": candidate,
        "suite": suite_id,
        "evaluation_exit": evaluation_exit,
    }
    _write_immutable_json(output_path, value)
    return value


def _raw_sha256_file(path: Path, label: str) -> str:
    return _sha256(_stable_bytes(path, label))


def _validate_final_evaluation_lane_without_exit(
    *,
    manifest_path: str | Path,
    candidate: str,
    suite_id: str,
    lane_root: str | Path,
) -> dict[str, object]:
    manifest_file, manifest = _load_core_manifest(manifest_path)
    if candidate not in {"final", "base_model"}:
        raise FinalEvaluationError("unknown final-evaluation candidate")
    references = manifest["references"]
    suite = references.get(suite_id)
    if not isinstance(suite, Mapping):
        raise FinalEvaluationError("unknown final-evaluation suite")
    raw_root = Path(lane_root).expanduser()
    if raw_root.is_symlink():
        raise FinalEvaluationError("final-evaluation lane root must not be a symlink")
    root = raw_root.resolve()
    journal = root / "predictions.resume.json"
    if journal.is_symlink():
        raise FinalEvaluationError("final-evaluation resume journal must not be a symlink")
    if journal.exists():
        raise FinalEvaluationError("completed final-evaluation lane still has a resume journal")
    predictions_path = root / "predictions.jsonl"
    prediction_manifest_path = root / "predictions.manifest.json"
    evaluation_path = root / "evaluation.json"
    prediction_payload = _stable_bytes(
        predictions_path,
        "final-evaluation predictions",
    )
    prediction_manifest = _json_object(
        _stable_bytes(
            prediction_manifest_path,
            "final-evaluation prediction manifest",
        ),
        "final-evaluation prediction manifest",
    )
    evaluation_payload = _stable_bytes(evaluation_path, "final-evaluation report")
    evaluation_temporary = evaluation_path.with_name(evaluation_path.name + ".tmp")
    if evaluation_temporary.is_symlink():
        raise FinalEvaluationError("final-evaluation report temporary must not be a symlink")
    pending_evaluation = None
    if evaluation_temporary.exists():
        pending_evaluation = _stable_bytes(
            evaluation_temporary,
            "final-evaluation report temporary",
        )
        if pending_evaluation != evaluation_payload:
            raise FinalEvaluationError(f"{candidate}/{suite_id} evaluation temporary differs from its report")
    evaluation = _json_object(
        evaluation_payload,
        "final-evaluation report",
    )
    reference_path = manifest_file.parents[1] / "evaluation-input" / str(suite["records_filename"])
    reference_sha256 = _raw_sha256_file(
        reference_path,
        "final-evaluation references",
    )
    prediction_sha256 = _sha256(prediction_payload)
    if (
        prediction_manifest.get("candidate") != candidate
        or prediction_manifest.get("record_count") != suite["record_count"]
        or prediction_manifest.get("batch_size") != _FINAL_EVALUATION_BATCH_SIZE
        or prediction_manifest.get("reference_sha256") != reference_sha256
        or prediction_manifest.get("prediction_sha256") != prediction_sha256
        or prediction_manifest.get("quantization") != "none"
        or prediction_manifest.get("device") != "cuda"
    ):
        raise FinalEvaluationError(f"{candidate}/{suite_id} prediction manifest is inconsistent")
    _verify_prediction_policy(prediction_manifest, manifest, candidate)
    if prediction_manifest.get("final_evaluation_model_identity") != _prediction_model_identity(
        manifest,
        candidate,
    ):
        raise FinalEvaluationError(f"{candidate}/{suite_id} prediction manifest has no exact model identity")
    gate = evaluation.get("gate")
    gate_passed = gate.get("passed") if isinstance(gate, Mapping) else None
    gate_status = gate.get("status") if isinstance(gate, Mapping) else None
    if (
        evaluation.get("candidate") != candidate
        or evaluation.get("reference_sha256") != reference_sha256
        or evaluation.get("prediction_sha256") != prediction_sha256
        or evaluation.get("evaluation_config_sha256") != manifest["evaluation"]["config_sha256"]
        or evaluation.get("exact_case_coverage") is not True
        or not isinstance(gate, Mapping)
        or not isinstance(gate_passed, bool)
        or gate_status != ("passed" if gate_passed else "failed")
    ):
        raise FinalEvaluationError(f"{candidate}/{suite_id} evaluation report is inconsistent")
    if pending_evaluation is not None:
        evaluation_temporary.unlink()
        _fsync_parent(evaluation_path)
    return {
        "root": root,
        "prediction_manifest_path": prediction_manifest_path,
        "evaluation_path": evaluation_path,
        "record_count": suite["record_count"],
        "prediction_sha256": prediction_sha256,
        "expected_evaluation_exit": 0 if gate_passed else 2,
        "gate_status": gate_status,
        "gate_passed": gate_passed,
    }


def recover_final_evaluation_exit(
    *,
    manifest_path: str | Path,
    candidate: str,
    suite_id: str,
    lane_root: str | Path,
) -> dict[str, object]:
    """Recover only the deterministic 0/2 exit after validating the report."""

    lane = _validate_final_evaluation_lane_without_exit(
        manifest_path=manifest_path,
        candidate=candidate,
        suite_id=suite_id,
        lane_root=lane_root,
    )
    return record_final_evaluation_exit(
        candidate=candidate,
        suite_id=suite_id,
        evaluation_exit=int(lane["expected_evaluation_exit"]),
        output_path=Path(lane["root"]) / "evaluation-exit.json",
    )


def verify_final_evaluation_lane(
    *,
    manifest_path: str | Path,
    candidate: str,
    suite_id: str,
    lane_root: str | Path,
) -> dict[str, object]:
    """Verify one complete candidate/suite prediction and evaluation lane."""

    lane = _validate_final_evaluation_lane_without_exit(
        manifest_path=manifest_path,
        candidate=candidate,
        suite_id=suite_id,
        lane_root=lane_root,
    )
    root = Path(lane["root"])
    exit_record = _json_object(
        _stable_bytes(root / "evaluation-exit.json", "final-evaluation gate exit"),
        "final-evaluation gate exit",
    )
    evaluation_exit = exit_record.get("evaluation_exit")
    if (
        exit_record.get("candidate") != candidate
        or exit_record.get("suite") != suite_id
        or evaluation_exit != lane["expected_evaluation_exit"]
    ):
        raise FinalEvaluationError(f"{candidate}/{suite_id} evaluation exit is inconsistent")
    prediction_manifest_path = Path(lane["prediction_manifest_path"])
    evaluation_path = Path(lane["evaluation_path"])
    return {
        "record_count": lane["record_count"],
        "prediction_sha256": lane["prediction_sha256"],
        "prediction_manifest_sha256": _raw_sha256_file(
            prediction_manifest_path,
            "final-evaluation prediction manifest",
        ),
        "evaluation_sha256": _raw_sha256_file(
            evaluation_path,
            "final-evaluation report",
        ),
        "evaluation_exit": evaluation_exit,
        "gate_status": lane["gate_status"],
        "gate_passed": lane["gate_passed"],
    }


def _validate_result(value: Mapping[str, object]) -> dict[str, object]:
    schema = json.loads(_RESULT_SCHEMA.read_bytes())
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(error.message for error in errors[:5])
        raise FinalEvaluationError(f"final-evaluation result is invalid: {detail}")
    return dict(value)


def verify_final_evaluation_output_provenance(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
) -> dict[str, object]:
    """Bind the durable output to the exact immutable packet and runner."""

    _, core = _load_core_manifest(manifest_path)
    root = Path(output_root).expanduser().resolve()
    payload = _stable_bytes(
        root / "job-manifest.json",
        "durable final-evaluation job manifest",
    )
    job_manifest = _json_object(
        payload,
        "durable final-evaluation job manifest",
    )
    if payload != canonical_json_bytes(job_manifest):
        raise FinalEvaluationError("durable final-evaluation job manifest is not canonical JSON")
    job_manifest = _validate_job_manifest(job_manifest)
    packet_fields = {
        "runner_sha256",
        "packet_tree_sha256",
        "file_count",
        "byte_count",
    }
    durable_core = {key: value for key, value in job_manifest.items() if key not in packet_fields}
    if durable_core != core:
        raise FinalEvaluationError("durable job manifest differs from the final-evaluation core")
    return {
        "packet_tree_sha256": job_manifest["packet_tree_sha256"],
        "runner_sha256": job_manifest["runner_sha256"],
        "source_git_head": job_manifest["source_git_head"],
    }


def finalize_hf_final_evaluation(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Finalize both candidates and every separately bound suite."""

    _, manifest = _load_core_manifest(manifest_path)
    root = _bound_final_evaluation_output_root(output_root)
    destination_candidate = Path(output_path).expanduser()
    if destination_candidate.is_symlink():
        raise FinalEvaluationError("final-evaluation result path must not be a symlink")
    destination = destination_candidate.resolve()
    if destination != root / "final-evaluation-result.json":
        raise FinalEvaluationError("final-evaluation result path differs from the unique output binding")
    provenance = verify_final_evaluation_output_provenance(
        manifest_path=manifest_path,
        output_root=root,
    )
    ledger_path = root / _LAUNCH_LEDGER_FILENAME
    ledger = _load_launch_ledger(
        ledger_path,
        continuation_evidence=manifest["continuation_evidence"],
        budget=manifest["budget"],
        retry_evidence=manifest.get("retry_evidence"),
        evaluation_flavor=_validated_final_evaluation_flavor(manifest.get("flavor")),
    )
    if ledger is None:
        raise FinalEvaluationError("final-evaluation output has no durable launch ledger")
    reports: dict[str, dict[str, object]] = {}
    for candidate in ("final", "base_model"):
        reports[candidate] = {}
        for suite_id in sorted(manifest["references"]):
            reports[candidate][suite_id] = verify_final_evaluation_lane(
                manifest_path=manifest_path,
                candidate=candidate,
                suite_id=suite_id,
                lane_root=root / candidate / suite_id,
            )
    expected_files = {
        "continuation-result.json",
        "final-evaluation-manifest.json",
        _LAUNCH_LEDGER_FILENAME,
        "hf-cost-audit.json",
        "job-manifest.json",
        "preflight-local.json",
        "preflight-remote.json",
        "preflight-base-local.json",
        "preflight-base-remote.json",
        "training-report.json",
    }
    if isinstance(
        manifest["continuation_evidence"].get("recovery_chain"),
        Mapping,
    ):
        expected_files.update(
            {
                "attempt9-finalization-source-contract.json",
                "attempt9-finalization-verification.json",
                "finalization-launch-guard.json",
            }
        )
    else:
        expected_files.update(
            {
                "continuation-manifest.json",
                "continuation-packet-verification.json",
            }
        )
    for candidate in ("final", "base_model"):
        for suite_id in manifest["references"]:
            expected_files.update(
                {
                    f"{candidate}/{suite_id}/evaluation-exit.json",
                    f"{candidate}/{suite_id}/evaluation.json",
                    f"{candidate}/{suite_id}/predictions.jsonl",
                    f"{candidate}/{suite_id}/predictions.manifest.json",
                }
            )
    files: dict[str, dict[str, object]] = {}
    result_temporary = destination.with_name(destination.name + ".tmp")
    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise FinalEvaluationError(f"final-evaluation output contains a symlink: {path}")
        if path.is_file() and path.resolve() not in {destination, result_temporary}:
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise FinalEvaluationError(f"final-evaluation output inventory is not exact (missing={missing}, extra={extra})")
    for relative in sorted(actual_files):
        path = root / relative
        if path.name.endswith(".resume.json") or path.name.endswith(".tmp"):
            raise FinalEvaluationError("final-evaluation output contains an incomplete artifact")
        payload = _stable_bytes(path, "final-evaluation output artifact")
        files[relative] = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    result_payload = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_result",
        "status": "complete",
        "training_steps_executed": 0,
        "packet": provenance,
        "final_model": manifest["final_model"],
        "base_model": manifest["base_model"],
        "continuation_evidence": manifest["continuation_evidence"],
        "budget": manifest["budget"],
        "launch_ledger": ledger,
        "publication": manifest["publication"],
        "reports": reports,
        "files": files,
    }
    if "retry_evidence" in manifest:
        result_payload["retry_evidence"] = manifest["retry_evidence"]
    result = _validate_result(result_payload)
    _write_immutable_json(destination, result)
    return result

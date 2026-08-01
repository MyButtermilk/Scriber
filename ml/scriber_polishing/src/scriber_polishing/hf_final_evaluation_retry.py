"""Fail-closed recovery and one replacement for the final-evaluation amendment."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import canonical_json_bytes
from .hf_final_evaluation import (
    FinalEvaluationError,
    _acquire_final_evaluation_writer,
    _bound_final_evaluation_output_root,
    _fsync_parent,
    _stable_bytes,
    _validate_result,
    _write_new_durable_payload,
    release_final_evaluation_writer,
    verify_final_evaluation_output_provenance,
)
from .hf_final_evaluation_amendment import (
    ACTUAL_BEFORE_EVALUATION_USD,
    AMENDMENT_FLAVOR,
    AMENDMENT_LEDGER_SIBLING,
    AMENDMENT_TIMEOUT_MINUTES,
    OUTPUT_BUCKET,
    OUTPUT_ROOT,
    PRIMARY_MAXIMUM_COST_USD,
    PRIMARY_TIMEOUT_MINUTES,
    REMOTE_RESULT_URI,
    RUNTIME_COMPLETION_SIBLING,
    WRITER_LOCK_SIBLING,
    FinalEvaluationAmendmentError,
    _canonical_file,
    _copy_tree,
    _git_head,
    _inventory_tree,
    _packet_inventory,
    _sha256,
    amendment_runner_payload,
    recover_unbound_prediction_manifests,
    verify_hf_final_evaluation_amendment_packet,
)

RETRY_TIMEOUT_MINUTES = 180
RETRY_FLAVOR = "l4x1"
RETRY_MAXIMUM_COST_USD = "2.400"
FAILED_AMENDMENT_ACTUAL_COST_USD = "0.026667"
FAILED_AMENDMENT_RUNNING_SECONDS = 78
FAILED_AMENDMENT_BILLED_MINUTES = 2
L4_HOURLY_RATE_USD = "0.800"
TOTAL_BUDGET_USD = "5.000"
MAXIMUM_TOTAL_COST_USD = "4.960335"
REMAINING_BUDGET_USD = "0.039665"

PRIMARY_JOB_ID = "6a6baf33b36a6516e96a3210"
FAILED_AMENDMENT_JOB_ID = "6a6bc2edb36a6516e96a3347"
FAILED_AMENDMENT_CREATED_AT_UTC = "2026-07-30T21:32:29.296000Z"
FAILED_AMENDMENT_STARTED_AT_UTC = "2026-07-30T21:32:37.482Z"
FAILED_AMENDMENT_FINISHED_AT_UTC = "2026-07-30T21:33:55.695Z"
FAILED_AMENDMENT_IMAGE = "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime"
HF_NAMESPACE = "Buttermilk03"
USER_RETRY_AUTHORIZATION_STATEMENT = "Egal Fang an zur Not mit mehr Budget"

RETRY_LEDGER_SIBLING = ".scriber-1100-final-evaluation-v1.launch-ledger-v3.json"
RETRY_RUNTIME_COMPLETION_SIBLING = ".scriber-1100-final-evaluation-v1.runtime-completion-v3.json"

_PROJECT_ROOT = Path(__file__).parents[2]
_REPOSITORY_ROOT = _PROJECT_ROOT.parents[1]
_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_retry_job_manifest_schema.json"
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z$")


class FinalEvaluationRetryError(ValueError):
    """The requested recovery/retry is not exactly the authorized replacement."""


@dataclass(frozen=True, slots=True)
class HfFinalEvaluationRetryPacket:
    """An immutable retry packet; constructing it never starts a job."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _require_exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise FinalEvaluationRetryError(
            f"{label} fields differ (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise FinalEvaluationRetryError(f"{label} must be a normalized UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise FinalEvaluationRetryError(f"{label} is not a valid UTC timestamp") from error
    if parsed.tzinfo != UTC:
        raise FinalEvaluationRetryError(f"{label} must use UTC")
    return parsed


def _launch_id(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]


def _retry_budget() -> dict[str, object]:
    actual_before = Decimal(ACTUAL_BEFORE_EVALUATION_USD)
    primary_reservation = Decimal(PRIMARY_MAXIMUM_COST_USD)
    failed_actual = Decimal(FAILED_AMENDMENT_ACTUAL_COST_USD)
    retry_reservation = Decimal(RETRY_MAXIMUM_COST_USD)
    budget = Decimal(TOTAL_BUDGET_USD)
    maximum = actual_before + primary_reservation + failed_actual + retry_reservation
    expected_failed = (Decimal(FAILED_AMENDMENT_BILLED_MINUTES) * Decimal(L4_HOURLY_RATE_USD) / Decimal(60)).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )
    if (
        expected_failed != failed_actual
        or maximum != Decimal(MAXIMUM_TOTAL_COST_USD)
        or budget - maximum != Decimal(REMAINING_BUDGET_USD)
        or maximum >= budget
    ):
        raise FinalEvaluationRetryError("final-evaluation retry budget arithmetic is invalid")
    return {
        "total_budget_usd": TOTAL_BUDGET_USD,
        "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
        "primary_cost_status": "unknown",
        "primary_budget_accounting": "full_timeout_reservation",
        "primary_timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
        "primary_reserved_cost_usd": PRIMARY_MAXIMUM_COST_USD,
        "failed_amendment_cost_status": "known_from_canonical_terminal_duration",
        "failed_amendment_running_seconds": FAILED_AMENDMENT_RUNNING_SECONDS,
        "failed_amendment_billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
        "failed_amendment_actual_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
        "retry_timeout_minutes": RETRY_TIMEOUT_MINUTES,
        "retry_reserved_cost_usd": RETRY_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
        "strictly_below_total_budget": True,
        "budget_increase_used": False,
    }


def build_retry_authorization_evidence(source_statement: str) -> dict[str, object]:
    """Bind the later user instruction to exactly one replacement evaluation."""

    if source_statement != USER_RETRY_AUTHORIZATION_STATEMENT:
        raise FinalEvaluationRetryError("retry authorization must use the exact user replacement instruction")
    return validate_retry_authorization(
        {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_retry_authorization",
            "authorized": True,
            "authorizer": "user",
            "scope": "one_replacement_after_exact_pre_prediction_writer_claim_failure",
            "maximum_replacement_jobs": 1,
            "flavor": RETRY_FLAVOR,
            "timeout_minutes": RETRY_TIMEOUT_MINUTES,
            "maximum_replacement_cost_usd": RETRY_MAXIMUM_COST_USD,
            "maximum_total_cost_usd": TOTAL_BUDGET_USD,
            "budget_increase_used": False,
            "training_steps_allowed": 0,
            "publication_allowed": False,
            "source_statement_sha256": _sha256(source_statement.encode("utf-8")),
        }
    )


def validate_retry_authorization(value: Mapping[str, object]) -> dict[str, object]:
    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_retry_authorization",
        "authorized": True,
        "authorizer": "user",
        "scope": "one_replacement_after_exact_pre_prediction_writer_claim_failure",
        "maximum_replacement_jobs": 1,
        "flavor": RETRY_FLAVOR,
        "timeout_minutes": RETRY_TIMEOUT_MINUTES,
        "maximum_replacement_cost_usd": RETRY_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": TOTAL_BUDGET_USD,
        "budget_increase_used": False,
        "training_steps_allowed": 0,
        "publication_allowed": False,
        "source_statement_sha256": _sha256(USER_RETRY_AUTHORIZATION_STATEMENT.encode("utf-8")),
    }
    _require_exact_keys(value, set(expected), "retry authorization")
    if dict(value) != expected:
        raise FinalEvaluationRetryError("retry authorization is not the exact one-job evaluation-only scope")
    return expected


def build_failed_amendment_terminal_evidence(
    *,
    observed_at_utc: str,
    primary_packet_tree_sha256: str,
    amendment_packet_tree_sha256: str,
) -> dict[str, object]:
    """Build sanitized canonical evidence from the direct Jobs REST response."""

    evidence = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_failed_amendment_terminal_evidence",
        "source": "hugging_face_jobs_rest_api",
        "endpoint_path": "/api/jobs/{namespace}/{job_id}",
        "namespace": HF_NAMESPACE,
        "observed_at_utc": observed_at_utc,
        "job_id": FAILED_AMENDMENT_JOB_ID,
        "created_at_utc": FAILED_AMENDMENT_CREATED_AT_UTC,
        "started_at_utc": FAILED_AMENDMENT_STARTED_AT_UTC,
        "finished_at_utc": FAILED_AMENDMENT_FINISHED_AT_UTC,
        "terminal_status": "ERROR",
        "failure_stage": "writer_claim",
        "failure_reason": "active_writer",
        "flavor": AMENDMENT_FLAVOR,
        "timeout_seconds": AMENDMENT_TIMEOUT_MINUTES * 60,
        "image": FAILED_AMENDMENT_IMAGE,
        "command_packet_tree_sha256": {
            "primary": primary_packet_tree_sha256,
            "amendment": amendment_packet_tree_sha256,
        },
        "durations": {
            "scheduling_seconds": 8,
            "running_seconds": FAILED_AMENDMENT_RUNNING_SECONDS,
            "total_seconds": 86,
        },
        "billing": {
            "hourly_rate_usd": L4_HOURLY_RATE_USD,
            "billing_unit_seconds": 60,
            "billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
            "actual_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
            "rounding": "ceil_to_6_decimal_places",
        },
        "actual_cost_status": "known_from_canonical_terminal_duration",
        "model_load_started": False,
        "prediction_records_written": 0,
        "output_mutation_performed": False,
        "training_steps_executed": 0,
    }
    return validate_failed_amendment_terminal_evidence(
        evidence,
        expected_primary_packet_tree_sha256=primary_packet_tree_sha256,
        expected_amendment_packet_tree_sha256=amendment_packet_tree_sha256,
    )


def validate_failed_amendment_terminal_evidence(
    value: Mapping[str, object],
    *,
    expected_primary_packet_tree_sha256: str,
    expected_amendment_packet_tree_sha256: str,
) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "kind",
        "source",
        "endpoint_path",
        "namespace",
        "observed_at_utc",
        "job_id",
        "created_at_utc",
        "started_at_utc",
        "finished_at_utc",
        "terminal_status",
        "failure_stage",
        "failure_reason",
        "flavor",
        "timeout_seconds",
        "image",
        "command_packet_tree_sha256",
        "durations",
        "billing",
        "actual_cost_status",
        "model_load_started",
        "prediction_records_written",
        "output_mutation_performed",
        "training_steps_executed",
    }
    _require_exact_keys(value, expected_keys, "failed amendment terminal evidence")
    observed = _parse_utc(value.get("observed_at_utc"), "failed amendment observation")
    created = _parse_utc(value.get("created_at_utc"), "failed amendment creation")
    started = _parse_utc(value.get("started_at_utc"), "failed amendment start")
    finished = _parse_utc(value.get("finished_at_utc"), "failed amendment finish")
    packet_hashes = value.get("command_packet_tree_sha256")
    durations = value.get("durations")
    billing = value.get("billing")
    expected_packet_hashes = {
        "primary": expected_primary_packet_tree_sha256,
        "amendment": expected_amendment_packet_tree_sha256,
    }
    expected_durations = {
        "scheduling_seconds": 8,
        "running_seconds": FAILED_AMENDMENT_RUNNING_SECONDS,
        "total_seconds": 86,
    }
    expected_billing = {
        "hourly_rate_usd": L4_HOURLY_RATE_USD,
        "billing_unit_seconds": 60,
        "billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
        "actual_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
        "rounding": "ceil_to_6_decimal_places",
    }
    expected_scalar = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_failed_amendment_terminal_evidence",
        "source": "hugging_face_jobs_rest_api",
        "endpoint_path": "/api/jobs/{namespace}/{job_id}",
        "namespace": HF_NAMESPACE,
        "job_id": FAILED_AMENDMENT_JOB_ID,
        "created_at_utc": FAILED_AMENDMENT_CREATED_AT_UTC,
        "started_at_utc": FAILED_AMENDMENT_STARTED_AT_UTC,
        "finished_at_utc": FAILED_AMENDMENT_FINISHED_AT_UTC,
        "terminal_status": "ERROR",
        "failure_stage": "writer_claim",
        "failure_reason": "active_writer",
        "flavor": AMENDMENT_FLAVOR,
        "timeout_seconds": AMENDMENT_TIMEOUT_MINUTES * 60,
        "image": FAILED_AMENDMENT_IMAGE,
        "actual_cost_status": "known_from_canonical_terminal_duration",
        "model_load_started": False,
        "prediction_records_written": 0,
        "output_mutation_performed": False,
        "training_steps_executed": 0,
    }
    if (
        observed < finished
        or not created < started < finished
        or any(value.get(key) != expected for key, expected in expected_scalar.items())
        or packet_hashes != expected_packet_hashes
        or durations != expected_durations
        or billing != expected_billing
    ):
        raise FinalEvaluationRetryError("failed amendment evidence differs from the canonical pre-prediction ERROR")
    return dict(value)


def build_no_active_jobs_evidence(*, observed_at_utc: str) -> dict[str, object]:
    """Bind the sanitized namespace-wide empty Jobs listing."""

    return validate_no_active_jobs_evidence(
        {
            "schema_version": 1,
            "kind": "scriber_hf_no_active_jobs_observation",
            "source": "hf_jobs_ps",
            "namespace": HF_NAMESPACE,
            "observed_at_utc": observed_at_utc,
            "active_job_ids": [],
            "active_job_count": 0,
        }
    )


def validate_no_active_jobs_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_no_active_jobs_observation",
        "source": "hf_jobs_ps",
        "namespace": HF_NAMESPACE,
        "active_job_ids": [],
        "active_job_count": 0,
    }
    _require_exact_keys(
        value,
        {*expected, "observed_at_utc"},
        "no-active-jobs evidence",
    )
    _parse_utc(value.get("observed_at_utc"), "no-active-jobs observation")
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise FinalEvaluationRetryError("no-active-jobs evidence does not prove an empty namespace listing")
    return dict(value)


def _read_exact_writer_owner(
    path: str | Path,
    *,
    expected_sha256: str,
) -> tuple[bytes, dict[str, object]]:
    if _HASH.fullmatch(expected_sha256) is None:
        raise FinalEvaluationRetryError("expected writer-owner hash is invalid")
    payload, owner = _canonical_file(path, "stale writer owner")
    _require_exact_keys(
        owner,
        {"schema_version", "kind", "launch_id", "owner_token"},
        "stale writer owner",
    )
    token = owner.get("owner_token")
    if (
        _sha256(payload) != expected_sha256
        or owner.get("schema_version") != 1
        or owner.get("kind") != "scriber_hf_final_evaluation_writer_owner"
        or owner.get("launch_id") != _launch_id(PRIMARY_JOB_ID)
        or not isinstance(token, str)
        or _OWNER_TOKEN.fullmatch(token) is None
        or len(payload) != 167
    ):
        raise FinalEvaluationRetryError("writer owner is not the exact stale primary-job lock")
    return payload, owner


def build_stale_writer_lock_observation(
    *,
    owner_path: str | Path,
    expected_owner_sha256: str,
    observed_at_utc: str,
    primary_terminal_evidence: Mapping[str, object],
    failed_amendment_evidence: Mapping[str, object],
    no_active_jobs_evidence: Mapping[str, object],
    output_snapshot_tree_sha256: str,
) -> dict[str, object]:
    """Sanitize the private owner file while retaining a byte-exact binding."""

    owner_payload, owner = _read_exact_writer_owner(
        owner_path,
        expected_sha256=expected_owner_sha256,
    )
    if (
        primary_terminal_evidence.get("job_id") != PRIMARY_JOB_ID
        or primary_terminal_evidence.get("terminal_status") != "CANCELED"
        or primary_terminal_evidence.get("terminal_duration_available") is not False
        or primary_terminal_evidence.get("budget_accounting") != "full_timeout_reservation"
    ):
        raise FinalEvaluationRetryError("stale lock observation requires the exact CANCELED primary evidence")
    if failed_amendment_evidence.get("job_id") != FAILED_AMENDMENT_JOB_ID:
        raise FinalEvaluationRetryError("stale lock observation names a different failed amendment")
    validate_no_active_jobs_evidence(no_active_jobs_evidence)
    if _HASH.fullmatch(output_snapshot_tree_sha256) is None:
        raise FinalEvaluationRetryError("stale lock output snapshot hash is invalid")
    observation = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_stale_writer_lock_observation",
        "observed_at_utc": observed_at_utc,
        "remote_lock_relative_path": ("attempt8/" + WRITER_LOCK_SIBLING + "/owner.json"),
        "owner_file_bytes": len(owner_payload),
        "owner_file_sha256": _sha256(owner_payload),
        "owner": {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_writer_owner",
            "launch_id": owner["launch_id"],
            "owner_token_present": True,
            "owner_token_length": 32,
            "owner_token_redacted": True,
        },
        "owner_job_id": PRIMARY_JOB_ID,
        "owner_job_terminal_status": "CANCELED",
        "primary_terminal_evidence_sha256": _sha256(canonical_json_bytes(primary_terminal_evidence)),
        "failed_amendment_evidence_sha256": _sha256(canonical_json_bytes(failed_amendment_evidence)),
        "no_active_jobs_evidence_sha256": _sha256(canonical_json_bytes(no_active_jobs_evidence)),
        "output_snapshot_tree_sha256": output_snapshot_tree_sha256,
        "v2_ledger_expected_absent": True,
        "v2_runtime_completion_expected_absent": True,
    }
    return validate_stale_writer_lock_observation(
        observation,
        primary_terminal_evidence=primary_terminal_evidence,
        failed_amendment_evidence=failed_amendment_evidence,
        no_active_jobs_evidence=no_active_jobs_evidence,
        output_snapshot_tree_sha256=output_snapshot_tree_sha256,
    )


def validate_stale_writer_lock_observation(
    value: Mapping[str, object],
    *,
    primary_terminal_evidence: Mapping[str, object],
    failed_amendment_evidence: Mapping[str, object],
    no_active_jobs_evidence: Mapping[str, object],
    output_snapshot_tree_sha256: str,
) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "kind",
        "observed_at_utc",
        "remote_lock_relative_path",
        "owner_file_bytes",
        "owner_file_sha256",
        "owner",
        "owner_job_id",
        "owner_job_terminal_status",
        "primary_terminal_evidence_sha256",
        "failed_amendment_evidence_sha256",
        "no_active_jobs_evidence_sha256",
        "output_snapshot_tree_sha256",
        "v2_ledger_expected_absent",
        "v2_runtime_completion_expected_absent",
    }
    _require_exact_keys(value, expected_keys, "stale writer lock observation")
    _parse_utc(value.get("observed_at_utc"), "stale writer lock observation")
    owner = value.get("owner")
    expected_owner = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_writer_owner",
        "launch_id": _launch_id(PRIMARY_JOB_ID),
        "owner_token_present": True,
        "owner_token_length": 32,
        "owner_token_redacted": True,
    }
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "scriber_hf_final_evaluation_stale_writer_lock_observation"
        or value.get("remote_lock_relative_path") != "attempt8/" + WRITER_LOCK_SIBLING + "/owner.json"
        or value.get("owner_file_bytes") != 167
        or not isinstance(value.get("owner_file_sha256"), str)
        or _HASH.fullmatch(str(value.get("owner_file_sha256"))) is None
        or owner != expected_owner
        or value.get("owner_job_id") != PRIMARY_JOB_ID
        or value.get("owner_job_terminal_status") != "CANCELED"
        or value.get("primary_terminal_evidence_sha256") != _sha256(canonical_json_bytes(primary_terminal_evidence))
        or value.get("failed_amendment_evidence_sha256") != _sha256(canonical_json_bytes(failed_amendment_evidence))
        or value.get("no_active_jobs_evidence_sha256") != _sha256(canonical_json_bytes(no_active_jobs_evidence))
        or value.get("output_snapshot_tree_sha256") != output_snapshot_tree_sha256
        or value.get("v2_ledger_expected_absent") is not True
        or value.get("v2_runtime_completion_expected_absent") is not True
    ):
        raise FinalEvaluationRetryError("stale writer lock observation differs from the exact recoverable lock")
    return dict(value)


def retry_runner_payload(primary_runner: bytes) -> bytes:
    """Derive V3 from the reviewed V2 runner transformation."""

    try:
        script = amendment_runner_payload(primary_runner).decode("utf-8")
    except (UnicodeDecodeError, FinalEvaluationAmendmentError) as error:
        raise FinalEvaluationRetryError(str(error)) from error

    def replace_once(old: str, new: str, label: str) -> None:
        nonlocal script
        if script.count(old) != 1:
            raise FinalEvaluationRetryError(f"V2 runner anchor changed: {label}")
        script = script.replace(old, new)

    replace_once(
        'AMENDMENT_ROOT=/input/amendment\nAMENDMENT_PROJECT="$AMENDMENT_ROOT/repo/ml/scriber_polishing"\n',
        (
            "AMENDMENT_ROOT=/input/amendment\n"
            'AMENDMENT_PROJECT="$AMENDMENT_ROOT/repo/ml/scriber_polishing"\n'
            "RETRY_ROOT=/input/retry\n"
            'RETRY_PROJECT="$RETRY_ROOT/repo/ml/scriber_polishing"\n'
        ),
        "retry mount",
    )
    replace_once(
        'EXPECTED_AMENDMENT_TREE_SHA256="${2:-}"\nRUN_MODE=gpu-evaluation\n',
        ('EXPECTED_AMENDMENT_TREE_SHA256="${2:-}"\nEXPECTED_RETRY_TREE_SHA256="${3:-}"\nRUN_MODE=gpu-evaluation\n'),
        "retry argument",
    )
    replace_once(
        'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || '
        '[ -z "$EXPECTED_AMENDMENT_TREE_SHA256" ]; then\n'
        '  echo "expected primary and amendment packet tree SHA-256 arguments are required" >&2\n',
        (
            'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_AMENDMENT_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_RETRY_TREE_SHA256" ]; then\n'
            '  echo "expected primary, amendment, and retry packet tree SHA-256 arguments are required" >&2\n'
        ),
        "retry argument validation",
    )
    replace_once(
        'if [ ! -d "$SOURCE_REPO" ] || [ ! -d "$AMENDMENT_PROJECT" ]; then\n'
        '  echo "immutable primary or amendment packet mount is missing" >&2\n',
        (
            'if [ ! -d "$SOURCE_REPO" ] || [ ! -d "$AMENDMENT_PROJECT" ] || '
            '[ ! -d "$RETRY_PROJECT" ]; then\n'
            '  echo "immutable primary, amendment, or retry packet mount is missing" >&2\n'
        ),
        "retry mount validation",
    )
    verification_anchor = (
        'export PYTHONPATH="$AMENDMENT_PROJECT/src"\n'
        'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
        "  --mode packet \\\n"
    )
    replace_once(
        verification_anchor,
        (
            'export PYTHONPATH="$RETRY_PROJECT/src"\n'
            'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
            "  --mode packet \\\n"
            '  --packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256"\n' + verification_anchor
        ),
        "retry packet verification",
    )
    replace_once(
        '  PYTHONPATH="$AMENDMENT_PROJECT/src" '
        'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
        "    --mode release-writer \\\n",
        (
            '  PYTHONPATH="$RETRY_PROJECT/src" '
            'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
            "    --mode release-writer \\\n"
        ),
        "retry writer release",
    )
    replace_once(
        'PYTHONPATH="$AMENDMENT_PROJECT/src" '
        'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
        "  --mode claim-launch \\\n"
        '  --packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n',
        (
            'PYTHONPATH="$RETRY_PROJECT/src" '
            'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
            "  --mode claim-launch \\\n"
            '  --packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n'
        ),
        "retry writer claim",
    )
    replace_once(
        'cp "$AMENDMENT_PROJECT/src/scriber_polishing/inference.py" '
        '"$PROJECT_ROOT/src/scriber_polishing/inference.py"\n',
        ('cp "$RETRY_PROJECT/src/scriber_polishing/inference.py" "$PROJECT_ROOT/src/scriber_polishing/inference.py"\n'),
        "retry inference overlay",
    )
    replace_once(
        'PYTHONPATH="$AMENDMENT_PROJECT/src" '
        'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
        "  --mode recover-unbound \\\n"
        '  --packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n',
        (
            'PYTHONPATH="$RETRY_PROJECT/src" '
            'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
            "  --mode recover-unbound \\\n"
            '  --packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n'
        ),
        "retry unbound recovery",
    )
    replace_once(
        'PYTHONPATH="$AMENDMENT_PROJECT/src" '
        'python "$AMENDMENT_PROJECT/scripts/verify_hf_final_evaluation_amendment.py" \\\n'
        "  --mode runtime-complete \\\n"
        '  --packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_AMENDMENT_TREE_SHA256" \\\n',
        (
            'PYTHONPATH="$RETRY_PROJECT/src" '
            'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
            "  --mode runtime-complete \\\n"
            '  --packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n'
        ),
        "retry runtime completion",
    )
    replace_once(
        'echo "cloud_final_evaluation=complete training_steps_executed=0 amendment_v2=complete" >&2\n',
        ('echo "cloud_final_evaluation=complete training_steps_executed=0 retry_v3=complete" >&2\n'),
        "retry completion marker",
    )
    return script.replace("\r\n", "\n").encode("utf-8")


def _validate_manifest_schema(value: Mapping[str, object]) -> dict[str, object]:
    if not _SCHEMA.is_file():
        raise FinalEvaluationRetryError(f"retry manifest schema is missing: {_SCHEMA}")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise FinalEvaluationRetryError(f"retry manifest schema failed at {location}: {errors[0].message}")
    return dict(value)


def _snapshot_identity(snapshot: Mapping[str, object]) -> dict[str, object]:
    return {
        key: snapshot[key]
        for key in (
            "directories",
            "files",
            "file_count",
            "byte_count",
            "tree_sha256",
        )
    }


def _assert_no_v2_or_v3_siblings(root: Path) -> None:
    siblings = (
        AMENDMENT_LEDGER_SIBLING,
        RUNTIME_COMPLETION_SIBLING,
        RETRY_LEDGER_SIBLING,
        RETRY_RUNTIME_COMPLETION_SIBLING,
    )
    present = [name for name in siblings if (root.parent / name).exists()]
    if present:
        raise FinalEvaluationRetryError("retry output already has forbidden sibling evidence: " + ", ".join(present))


def build_hf_final_evaluation_retry_packet(
    *,
    primary_packet_root: str | Path,
    amendment_packet_root: str | Path,
    expected_primary_packet_tree_sha256: str,
    expected_amendment_packet_tree_sha256: str,
    output_snapshot_root: str | Path,
    failed_amendment_evidence_path: str | Path,
    no_active_jobs_evidence_path: str | Path,
    stale_writer_lock_observation_path: str | Path,
    stale_owner_path: str | Path,
    authorization_path: str | Path,
    output_dir: str | Path,
) -> HfFinalEvaluationRetryPacket:
    """Build V3 without mutating the output or launching a remote job."""

    primary = Path(primary_packet_root).expanduser().resolve()
    amendment = Path(amendment_packet_root).expanduser().resolve()
    try:
        amendment_manifest = verify_hf_final_evaluation_amendment_packet(
            amendment,
            primary_packet_root=primary,
            expected_packet_tree_sha256=expected_amendment_packet_tree_sha256,
        )
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationRetryError(str(error)) from error
    if amendment_manifest["primary_packet"]["packet_tree_sha256"] != expected_primary_packet_tree_sha256:
        raise FinalEvaluationRetryError("V2 amendment binds a different primary packet")
    primary_terminal = amendment_manifest["terminal_predecessor"]
    if (
        primary_terminal.get("job_id") != PRIMARY_JOB_ID
        or primary_terminal.get("terminal_status") != "CANCELED"
        or primary_terminal.get("terminal_duration_available") is not False
    ):
        raise FinalEvaluationRetryError("V2 packet does not bind the exact CANCELED primary job")
    failed_payload, failed_raw = _canonical_file(
        failed_amendment_evidence_path,
        "failed amendment terminal evidence",
    )
    failed = validate_failed_amendment_terminal_evidence(
        failed_raw,
        expected_primary_packet_tree_sha256=expected_primary_packet_tree_sha256,
        expected_amendment_packet_tree_sha256=expected_amendment_packet_tree_sha256,
    )
    no_active_payload, no_active_raw = _canonical_file(
        no_active_jobs_evidence_path,
        "no-active-jobs evidence",
    )
    no_active = validate_no_active_jobs_evidence(no_active_raw)
    authorization_payload, authorization_raw = _canonical_file(
        authorization_path,
        "retry authorization",
    )
    authorization = validate_retry_authorization(authorization_raw)
    observation_payload, observation_raw = _canonical_file(
        stale_writer_lock_observation_path,
        "stale writer lock observation",
    )
    snapshot = amendment_manifest["output"]["snapshot"]
    observation = validate_stale_writer_lock_observation(
        observation_raw,
        primary_terminal_evidence=primary_terminal,
        failed_amendment_evidence=failed,
        no_active_jobs_evidence=no_active,
        output_snapshot_tree_sha256=str(snapshot["tree_sha256"]),
    )
    owner_payload, _owner = _read_exact_writer_owner(
        stale_owner_path,
        expected_sha256=str(observation["owner_file_sha256"]),
    )
    root = _bound_final_evaluation_output_root(output_snapshot_root)
    current_inventory = _inventory_tree(root)
    if current_inventory != _snapshot_identity(snapshot):
        raise FinalEvaluationRetryError("post-failure output snapshot differs from the V2 launch-bound snapshot")
    _assert_no_v2_or_v3_siblings(root)
    if (root / "final-evaluation-result.json").exists():
        raise FinalEvaluationRetryError("retry output already has a final result")
    inference_path = _PROJECT_ROOT / "src/scriber_polishing/inference.py"
    inference_hash = _sha256(_stable_bytes(inference_path, "retry inference module"))
    if inference_hash != amendment_manifest["durability_patch"]["inference_module_sha256"]:
        raise FinalEvaluationRetryError("retry inference differs from the reviewed batch-durable V2 patch")
    target = Path(output_dir).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise FinalEvaluationRetryError(f"refusing to overwrite retry packet: {target}")
    primary_runner = _stable_bytes(
        primary / "run-final-evaluation.sh",
        "primary final-evaluation runner",
    )
    runner = retry_runner_payload(primary_runner)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        project = temporary / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        _copy_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_tree(_PROJECT_ROOT / "contracts", project / "contracts")
        (project / "scripts").mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "scripts/verify_hf_final_evaluation_retry.py",
            project / "scripts/verify_hf_final_evaluation_retry.py",
        )
        job_input = project / "job-input"
        job_input.mkdir()
        (job_input / "failed-amendment-terminal.json").write_bytes(failed_payload)
        (job_input / "no-active-jobs.json").write_bytes(no_active_payload)
        (job_input / "stale-writer-lock.json").write_bytes(observation_payload)
        (job_input / "authorization.json").write_bytes(authorization_payload)
        (temporary / "run-final-evaluation-retry.sh").write_bytes(runner)
        v2_job_manifest = _stable_bytes(
            amendment / "job-manifest.json",
            "V2 amendment job manifest",
        )
        v2_runner = _stable_bytes(
            amendment / "run-final-evaluation-amendment.sh",
            "V2 amendment runner",
        )
        core = {
            "schema_version": 3,
            "kind": "scriber_hf_final_evaluation_retry",
            "source_git_head": _git_head(),
            "image": amendment_manifest["image"],
            "flavor": RETRY_FLAVOR,
            "timeout_minutes": RETRY_TIMEOUT_MINUTES,
            "batch_size": 8,
            "max_new_tokens": 512,
            "primary_packet": dict(amendment_manifest["primary_packet"]),
            "amendment_packet": {
                "packet_tree_sha256": expected_amendment_packet_tree_sha256,
                "job_manifest_sha256": _sha256(v2_job_manifest),
                "runner_sha256": _sha256(v2_runner),
            },
            "terminal_primary": dict(primary_terminal),
            "terminal_primary_sha256": str(amendment_manifest["terminal_predecessor_sha256"]),
            "failed_amendment": failed,
            "failed_amendment_sha256": _sha256(failed_payload),
            "no_active_jobs": no_active,
            "no_active_jobs_sha256": _sha256(no_active_payload),
            "stale_writer_lock": observation,
            "stale_writer_lock_sha256": _sha256(observation_payload),
            "authorization": authorization,
            "authorization_sha256": _sha256(authorization_payload),
            "output": {
                "bucket_root": OUTPUT_BUCKET,
                "relative_path": "attempt8/scriber-1100-final-evaluation-v1",
                "container_path": OUTPUT_ROOT,
                "remote_result_uri": REMOTE_RESULT_URI,
                "primary_launch_ledger_v1_sha256": amendment_manifest["output"]["primary_launch_ledger_v1_sha256"],
                "v2_ledger_sibling": AMENDMENT_LEDGER_SIBLING,
                "v2_runtime_completion_sibling": RUNTIME_COMPLETION_SIBLING,
                "v3_ledger_sibling": RETRY_LEDGER_SIBLING,
                "v3_runtime_completion_sibling": RETRY_RUNTIME_COMPLETION_SIBLING,
                "snapshot": snapshot,
            },
            "budget": _retry_budget(),
            "durability_patch": dict(amendment_manifest["durability_patch"]),
            "writer_recovery": {
                "method": "exact_owner_release_then_atomic_new_claim",
                "owner_file_sha256": _sha256(owner_payload),
                "owner_launch_id": _launch_id(PRIMARY_JOB_ID),
                "owner_token_packaged": False,
                "snapshot_required_equal_before_and_after": True,
                "recursive_delete_allowed": False,
                "new_claim_after_recovery_only": True,
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
        (job_input / "retry-manifest.json").write_bytes(canonical_json_bytes(core))
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
        owner_token = str(_owner["owner_token"]).encode("ascii")
        for path in temporary.rglob("*"):
            if path.is_file() and owner_token in path.read_bytes():
                raise FinalEvaluationRetryError("private writer owner token leaked into the retry packet")
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HfFinalEvaluationRetryPacket(
        output_dir=target,
        manifest=MappingProxyType(manifest),
    )


def _load_retry_manifest(packet_root: Path) -> tuple[bytes, dict[str, object]]:
    payload, manifest = _canonical_file(
        packet_root / "job-manifest.json",
        "retry job manifest",
    )
    return payload, _validate_manifest_schema(manifest)


def verify_hf_final_evaluation_retry_packet(
    packet_root: str | Path,
    *,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    """Verify V3, V2, and the original primary packet without remote mutation."""

    packet = Path(packet_root).expanduser().resolve()
    if packet.is_symlink() or not packet.is_dir():
        raise FinalEvaluationRetryError("retry packet is missing or symlinked")
    _, manifest = _load_retry_manifest(packet)
    inventory, tree = _packet_inventory(packet)
    if (
        tree != expected_packet_tree_sha256
        or manifest["packet_tree_sha256"] != tree
        or manifest["file_count"] != len(inventory)
        or manifest["byte_count"] != sum(int(item["bytes"]) for item in inventory)
    ):
        raise FinalEvaluationRetryError("retry packet differs from its launch-bound inventory")
    amendment = Path(amendment_packet_root).expanduser().resolve()
    primary = Path(primary_packet_root).expanduser().resolve()
    try:
        amendment_manifest = verify_hf_final_evaluation_amendment_packet(
            amendment,
            primary_packet_root=primary,
            expected_packet_tree_sha256=str(manifest["amendment_packet"]["packet_tree_sha256"]),
        )
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationRetryError(str(error)) from error
    if manifest["primary_packet"] != amendment_manifest["primary_packet"]:
        raise FinalEvaluationRetryError("retry primary-packet binding changed")
    v2_binding = {
        "packet_tree_sha256": amendment_manifest["packet_tree_sha256"],
        "job_manifest_sha256": _sha256(_stable_bytes(amendment / "job-manifest.json", "V2 job manifest")),
        "runner_sha256": _sha256(
            _stable_bytes(
                amendment / "run-final-evaluation-amendment.sh",
                "V2 runner",
            )
        ),
    }
    if manifest["amendment_packet"] != v2_binding:
        raise FinalEvaluationRetryError("V2 amendment packet byte binding changed")
    primary_runner = _stable_bytes(
        primary / "run-final-evaluation.sh",
        "primary runner",
    )
    runner = _stable_bytes(
        packet / "run-final-evaluation-retry.sh",
        "retry runner",
    )
    if manifest["runner_sha256"] != _sha256(runner) or runner != retry_runner_payload(primary_runner):
        raise FinalEvaluationRetryError("retry runner differs from the reviewed transformation")
    project = packet / "repo/ml/scriber_polishing"
    failed_payload, failed_raw = _canonical_file(
        project / "job-input/failed-amendment-terminal.json",
        "failed amendment terminal evidence",
    )
    failed = validate_failed_amendment_terminal_evidence(
        failed_raw,
        expected_primary_packet_tree_sha256=str(manifest["primary_packet"]["packet_tree_sha256"]),
        expected_amendment_packet_tree_sha256=str(manifest["amendment_packet"]["packet_tree_sha256"]),
    )
    no_active_payload, no_active_raw = _canonical_file(
        project / "job-input/no-active-jobs.json",
        "no-active-jobs evidence",
    )
    no_active = validate_no_active_jobs_evidence(no_active_raw)
    authorization_payload, authorization_raw = _canonical_file(
        project / "job-input/authorization.json",
        "retry authorization",
    )
    authorization = validate_retry_authorization(authorization_raw)
    observation_payload, observation_raw = _canonical_file(
        project / "job-input/stale-writer-lock.json",
        "stale writer lock observation",
    )
    observation = validate_stale_writer_lock_observation(
        observation_raw,
        primary_terminal_evidence=amendment_manifest["terminal_predecessor"],
        failed_amendment_evidence=failed,
        no_active_jobs_evidence=no_active,
        output_snapshot_tree_sha256=str(amendment_manifest["output"]["snapshot"]["tree_sha256"]),
    )
    evidence_checks = (
        (
            manifest["failed_amendment"],
            manifest["failed_amendment_sha256"],
            failed,
            _sha256(failed_payload),
            "failed amendment",
        ),
        (
            manifest["no_active_jobs"],
            manifest["no_active_jobs_sha256"],
            no_active,
            _sha256(no_active_payload),
            "no-active-jobs",
        ),
        (
            manifest["authorization"],
            manifest["authorization_sha256"],
            authorization,
            _sha256(authorization_payload),
            "authorization",
        ),
        (
            manifest["stale_writer_lock"],
            manifest["stale_writer_lock_sha256"],
            observation,
            _sha256(observation_payload),
            "stale writer lock",
        ),
    )
    for bound, bound_hash, actual, actual_hash, label in evidence_checks:
        if bound != actual or bound_hash != actual_hash:
            raise FinalEvaluationRetryError(f"{label} evidence differs from the retry binding")
    if (
        manifest["terminal_primary"] != amendment_manifest["terminal_predecessor"]
        or manifest["terminal_primary_sha256"] != amendment_manifest["terminal_predecessor_sha256"]
        or manifest["output"]["snapshot"] != amendment_manifest["output"]["snapshot"]
        or manifest["budget"] != _retry_budget()
        or manifest["durability_patch"] != amendment_manifest["durability_patch"]
    ):
        raise FinalEvaluationRetryError("retry manifest differs from the exact V2 predecessor state")
    inference_payload = _stable_bytes(
        project / "src/scriber_polishing/inference.py",
        "retry inference module",
    )
    if manifest["durability_patch"]["inference_module_sha256"] != _sha256(inference_payload):
        raise FinalEvaluationRetryError("retry batch-durable inference module changed")
    return manifest


def build_hf_final_evaluation_retry_launch_contract(
    packet_root: str | Path,
    *,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
) -> dict[str, object]:
    """Build, but never execute, the single replacement L4 launch."""

    packet = Path(packet_root).expanduser().resolve()
    amendment = Path(amendment_packet_root).expanduser().resolve()
    primary = Path(primary_packet_root).expanduser().resolve()
    _, manifest = _load_retry_manifest(packet)
    verified = verify_hf_final_evaluation_retry_packet(
        packet,
        amendment_packet_root=amendment,
        primary_packet_root=primary,
        expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
    )
    primary_manifest = json.loads((primary / "job-manifest.json").read_text(encoding="utf-8"))
    retry_volume = f"{packet}:/input/retry:ro"
    amendment_volume = f"{amendment}:/input/amendment:ro"
    primary_volume = f"{primary}:/input/primary:ro"
    model_volume = f"{primary_manifest['final_model']['remote_model_uri']}:/final-model:ro"
    base_volume = f"{primary_manifest['base_model']['remote_model_uri']}:/base-model:ro"
    output_volume = f"{OUTPUT_BUCKET}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        RETRY_FLAVOR,
        "--timeout",
        f"{RETRY_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=final-evaluation-1100-retry-v3",
        "-v",
        retry_volume,
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
        "/input/retry/run-final-evaluation-retry.sh",
        str(verified["primary_packet"]["packet_tree_sha256"]),
        str(verified["amendment_packet"]["packet_tree_sha256"]),
        str(verified["packet_tree_sha256"]),
    ]
    return {
        "schema_version": 3,
        "kind": "scriber_hf_final_evaluation_retry_launch_contract",
        "flavor": RETRY_FLAVOR,
        "timeout": f"{RETRY_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": RETRY_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "remaining_budget_usd": REMAINING_BUDGET_USD,
        "strictly_below_total_budget": True,
        "budget_increase_used": False,
        "primary_packet_tree_sha256": verified["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": verified["amendment_packet"]["packet_tree_sha256"],
        "retry_packet_tree_sha256": verified["packet_tree_sha256"],
        "primary_job_id": PRIMARY_JOB_ID,
        "failed_amendment_job_id": FAILED_AMENDMENT_JOB_ID,
        "failed_amendment_actual_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
        "authorization_sha256": verified["authorization_sha256"],
        "stale_writer_owner_file_sha256": verified["stale_writer_lock"]["owner_file_sha256"],
        "partial_output_tree_sha256": verified["output"]["snapshot"]["tree_sha256"],
        "durable_prediction_records": verified["output"]["snapshot"]["durable_prediction_records"],
        "batch_size": 8,
        "max_new_tokens": 512,
        "training_steps_executed": 0,
        "publication_allowed": False,
        "external_job_started": False,
        "retry_volume": retry_volume,
        "amendment_volume": amendment_volume,
        "primary_packet_volume": primary_volume,
        "model_volume": model_volume,
        "base_model_volume": base_volume,
        "output_volume": output_volume,
        "output_root": OUTPUT_ROOT,
        "remote_result_uri": REMOTE_RESULT_URI,
        "argv": argv,
    }


def _current_inventory_matches(
    root: Path,
    expected: Mapping[str, object],
) -> dict[str, object]:
    actual = _inventory_tree(root)
    if actual != _snapshot_identity(expected):
        raise FinalEvaluationRetryError("remote partial output differs from the launch-bound snapshot")
    return actual


def _recover_stale_writer_for_retry(
    *,
    root: Path,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Remove only the byte-bound primary owner through the exact-owner release."""

    lock = root.parent / WRITER_LOCK_SIBLING
    owner_path = lock / "owner.json"
    if lock.is_symlink() or not lock.is_dir():
        raise FinalEvaluationRetryError("the exact stale writer-lock directory is absent or symlinked")
    entries = list(lock.iterdir())
    if len(entries) != 1 or entries[0].name != "owner.json" or entries[0].is_symlink() or not entries[0].is_file():
        raise FinalEvaluationRetryError("stale writer-lock directory contains unexpected entries")
    observation = manifest["stale_writer_lock"]
    owner_payload, owner = _read_exact_writer_owner(
        owner_path,
        expected_sha256=str(observation["owner_file_sha256"]),
    )
    before = _current_inventory_matches(root, manifest["output"]["snapshot"])
    token = str(owner["owner_token"])
    try:
        release = release_final_evaluation_writer(
            output_root=root,
            launch_id=str(owner["launch_id"]),
            owner_token=token,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationRetryError(str(error)) from error
    finally:
        token = ""
        owner["owner_token"] = "<redacted>"
    if release.get("released") is not True or lock.exists():
        raise FinalEvaluationRetryError("exact stale writer release did not remove the lock")
    after = _current_inventory_matches(root, manifest["output"]["snapshot"])
    if before != after:
        raise FinalEvaluationRetryError("partial output changed while recovering the stale writer lock")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_stale_writer_recovery",
        "released": True,
        "owner_file_sha256": _sha256(owner_payload),
        "owner_file_bytes": len(owner_payload),
        "owner_launch_id": _launch_id(PRIMARY_JOB_ID),
        "owner_job_id": PRIMARY_JOB_ID,
        "owner_job_terminal_status": "CANCELED",
        "failed_amendment_job_id": FAILED_AMENDMENT_JOB_ID,
        "failed_amendment_terminal_status": "ERROR",
        "no_active_jobs_evidence_sha256": manifest["no_active_jobs_sha256"],
        "snapshot_before_tree_sha256": before["tree_sha256"],
        "snapshot_after_tree_sha256": after["tree_sha256"],
        "writer_lock_absent_after": True,
        "recursive_delete_performed": False,
        "owner_token_persisted": False,
    }


def _validate_v3_ledger(
    value: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    current_job_id: str | None = None,
    current_launch_id: str | None = None,
) -> dict[str, object]:
    launches = value.get("evaluation_launches")
    if not isinstance(launches, list) or len(launches) != 3:
        raise FinalEvaluationRetryError("retry V3 ledger must bind exactly three heterogeneous launches")
    retry_job_id = launches[2].get("job_id") if isinstance(launches[2], Mapping) else None
    retry_launch_id = launches[2].get("launch_id") if isinstance(launches[2], Mapping) else None
    if current_job_id is not None and retry_job_id != current_job_id:
        raise FinalEvaluationRetryError("V3 ledger names a different retry job")
    if current_launch_id is not None and retry_launch_id != current_launch_id:
        raise FinalEvaluationRetryError("V3 ledger names a different retry launch")
    expected = {
        "schema_version": 3,
        "kind": "scriber_hf_final_evaluation_launch_ledger",
        "total_budget_usd": TOTAL_BUDGET_USD,
        "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "authorization_sha256": manifest["authorization_sha256"],
        "terminal_primary_sha256": manifest["terminal_primary_sha256"],
        "failed_amendment_sha256": manifest["failed_amendment_sha256"],
        "stale_writer_lock_sha256": manifest["stale_writer_lock_sha256"],
        "partial_output_tree_sha256": manifest["output"]["snapshot"]["tree_sha256"],
        "evaluation_launches": [
            {
                "sequence": 1,
                "role": "primary",
                "job_id": PRIMARY_JOB_ID,
                "launch_id": _launch_id(PRIMARY_JOB_ID),
                "terminal_status": "CANCELED",
                "cost_status": "unknown",
                "budget_accounting": "full_timeout_reservation",
                "flavor": RETRY_FLAVOR,
                "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
                "accounted_cost_usd": PRIMARY_MAXIMUM_COST_USD,
                "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
            },
            {
                "sequence": 2,
                "role": "failed_amendment",
                "job_id": FAILED_AMENDMENT_JOB_ID,
                "launch_id": _launch_id(FAILED_AMENDMENT_JOB_ID),
                "terminal_status": "ERROR",
                "failure_stage": "writer_claim",
                "prediction_records_written": 0,
                "output_mutation_performed": False,
                "cost_status": "known_from_canonical_terminal_duration",
                "running_seconds": FAILED_AMENDMENT_RUNNING_SECONDS,
                "billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
                "accounted_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
                "packet_tree_sha256": manifest["amendment_packet"]["packet_tree_sha256"],
            },
            {
                "sequence": 3,
                "role": "replacement_retry",
                "job_id": retry_job_id,
                "launch_id": retry_launch_id,
                "claim_status": "running",
                "flavor": RETRY_FLAVOR,
                "timeout_minutes": RETRY_TIMEOUT_MINUTES,
                "accounted_cost_usd": RETRY_MAXIMUM_COST_USD,
                "packet_tree_sha256": manifest["packet_tree_sha256"],
            },
        ],
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
        "strictly_below_total_budget": True,
        "budget_increase_used": False,
        "writer_recovery": value.get("writer_recovery"),
    }
    recovery = value.get("writer_recovery")
    if (
        not isinstance(retry_job_id, str)
        or _JOB_ID.fullmatch(retry_job_id) is None
        or retry_job_id in {PRIMARY_JOB_ID, FAILED_AMENDMENT_JOB_ID}
        or not isinstance(retry_launch_id, str)
        or retry_launch_id != _launch_id(retry_job_id)
        or not isinstance(recovery, Mapping)
        or recovery.get("kind") != "scriber_hf_final_evaluation_stale_writer_recovery"
        or recovery.get("released") is not True
        or recovery.get("owner_token_persisted") is not False
        or dict(value) != expected
    ):
        raise FinalEvaluationRetryError("retry V3 ledger differs from the exact recovery/budget contract")
    return expected


def claim_final_evaluation_retry(
    *,
    packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    job_id: str,
    launch_id: str,
    owner_token: str,
) -> dict[str, object]:
    """Recover the stale primary lock, then atomically claim the V3 writer."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or job_id in {PRIMARY_JOB_ID, FAILED_AMENDMENT_JOB_ID}
        or _LAUNCH_ID.fullmatch(launch_id) is None
        or _OWNER_TOKEN.fullmatch(owner_token) is None
        or launch_id != _launch_id(job_id)
    ):
        raise FinalEvaluationRetryError("retry runtime job/launch/owner identity is invalid")
    manifest = verify_hf_final_evaluation_retry_packet(
        packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    _assert_no_v2_or_v3_siblings(root)
    _current_inventory_matches(root, manifest["output"]["snapshot"])
    recovery = _recover_stale_writer_for_retry(root=root, manifest=manifest)
    acquired = False
    try:
        _current_inventory_matches(root, manifest["output"]["snapshot"])
        _acquire_final_evaluation_writer(
            root,
            launch_id=launch_id,
            owner_token=owner_token,
        )
        acquired = True
        _current_inventory_matches(root, manifest["output"]["snapshot"])
        _assert_no_v2_or_v3_siblings(root)
        ledger = _validate_v3_ledger(
            {
                "schema_version": 3,
                "kind": "scriber_hf_final_evaluation_launch_ledger",
                "total_budget_usd": TOTAL_BUDGET_USD,
                "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
                "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
                "authorization_sha256": manifest["authorization_sha256"],
                "terminal_primary_sha256": manifest["terminal_primary_sha256"],
                "failed_amendment_sha256": manifest["failed_amendment_sha256"],
                "stale_writer_lock_sha256": manifest["stale_writer_lock_sha256"],
                "partial_output_tree_sha256": manifest["output"]["snapshot"]["tree_sha256"],
                "evaluation_launches": [
                    {
                        "sequence": 1,
                        "role": "primary",
                        "job_id": PRIMARY_JOB_ID,
                        "launch_id": _launch_id(PRIMARY_JOB_ID),
                        "terminal_status": "CANCELED",
                        "cost_status": "unknown",
                        "budget_accounting": "full_timeout_reservation",
                        "flavor": RETRY_FLAVOR,
                        "timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
                        "accounted_cost_usd": PRIMARY_MAXIMUM_COST_USD,
                        "packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
                    },
                    {
                        "sequence": 2,
                        "role": "failed_amendment",
                        "job_id": FAILED_AMENDMENT_JOB_ID,
                        "launch_id": _launch_id(FAILED_AMENDMENT_JOB_ID),
                        "terminal_status": "ERROR",
                        "failure_stage": "writer_claim",
                        "prediction_records_written": 0,
                        "output_mutation_performed": False,
                        "cost_status": "known_from_canonical_terminal_duration",
                        "running_seconds": FAILED_AMENDMENT_RUNNING_SECONDS,
                        "billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
                        "accounted_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
                        "packet_tree_sha256": manifest["amendment_packet"]["packet_tree_sha256"],
                    },
                    {
                        "sequence": 3,
                        "role": "replacement_retry",
                        "job_id": job_id,
                        "launch_id": launch_id,
                        "claim_status": "running",
                        "flavor": RETRY_FLAVOR,
                        "timeout_minutes": RETRY_TIMEOUT_MINUTES,
                        "accounted_cost_usd": RETRY_MAXIMUM_COST_USD,
                        "packet_tree_sha256": manifest["packet_tree_sha256"],
                    },
                ],
                "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
                "remaining_after_reservations_usd": REMAINING_BUDGET_USD,
                "strictly_below_total_budget": True,
                "budget_increase_used": False,
                "writer_recovery": recovery,
            },
            manifest=manifest,
            current_job_id=job_id,
            current_launch_id=launch_id,
        )
        ledger_path = root.parent / RETRY_LEDGER_SIBLING
        _write_new_durable_payload(ledger_path, canonical_json_bytes(ledger))
        _fsync_parent(ledger_path)
    except Exception:
        if acquired:
            with suppress(FinalEvaluationError):
                release_final_evaluation_writer(
                    output_root=root,
                    launch_id=launch_id,
                    owner_token=owner_token,
                )
        raise
    return {
        "schema_version": 3,
        "kind": "scriber_hf_final_evaluation_retry_launch_claim",
        "job_id": job_id,
        "launch_id": launch_id,
        "stale_writer_recovered": True,
        "writer_acquired": True,
        "ledger_path": RETRY_LEDGER_SIBLING,
        "maximum_job_cost_usd": RETRY_MAXIMUM_COST_USD,
        "maximum_total_cost_usd": MAXIMUM_TOTAL_COST_USD,
        "remaining_budget_usd": REMAINING_BUDGET_USD,
    }


def recover_unbound_prediction_manifests_for_retry(
    *,
    packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    final_model_root: str | Path,
    base_model_root: str | Path,
) -> list[dict[str, str]]:
    """Use the byte-bound V2 repair after verifying the V3 packet."""

    manifest = verify_hf_final_evaluation_retry_packet(
        packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    return recover_unbound_prediction_manifests(
        packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=str(manifest["amendment_packet"]["packet_tree_sha256"]),
        output_root=output_root,
        final_model_root=final_model_root,
        base_model_root=base_model_root,
    )


def record_retry_runtime_completion(
    *,
    packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    job_id: str,
    launch_id: str,
) -> dict[str, object]:
    """Write a V3 runtime envelope; terminal evidence remains external."""

    manifest = verify_hf_final_evaluation_retry_packet(
        packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    ledger_payload, ledger_raw = _canonical_file(
        root.parent / RETRY_LEDGER_SIBLING,
        "retry V3 ledger",
    )
    ledger = _validate_v3_ledger(
        ledger_raw,
        manifest=manifest,
        current_job_id=job_id,
        current_launch_id=launch_id,
    )
    result_payload, result = _canonical_file(
        root / "final-evaluation-result.json",
        "final-evaluation result",
    )
    try:
        result = _validate_result(result)
        primary_core = (
            Path(primary_packet_root).expanduser().resolve()
            / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
        )
        provenance = verify_final_evaluation_output_provenance(
            manifest_path=primary_core,
            output_root=root,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationRetryError(str(error)) from error
    if (
        result.get("status") != "complete"
        or result.get("packet") != provenance
        or provenance.get("packet_tree_sha256") != manifest["primary_packet"]["packet_tree_sha256"]
    ):
        raise FinalEvaluationRetryError("primary result does not complete the bound retry evaluation")
    completion = {
        "schema_version": 3,
        "kind": "scriber_hf_final_evaluation_retry_runtime_completion",
        "status": "runtime_complete_pending_terminal_evidence",
        "job_id": job_id,
        "launch_id": launch_id,
        "primary_packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": manifest["amendment_packet"]["packet_tree_sha256"],
        "retry_packet_tree_sha256": manifest["packet_tree_sha256"],
        "primary_result_sha256": _sha256(result_payload),
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "retry_launch_ledger_v3_sha256": _sha256(ledger_payload),
        "maximum_total_cost_usd": ledger["maximum_total_cost_usd"],
        "remaining_budget_usd": ledger["remaining_after_reservations_usd"],
        "training_steps_executed": 0,
        "publication_performed": False,
    }
    destination = root.parent / RETRY_RUNTIME_COMPLETION_SIBLING
    _write_new_durable_payload(destination, canonical_json_bytes(completion))
    _fsync_parent(destination)
    return completion

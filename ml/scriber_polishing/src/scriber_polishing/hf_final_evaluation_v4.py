"""Fail-closed V4 recovery for one stale Hugging Face Bucket resume journal.

The V3 worker completed ``final/ai_gold`` but the Bucket retained the last
durable resume-journal prefix after the worker unlinked it.  This module keeps
that recovery separate from normal lane verification:

* source artifacts are inspected read-only;
* a deletion plan names exactly one byte-bound Bucket object;
* deletion is performed externally through the Bucket API, never through FUSE;
* the complete lane must pass the unchanged verifier after the copied journal
  is removed; and
* later V4 inference journals live outside the immutable result tree.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import canonical_json_bytes
from .evaluation_io import load_candidate_predictions, load_evaluation_references
from .hf_final_evaluation import (
    FinalEvaluationError,
    _acquire_final_evaluation_writer,
    _bound_final_evaluation_output_root,
    _fsync_parent,
    _load_core_manifest,
    _stable_bytes,
    _validate_result,
    _write_new_durable_payload,
    release_final_evaluation_writer,
    verify_final_evaluation_lane,
    verify_final_evaluation_output_provenance,
)
from .hf_final_evaluation_amendment import (
    ACTUAL_BEFORE_EVALUATION_USD,
    AMENDMENT_LEDGER_SIBLING,
    OUTPUT_ROOT,
    PRIMARY_MAXIMUM_COST_USD,
    PRIMARY_TIMEOUT_MINUTES,
    RUNTIME_COMPLETION_SIBLING,
    FinalEvaluationAmendmentError,
    _canonical_file,
    _copy_tree,
    _git_head,
    _inventory_tree,
    _packet_inventory,
    _read_journal,
    _sha256,
)
from .hf_final_evaluation_retry import (
    FAILED_AMENDMENT_ACTUAL_COST_USD,
    FAILED_AMENDMENT_BILLED_MINUTES,
    FAILED_AMENDMENT_JOB_ID,
    PRIMARY_JOB_ID,
    RETRY_LEDGER_SIBLING,
    RETRY_RUNTIME_COMPLETION_SIBLING,
    USER_RETRY_AUTHORIZATION_STATEMENT,
    WRITER_LOCK_SIBLING,
    FinalEvaluationRetryError,
    _launch_id,
    _validate_v3_ledger,
    retry_runner_payload,
    validate_no_active_jobs_evidence,
    verify_hf_final_evaluation_retry_packet,
)
from .inference import _canonical_json, validate_batch_resume_journal

V3_JOB_ID = "6a6bcb2d23ed89c748ec880e"
V3_RUNNING_SECONDS = 1301
V3_SCHEDULING_SECONDS = 165
V3_TOTAL_SECONDS = 1467
V3_BILLED_MINUTES = 22
V3_ACTUAL_COST_USD = "0.293333"
V3_STARTED_AT_UTC = "2026-07-30T22:10:27.506Z"
V3_FINISHED_AT_UTC = "2026-07-30T22:32:08.685Z"
V3_IMAGE = "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime"
V3_PACKET_TREE_SHA256 = "sha256:0f2bbab45433c95a15d52e7c5bb26a17ea8ee74879e48c09bc00884df575e719"

V4_TIMEOUT_MINUTES = 180
V4_FLAVOR = "l4x1"
V4_MAXIMUM_COST_USD = "2.400"
OVERALL_TOTAL_BUDGET_USD = "8.000"
EVALUATION_MAXIMUM_AFTER_V4_USD = "5.253668"
DOWNSTREAM_TOOLCHAIN_RESERVATION_USD = "0.053333"
DOWNSTREAM_QUANTIZATION_RESERVATION_USD = "2.400"
OVERALL_MAXIMUM_USD = "7.707001"
OVERALL_REMAINING_USD = "0.292999"
V4_AUTHORIZATION_STATEMENT = USER_RETRY_AUTHORIZATION_STATEMENT
V4_LEDGER_SIBLING = ".scriber-1100-final-evaluation-v1.launch-ledger-v4.json"
V4_RUNTIME_COMPLETION_SIBLING = ".scriber-1100-final-evaluation-v1.runtime-completion-v4.json"
V3_LEDGER_BYTES = 2949
V3_LEDGER_SHA256 = "sha256:c2cc76efcbb267e05ea1e9759fcaca628a30421148c780b33a9405748d197834"
STALE_JOURNAL_EVIDENCE_SHA256 = "sha256:8e4d0b99a3669bb4844ec7c9be399358c1bddb85eb88f0306cdadf3334172869"
EXACT_REMOVAL_PLAN_SHA256 = "sha256:69540145b604ef7ff2acd6e3a6bbf7f4708ae29e098b4d508020828a17cbabeb"
POST_REMOVAL_PROOF_SHA256 = "sha256:072c2303bb5c96be19b1dd52d3eb3dd89c5cee920e2e57a61cad1901ac4c8861"
V3_TERMINAL_EVIDENCE_SHA256 = "sha256:bad98dcc50b0ba1ac60a6f2147ba5d2ddc326e5894231c6495e075b256b02f07"
NO_ACTIVE_JOBS_EVIDENCE_SHA256 = "sha256:22088b676e02b662d6d972da25215ebcd7fe35b9c1aead6d434fa172f27e86eb"
V4_AUTHORIZATION_SHA256 = "sha256:5fc3392461b6a115354ada933a73f15732b85040cd17b202ef60d9eee6e3c2a3"

FINAL_AI_GOLD_CANDIDATE = "final"
FINAL_AI_GOLD_SUITE = "ai_gold"
FINAL_AI_GOLD_RECORD_COUNT = 1750
FINAL_AI_GOLD_STALE_PREFIX_COUNT = 1744
FINAL_AI_GOLD_TAIL_COUNT = 6
FINAL_EVALUATION_BATCH_SIZE = 8
FINAL_AI_GOLD_JOURNAL_BYTES = 2_009_893
FINAL_AI_GOLD_JOURNAL_SHA256 = "sha256:5cc8590e73cb75bba880b264a968214fcde1c1c39f2a3e9b38412dc7ea8ac41c"
FINAL_AI_GOLD_PREDICTION_SHA256 = "sha256:3ba01833d08afa4c310b84b1989eec47b8be2404770d91217a974e633f324a63"
FINAL_AI_GOLD_MANIFEST_SHA256 = "sha256:db3fc09e41c5963fc5fdfab8192722cf5261afd232177a0ef8d9cbdc21976d9d"
FINAL_AI_GOLD_EVALUATION_SHA256 = "sha256:47ff23aa14e8dc71d8fee721d86379c2c1ed94983de29348587f552cdf2d95d4"
FINAL_AI_GOLD_EXIT_SHA256 = "sha256:137cfbb7e0836aa626a63e34151fb060519bb1b94335642e07a12350f47371e3"

OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
REMOTE_RESULT_URI = OUTPUT_BUCKET + "/attempt8/scriber-1100-final-evaluation-v1"
STALE_JOURNAL_RELATIVE_PATH = "final/ai_gold/predictions.resume.json"
STALE_JOURNAL_REMOTE_URI = REMOTE_RESULT_URI + "/" + STALE_JOURNAL_RELATIVE_PATH
EXTERNAL_JOURNAL_ROOT = "/outputs/attempt8/.scriber-1100-final-evaluation-v1.resume-journals-v4"
EXTERNAL_JOURNAL_SIBLING = ".scriber-1100-final-evaluation-v1.resume-journals-v4"
_ALLOWED_EXTERNAL_JOURNALS = (
    "final/critical_regression/predictions.resume.json",
    "final/regular_test/predictions.resume.json",
    "base_model/ai_gold/predictions.resume.json",
    "base_model/critical_regression/predictions.resume.json",
    "base_model/regular_test/predictions.resume.json",
)

_LANE_FILES_WITH_STALE_JOURNAL = frozenset(
    {
        "predictions.resume.json",
        "predictions.jsonl",
        "predictions.manifest.json",
        "evaluation.json",
        "evaluation-exit.json",
    }
)
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")
_OWNER_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROJECT_ROOT = Path(__file__).parents[2]
_SCHEMA = _PROJECT_ROOT / "contracts/hf_final_evaluation_v4_job_manifest_schema.json"


class FinalEvaluationV4Error(ValueError):
    """The observed V3 output is not safe for the narrowly scoped V4 recovery."""


@dataclass(frozen=True, slots=True)
class HfFinalEvaluationV4Packet:
    """An immutable V4 packet; construction never starts a remote job."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise FinalEvaluationV4Error(f"{label} must be a normalized UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise FinalEvaluationV4Error(f"{label} is not a valid UTC timestamp") from error
    if parsed.tzinfo != UTC:
        raise FinalEvaluationV4Error(f"{label} must use UTC")
    return parsed


def _v4_budget() -> dict[str, object]:
    evaluation_maximum = (
        Decimal(ACTUAL_BEFORE_EVALUATION_USD)
        + Decimal(PRIMARY_MAXIMUM_COST_USD)
        + Decimal(FAILED_AMENDMENT_ACTUAL_COST_USD)
        + Decimal(V3_ACTUAL_COST_USD)
        + Decimal(V4_MAXIMUM_COST_USD)
    )
    overall_maximum = (
        evaluation_maximum
        + Decimal(DOWNSTREAM_TOOLCHAIN_RESERVATION_USD)
        + Decimal(DOWNSTREAM_QUANTIZATION_RESERVATION_USD)
    )
    total = Decimal(OVERALL_TOTAL_BUDGET_USD)
    if (
        evaluation_maximum != Decimal(EVALUATION_MAXIMUM_AFTER_V4_USD)
        or overall_maximum != Decimal(OVERALL_MAXIMUM_USD)
        or total - overall_maximum != Decimal(OVERALL_REMAINING_USD)
        or overall_maximum >= total
    ):
        raise FinalEvaluationV4Error("V4 overall budget arithmetic is invalid")
    return {
        "overall_total_budget_usd": OVERALL_TOTAL_BUDGET_USD,
        "actual_before_evaluation_usd": ACTUAL_BEFORE_EVALUATION_USD,
        "primary_cost_status": "unknown",
        "primary_budget_accounting": "full_timeout_reservation",
        "primary_timeout_minutes": PRIMARY_TIMEOUT_MINUTES,
        "primary_reserved_cost_usd": PRIMARY_MAXIMUM_COST_USD,
        "failed_amendment_actual_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
        "failed_amendment_billed_minutes": FAILED_AMENDMENT_BILLED_MINUTES,
        "v3_actual_cost_usd": V3_ACTUAL_COST_USD,
        "v3_billed_minutes": V3_BILLED_MINUTES,
        "v4_timeout_minutes": V4_TIMEOUT_MINUTES,
        "v4_reserved_cost_usd": V4_MAXIMUM_COST_USD,
        "evaluation_maximum_after_v4_usd": EVALUATION_MAXIMUM_AFTER_V4_USD,
        "downstream_toolchain_reservation_usd": DOWNSTREAM_TOOLCHAIN_RESERVATION_USD,
        "downstream_quantization_reservation_usd": DOWNSTREAM_QUANTIZATION_RESERVATION_USD,
        "overall_maximum_usd": OVERALL_MAXIMUM_USD,
        "overall_remaining_usd": OVERALL_REMAINING_USD,
        "strictly_below_overall_budget": True,
        "budget_increase_used": True,
    }


def build_v3_terminal_evidence(*, observed_at_utc: str) -> dict[str, object]:
    """Bind the exact V3 terminal status and provider-reported cost."""

    return validate_v3_terminal_evidence(
        {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_v3_terminal_evidence",
            "source": "hugging_face_jobs_rest_api",
            "endpoint_path": "/api/jobs/{namespace}/{job_id}",
            "namespace": "Buttermilk03",
            "observed_at_utc": observed_at_utc,
            "job_id": V3_JOB_ID,
            "started_at_utc": V3_STARTED_AT_UTC,
            "finished_at_utc": V3_FINISHED_AT_UTC,
            "terminal_status": "ERROR",
            "failure_stage": "lane_verification",
            "failure_reason": "stale_resume_journal_after_completed_lane",
            "flavor": V4_FLAVOR,
            "timeout_seconds": 10_800,
            "image": V3_IMAGE,
            "packet_tree_sha256": V3_PACKET_TREE_SHA256,
            "durations": {
                "scheduling_seconds": V3_SCHEDULING_SECONDS,
                "running_seconds": V3_RUNNING_SECONDS,
                "total_seconds": V3_TOTAL_SECONDS,
            },
            "billing": {
                "hourly_rate_usd": "0.800",
                "billing_unit_seconds": 60,
                "billed_minutes": V3_BILLED_MINUTES,
                "actual_cost_usd": V3_ACTUAL_COST_USD,
                "source": "provider_terminal_record",
            },
            "completed_lane": {
                "candidate": FINAL_AI_GOLD_CANDIDATE,
                "suite": FINAL_AI_GOLD_SUITE,
                "record_count": FINAL_AI_GOLD_RECORD_COUNT,
                "prediction_sha256": FINAL_AI_GOLD_PREDICTION_SHA256,
                "prediction_manifest_sha256": FINAL_AI_GOLD_MANIFEST_SHA256,
                "evaluation_sha256": FINAL_AI_GOLD_EVALUATION_SHA256,
                "evaluation_exit_sha256": FINAL_AI_GOLD_EXIT_SHA256,
                "evaluation_exit": 2,
            },
            "training_steps_executed": 0,
            "publication_performed": False,
        }
    )


def validate_v3_terminal_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    observed = _parse_utc(value.get("observed_at_utc"), "V3 terminal observation")
    finished = _parse_utc(V3_FINISHED_AT_UTC, "V3 finish")
    exact = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v3_terminal_evidence",
        "source": "hugging_face_jobs_rest_api",
        "endpoint_path": "/api/jobs/{namespace}/{job_id}",
        "namespace": "Buttermilk03",
        "observed_at_utc": value.get("observed_at_utc"),
        "job_id": V3_JOB_ID,
        "started_at_utc": V3_STARTED_AT_UTC,
        "finished_at_utc": V3_FINISHED_AT_UTC,
        "terminal_status": "ERROR",
        "failure_stage": "lane_verification",
        "failure_reason": "stale_resume_journal_after_completed_lane",
        "flavor": V4_FLAVOR,
        "timeout_seconds": 10_800,
        "image": V3_IMAGE,
        "packet_tree_sha256": V3_PACKET_TREE_SHA256,
        "durations": {
            "scheduling_seconds": V3_SCHEDULING_SECONDS,
            "running_seconds": V3_RUNNING_SECONDS,
            "total_seconds": V3_TOTAL_SECONDS,
        },
        "billing": {
            "hourly_rate_usd": "0.800",
            "billing_unit_seconds": 60,
            "billed_minutes": V3_BILLED_MINUTES,
            "actual_cost_usd": V3_ACTUAL_COST_USD,
            "source": "provider_terminal_record",
        },
        "completed_lane": {
            "candidate": FINAL_AI_GOLD_CANDIDATE,
            "suite": FINAL_AI_GOLD_SUITE,
            "record_count": FINAL_AI_GOLD_RECORD_COUNT,
            "prediction_sha256": FINAL_AI_GOLD_PREDICTION_SHA256,
            "prediction_manifest_sha256": FINAL_AI_GOLD_MANIFEST_SHA256,
            "evaluation_sha256": FINAL_AI_GOLD_EVALUATION_SHA256,
            "evaluation_exit_sha256": FINAL_AI_GOLD_EXIT_SHA256,
            "evaluation_exit": 2,
        },
        "training_steps_executed": 0,
        "publication_performed": False,
    }
    if observed < finished or dict(value) != exact:
        raise FinalEvaluationV4Error("V3 terminal evidence differs from the exact terminal/cost")
    return exact


def build_v4_authorization_evidence(source_statement: str) -> dict[str, object]:
    """Bind the user's budget override to this one cleanup replacement."""

    if source_statement != V4_AUTHORIZATION_STATEMENT:
        raise FinalEvaluationV4Error("V4 authorization must use the exact user instruction")
    return validate_v4_authorization(
        {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_v4_authorization",
            "authorized": True,
            "authorizer": "user",
            "scope": "one_replacement_after_exact_post_evaluation_bucket_journal_cleanup",
            "maximum_replacement_jobs": 1,
            "flavor": V4_FLAVOR,
            "timeout_minutes": V4_TIMEOUT_MINUTES,
            "maximum_replacement_cost_usd": V4_MAXIMUM_COST_USD,
            "maximum_overall_cost_usd": OVERALL_TOTAL_BUDGET_USD,
            "budget_increase_used": True,
            "training_steps_allowed": 0,
            "final_ai_gold_inference_allowed": False,
            "final_ai_gold_evaluation_allowed": False,
            "publication_allowed": False,
            "source_statement_sha256": _sha256(source_statement.encode("utf-8")),
        }
    )


def validate_v4_authorization(value: Mapping[str, object]) -> dict[str, object]:
    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_authorization",
        "authorized": True,
        "authorizer": "user",
        "scope": "one_replacement_after_exact_post_evaluation_bucket_journal_cleanup",
        "maximum_replacement_jobs": 1,
        "flavor": V4_FLAVOR,
        "timeout_minutes": V4_TIMEOUT_MINUTES,
        "maximum_replacement_cost_usd": V4_MAXIMUM_COST_USD,
        "maximum_overall_cost_usd": OVERALL_TOTAL_BUDGET_USD,
        "budget_increase_used": True,
        "training_steps_allowed": 0,
        "final_ai_gold_inference_allowed": False,
        "final_ai_gold_evaluation_allowed": False,
        "publication_allowed": False,
        "source_statement_sha256": _sha256(V4_AUTHORIZATION_STATEMENT.encode("utf-8")),
    }
    if dict(value) != expected:
        raise FinalEvaluationV4Error("V4 authorization differs from the exact user scope")
    return expected


def v4_runner_payload(primary_runner: bytes) -> bytes:
    """Derive V4 from V3 while preserving final/ai_gold read-only."""

    try:
        script = retry_runner_payload(primary_runner).decode("utf-8")
    except (UnicodeDecodeError, FinalEvaluationRetryError) as error:
        raise FinalEvaluationV4Error(str(error)) from error

    def replace_once(old: str, new: str, label: str) -> None:
        nonlocal script
        if script.count(old) != 1:
            raise FinalEvaluationV4Error(f"V3 runner anchor changed: {label}")
        script = script.replace(old, new)

    replace_once(
        'RETRY_ROOT=/input/retry\nRETRY_PROJECT="$RETRY_ROOT/repo/ml/scriber_polishing"\n',
        (
            "RETRY_ROOT=/input/retry\n"
            'RETRY_PROJECT="$RETRY_ROOT/repo/ml/scriber_polishing"\n'
            "V4_ROOT=/input/v4\n"
            'V4_PROJECT="$V4_ROOT/repo/ml/scriber_polishing"\n'
        ),
        "V4 mount",
    )
    replace_once(
        "OUTPUT_ROOT=/outputs/attempt8/scriber-1100-final-evaluation-v1\n",
        (
            "OUTPUT_ROOT=/outputs/attempt8/scriber-1100-final-evaluation-v1\n"
            f"EXTERNAL_JOURNAL_ROOT={EXTERNAL_JOURNAL_ROOT}\n"
        ),
        "external journal root",
    )
    replace_once(
        'EXPECTED_RETRY_TREE_SHA256="${3:-}"\nRUN_MODE=gpu-evaluation\n',
        ('EXPECTED_RETRY_TREE_SHA256="${3:-}"\nEXPECTED_V4_TREE_SHA256="${4:-}"\nRUN_MODE=gpu-evaluation\n'),
        "V4 argument",
    )
    replace_once(
        'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || '
        '[ -z "$EXPECTED_AMENDMENT_TREE_SHA256" ] || '
        '[ -z "$EXPECTED_RETRY_TREE_SHA256" ]; then\n'
        '  echo "expected primary, amendment, and retry packet tree SHA-256 arguments are required" >&2\n',
        (
            'if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_AMENDMENT_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_RETRY_TREE_SHA256" ] || '
            '[ -z "$EXPECTED_V4_TREE_SHA256" ]; then\n'
            '  echo "expected primary, amendment, retry, and V4 packet tree SHA-256 arguments are required" >&2\n'
        ),
        "V4 argument validation",
    )
    replace_once(
        'if [ ! -d "$SOURCE_REPO" ] || [ ! -d "$AMENDMENT_PROJECT" ] || '
        '[ ! -d "$RETRY_PROJECT" ]; then\n'
        '  echo "immutable primary, amendment, or retry packet mount is missing" >&2\n',
        (
            'if [ ! -d "$SOURCE_REPO" ] || [ ! -d "$AMENDMENT_PROJECT" ] || '
            '[ ! -d "$RETRY_PROJECT" ] || [ ! -d "$V4_PROJECT" ]; then\n'
            '  echo "immutable primary, amendment, retry, or V4 packet mount is missing" >&2\n'
        ),
        "V4 mount validation",
    )
    verification_anchor = (
        'export PYTHONPATH="$RETRY_PROJECT/src"\n'
        'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
        "  --mode packet \\\n"
    )
    replace_once(
        verification_anchor,
        (
            'export PYTHONPATH="$V4_PROJECT/src"\n'
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "  --mode packet \\\n"
            '  --packet-root "$V4_ROOT" \\\n'
            '  --retry-packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_V4_TREE_SHA256"\n' + verification_anchor
        ),
        "V4 packet verification",
    )
    replace_once(
        '  PYTHONPATH="$RETRY_PROJECT/src" '
        'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
        "    --mode release-writer \\\n",
        (
            '  PYTHONPATH="$V4_PROJECT/src" '
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "    --mode release-writer \\\n"
        ),
        "V4 writer release",
    )
    replace_once(
        'PYTHONPATH="$RETRY_PROJECT/src" '
        'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
        "  --mode claim-launch \\\n"
        '  --packet-root "$RETRY_ROOT" \\\n'
        '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n',
        (
            'PYTHONPATH="$V4_PROJECT/src" '
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "  --mode claim-launch \\\n"
            '  --packet-root "$V4_ROOT" \\\n'
            '  --retry-packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_V4_TREE_SHA256" \\\n'
        ),
        "V4 writer claim",
    )
    replace_once(
        '  --owner-token "$OWNER_TOKEN"\n\nmkdir -p "$WORKSPACE_ROOT"\n',
        (
            '  --owner-token "$OWNER_TOKEN"\n\n'
            'if [ -e "$EXTERNAL_JOURNAL_ROOT" ]; then\n'
            '  echo "external V4 journal root appeared after the atomic claim" >&2\n'
            "  exit 1\n"
            "fi\n"
            'mkdir -p "$EXTERNAL_JOURNAL_ROOT"\n'
            'sync "$EXTERNAL_JOURNAL_ROOT"\n'
            'mkdir -p "$WORKSPACE_ROOT"\n'
        ),
        "external journal root creation",
    )
    replace_once(
        'cp "$RETRY_PROJECT/src/scriber_polishing/inference.py" "$PROJECT_ROOT/src/scriber_polishing/inference.py"\n',
        ('cp "$V4_PROJECT/src/scriber_polishing/inference.py" "$PROJECT_ROOT/src/scriber_polishing/inference.py"\n'),
        "V4 inference overlay",
    )
    recover_block = (
        'PYTHONPATH="$RETRY_PROJECT/src" '
        'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
        "  --mode recover-unbound \\\n"
        '  --packet-root "$RETRY_ROOT" \\\n'
        '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n'
        '  --output-root "$OUTPUT_ROOT" \\\n'
        '  --final-model "$LOCAL_FINAL_MODEL" \\\n'
        '  --base-model "$LOCAL_BASE_MODEL"\n'
    )
    replace_once(
        recover_block,
        (
            'PYTHONPATH="$V4_PROJECT/src" '
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "  --mode preserved-lane \\\n"
            '  --packet-root "$V4_ROOT" \\\n'
            '  --retry-packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_V4_TREE_SHA256" \\\n'
            '  --output-root "$OUTPUT_ROOT"\n'
        ),
        "remove V3 unbound recovery",
    )
    replace_once(
        'run_final_suite() {\n  local suite_id="$1"\n  local references="$2"\n',
        (
            "run_final_suite() {\n"
            '  local suite_id="$1"\n'
            '  local references="$2"\n'
            '  if [ "$suite_id" = ai_gold ]; then\n'
            '    echo "V4 forbids final/ai_gold inference and evaluation" >&2\n'
            "    exit 1\n"
            "  fi\n"
        ),
        "final ai_gold function guard",
    )
    replace_once(
        '      --candidate final \\\n      --resume-journal "$lane_root/predictions.resume.json" \\\n',
        (
            "      --candidate final \\\n"
            '      --resume-journal "$EXTERNAL_JOURNAL_ROOT/final/'
            '$suite_id/predictions.resume.json" \\\n'
        ),
        "external final journal",
    )
    replace_once(
        '      --candidate base_model \\\n      --resume-journal "$lane_root/predictions.resume.json" \\\n',
        (
            "      --candidate base_model \\\n"
            '      --resume-journal "$EXTERNAL_JOURNAL_ROOT/base_model/'
            '$suite_id/predictions.resume.json" \\\n'
        ),
        "external base journal",
    )
    replace_once(
        '  run_final_suite "$SUITE_ID" "$REFERENCES"\n  run_base_suite "$SUITE_ID" "$REFERENCES"\n',
        (
            '  if [ "$SUITE_ID" = ai_gold ]; then\n'
            '    PYTHONPATH="$V4_PROJECT/src" '
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "      --mode preserved-lane \\\n"
            '      --packet-root "$V4_ROOT" \\\n'
            '      --retry-packet-root "$RETRY_ROOT" \\\n'
            '      --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '      --primary-packet-root "$PACKET_ROOT" \\\n'
            '      --expected-packet-tree-sha256 "$EXPECTED_V4_TREE_SHA256" \\\n'
            '      --output-root "$OUTPUT_ROOT"\n'
            "  else\n"
            '    run_final_suite "$SUITE_ID" "$REFERENCES"\n'
            "  fi\n"
            '  run_base_suite "$SUITE_ID" "$REFERENCES"\n'
        ),
        "final ai_gold loop skip",
    )
    replace_once(
        'PYTHONPATH="$RETRY_PROJECT/src" '
        'python "$RETRY_PROJECT/scripts/verify_hf_final_evaluation_retry.py" \\\n'
        "  --mode runtime-complete \\\n"
        '  --packet-root "$RETRY_ROOT" \\\n'
        '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
        '  --primary-packet-root "$PACKET_ROOT" \\\n'
        '  --expected-packet-tree-sha256 "$EXPECTED_RETRY_TREE_SHA256" \\\n',
        (
            'PYTHONPATH="$V4_PROJECT/src" '
            'python "$V4_PROJECT/scripts/verify_hf_final_evaluation_v4.py" \\\n'
            "  --mode runtime-complete \\\n"
            '  --packet-root "$V4_ROOT" \\\n'
            '  --retry-packet-root "$RETRY_ROOT" \\\n'
            '  --amendment-packet-root "$AMENDMENT_ROOT" \\\n'
            '  --primary-packet-root "$PACKET_ROOT" \\\n'
            '  --expected-packet-tree-sha256 "$EXPECTED_V4_TREE_SHA256" \\\n'
        ),
        "V4 runtime completion",
    )
    replace_once(
        '  --output-root "$OUTPUT_ROOT" \\\n'
        '  --job-id "$JOB_ID" \\\n'
        '  --launch-id "$LAUNCH_ID"\n\n'
        'echo "cloud_final_evaluation=complete training_steps_executed=0 retry_v3=complete" >&2\n',
        (
            '  --output-root "$OUTPUT_ROOT" \\\n'
            '  --external-journal-root "$EXTERNAL_JOURNAL_ROOT" \\\n'
            '  --job-id "$JOB_ID" \\\n'
            '  --launch-id "$LAUNCH_ID"\n\n'
            'echo "cloud_final_evaluation=complete training_steps_executed=0 recovery_v4=complete" >&2\n'
        ),
        "V4 completion marker",
    )
    if (
        '"$lane_root/predictions.resume.json"' in script
        or "recover-unbound" in script
        or "hf buckets rm" in script
        or "--recursive" in script
    ):
        raise FinalEvaluationV4Error("V4 runner retains a forbidden mutation/resume path")
    return script.replace("\r\n", "\n").encode("utf-8")


def _validate_manifest_schema(value: Mapping[str, object]) -> dict[str, object]:
    if not _SCHEMA.is_file():
        raise FinalEvaluationV4Error(f"V4 manifest schema is missing: {_SCHEMA}")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise FinalEvaluationV4Error(f"V4 manifest schema failed at {location}: {errors[0].message}")
    return dict(value)


def _load_v4_job_inputs(
    input_dir: Path,
    *,
    v3_manifest: Mapping[str, object],
    allow_v4_manifest: bool = False,
) -> dict[str, object]:
    expected_names = {
        "input-manifest.json",
        "v3-terminal.json",
        "no-active-jobs.json",
        "authorization.json",
        "launch-ledger-v3.json",
        "stale-journal-evidence.json",
        "exact-removal-plan.json",
        "post-removal-proof.json",
    }
    if input_dir.is_symlink() or not input_dir.is_dir():
        raise FinalEvaluationV4Error("V4 job-input directory is missing or symlinked")
    actual_names: set[str] = set()
    for path in input_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise FinalEvaluationV4Error(f"V4 job input is not a regular file: {path.name}")
        actual_names.add(path.name)
    allowed_names = expected_names | {"v4-manifest.json"} if allow_v4_manifest else expected_names
    if actual_names != allowed_names:
        raise FinalEvaluationV4Error(
            "V4 job-input inventory differs "
            f"(missing={sorted(allowed_names - actual_names)}, "
            f"extra={sorted(actual_names - allowed_names)})"
        )
    input_manifest_payload, input_manifest = _canonical_file(
        input_dir / "input-manifest.json",
        "V4 input manifest",
    )
    payloads: dict[str, bytes] = {}
    values: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_names - {"input-manifest.json"}):
        payload, value = _canonical_file(input_dir / name, f"V4 job input {name}")
        payloads[name] = payload
        values[name] = value
    expected_files = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
        for name, payload in sorted(payloads.items())
    ]
    expected_input_manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_job_inputs",
        "status": "validated",
        "v3_packet_tree_sha256": V3_PACKET_TREE_SHA256,
        "v3_job_id": V3_JOB_ID,
        "v3_launch_id": _launch_id(V3_JOB_ID),
        "files": expected_files,
        "post_removal_tree_sha256": values["post-removal-proof.json"]["snapshot"]["tree_sha256"],
        "v3_ledger_sha256": _sha256(payloads["launch-ledger-v3.json"]),
        "v3_terminal_sha256": _sha256(payloads["v3-terminal.json"]),
        "authorization_sha256": _sha256(payloads["authorization.json"]),
        "no_active_jobs_sha256": _sha256(payloads["no-active-jobs.json"]),
        "remote_mutation_performed": False,
        "gpu_job_started": False,
    }
    if input_manifest != expected_input_manifest:
        raise FinalEvaluationV4Error("V4 input manifest differs from its canonical files")
    exact_hashes = {
        "launch-ledger-v3.json": V3_LEDGER_SHA256,
        "stale-journal-evidence.json": STALE_JOURNAL_EVIDENCE_SHA256,
        "exact-removal-plan.json": EXACT_REMOVAL_PLAN_SHA256,
        "post-removal-proof.json": POST_REMOVAL_PROOF_SHA256,
        "v3-terminal.json": V3_TERMINAL_EVIDENCE_SHA256,
        "no-active-jobs.json": NO_ACTIVE_JOBS_EVIDENCE_SHA256,
        "authorization.json": V4_AUTHORIZATION_SHA256,
    }
    if any(_sha256(payloads[name]) != expected for name, expected in exact_hashes.items()):
        raise FinalEvaluationV4Error("V4 job input differs from its exact reviewed SHA-256")
    if len(payloads["launch-ledger-v3.json"]) != V3_LEDGER_BYTES:
        raise FinalEvaluationV4Error("V3 launch ledger byte length changed")
    try:
        ledger = _validate_v3_ledger(
            values["launch-ledger-v3.json"],
            manifest=v3_manifest,
            current_job_id=V3_JOB_ID,
            current_launch_id=_launch_id(V3_JOB_ID),
        )
    except FinalEvaluationRetryError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    terminal = validate_v3_terminal_evidence(values["v3-terminal.json"])
    no_active = validate_no_active_jobs_evidence(values["no-active-jobs.json"])
    authorization = validate_v4_authorization(values["authorization.json"])
    evidence = values["stale-journal-evidence.json"]
    plan = values["exact-removal-plan.json"]
    if (
        evidence.get("safe_to_remove_exact_journal") is not True
        or evidence.get("record_count") != FINAL_AI_GOLD_RECORD_COUNT
        or evidence.get("journal_prefix_count") != FINAL_AI_GOLD_STALE_PREFIX_COUNT
        or evidence.get("tail_count") != FINAL_AI_GOLD_TAIL_COUNT
        or plan.get("recursive") is not False
        or plan.get("target_count") != 1
        or plan.get("target", {}).get("uri") != STALE_JOURNAL_REMOTE_URI
        or plan.get("target", {}).get("sha256") != FINAL_AI_GOLD_JOURNAL_SHA256
    ):
        raise FinalEvaluationV4Error("V4 cleanup evidence differs from the exact stale journal")
    post = validate_post_removal_proof(
        values["post-removal-proof.json"],
        evidence=evidence,
        removal_plan=plan,
    )
    return {
        "input_manifest_payload": input_manifest_payload,
        "input_manifest": input_manifest,
        "payloads": payloads,
        "v3_ledger": ledger,
        "v3_terminal": terminal,
        "no_active_jobs": no_active,
        "authorization": authorization,
        "stale_journal_evidence": evidence,
        "removal_plan": plan,
        "post_removal_proof": post,
    }


def build_hf_final_evaluation_v4_packet(
    *,
    primary_packet_root: str | Path,
    amendment_packet_root: str | Path,
    retry_packet_root: str | Path,
    job_input_dir: str | Path,
    post_removal_snapshot_root: str | Path,
    output_dir: str | Path,
) -> HfFinalEvaluationV4Packet:
    """Build V4 without changing Bucket state or starting a GPU job."""

    primary = Path(primary_packet_root).expanduser().resolve()
    amendment = Path(amendment_packet_root).expanduser().resolve()
    retry = Path(retry_packet_root).expanduser().resolve()
    try:
        v3_manifest = verify_hf_final_evaluation_retry_packet(
            retry,
            amendment_packet_root=amendment,
            primary_packet_root=primary,
            expected_packet_tree_sha256=V3_PACKET_TREE_SHA256,
        )
    except FinalEvaluationRetryError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    inputs = _load_v4_job_inputs(
        Path(job_input_dir).expanduser().resolve(),
        v3_manifest=v3_manifest,
    )
    snapshot_root = Path(post_removal_snapshot_root).expanduser().resolve()
    if snapshot_root.is_symlink() or not snapshot_root.is_dir():
        raise FinalEvaluationV4Error("post-removal snapshot root is missing or symlinked")
    try:
        snapshot = _inventory_tree(snapshot_root)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if snapshot != inputs["post_removal_proof"]["snapshot"]:
        raise FinalEvaluationV4Error("fresh V4 output snapshot differs from the post-removal proof")
    try:
        preserved = verify_final_evaluation_lane(
            manifest_path=primary / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json",
            candidate=FINAL_AI_GOLD_CANDIDATE,
            suite_id=FINAL_AI_GOLD_SUITE,
            lane_root=snapshot_root / "final/ai_gold",
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if preserved != inputs["post_removal_proof"]["preserved_lane"]:
        raise FinalEvaluationV4Error("fresh V4 preserved lane changed after its proof")

    primary_runner = _stable_bytes(
        primary / "run-final-evaluation.sh",
        "primary final-evaluation runner",
    )
    runner = v4_runner_payload(primary_runner)
    target = Path(output_dir).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise FinalEvaluationV4Error(f"refusing to overwrite V4 packet: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        project = temporary / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        _copy_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_tree(_PROJECT_ROOT / "contracts", project / "contracts")
        (project / "scripts").mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "scripts/verify_hf_final_evaluation_v4.py",
            project / "scripts/verify_hf_final_evaluation_v4.py",
        )
        packet_inputs = project / "job-input"
        packet_inputs.mkdir()
        source_inputs = Path(job_input_dir).expanduser().resolve()
        for name in (
            "input-manifest.json",
            "v3-terminal.json",
            "no-active-jobs.json",
            "authorization.json",
            "launch-ledger-v3.json",
            "stale-journal-evidence.json",
            "exact-removal-plan.json",
            "post-removal-proof.json",
        ):
            shutil.copy2(source_inputs / name, packet_inputs / name)
        (temporary / "run-final-evaluation-v4.sh").write_bytes(runner)

        v2_job_manifest = _stable_bytes(
            amendment / "job-manifest.json",
            "V2 job manifest",
        )
        v2_runner = _stable_bytes(
            amendment / "run-final-evaluation-amendment.sh",
            "V2 runner",
        )
        v3_job_manifest = _stable_bytes(
            retry / "job-manifest.json",
            "V3 job manifest",
        )
        v3_runner = _stable_bytes(
            retry / "run-final-evaluation-retry.sh",
            "V3 runner",
        )
        core = {
            "schema_version": 4,
            "kind": "scriber_hf_final_evaluation_recovery_v4",
            "source_git_head": _git_head(),
            "image": v3_manifest["image"],
            "flavor": V4_FLAVOR,
            "timeout_minutes": V4_TIMEOUT_MINUTES,
            "batch_size": FINAL_EVALUATION_BATCH_SIZE,
            "max_new_tokens": 512,
            "primary_packet": dict(v3_manifest["primary_packet"]),
            "amendment_packet": {
                "packet_tree_sha256": v3_manifest["amendment_packet"]["packet_tree_sha256"],
                "job_manifest_sha256": _sha256(v2_job_manifest),
                "runner_sha256": _sha256(v2_runner),
            },
            "retry_packet": {
                "packet_tree_sha256": V3_PACKET_TREE_SHA256,
                "job_manifest_sha256": _sha256(v3_job_manifest),
                "runner_sha256": _sha256(v3_runner),
            },
            "v3_terminal": inputs["v3_terminal"],
            "v3_terminal_sha256": V3_TERMINAL_EVIDENCE_SHA256,
            "v3_launch_ledger": {
                "relative_path": RETRY_LEDGER_SIBLING,
                "bytes": V3_LEDGER_BYTES,
                "sha256": V3_LEDGER_SHA256,
                "job_id": V3_JOB_ID,
                "launch_id": _launch_id(V3_JOB_ID),
            },
            "no_active_jobs": inputs["no_active_jobs"],
            "no_active_jobs_sha256": NO_ACTIVE_JOBS_EVIDENCE_SHA256,
            "authorization": inputs["authorization"],
            "authorization_sha256": V4_AUTHORIZATION_SHA256,
            "journal_recovery": {
                "method": "exact_external_bucket_single_object_delete",
                "stale_journal_evidence_sha256": STALE_JOURNAL_EVIDENCE_SHA256,
                "exact_removal_plan_sha256": EXACT_REMOVAL_PLAN_SHA256,
                "post_removal_proof_sha256": POST_REMOVAL_PROOF_SHA256,
                "target": inputs["removal_plan"]["target"],
                "recursive_delete_performed": False,
                "pre_removal_tree_sha256": inputs["removal_plan"]["pre_removal_snapshot"]["tree_sha256"],
                "post_removal_tree_sha256": snapshot["tree_sha256"],
                "stale_journal_absent": True,
                "preserved_lane": preserved,
            },
            "output": {
                "bucket_root": OUTPUT_BUCKET,
                "relative_path": "attempt8/scriber-1100-final-evaluation-v1",
                "container_path": OUTPUT_ROOT,
                "remote_result_uri": REMOTE_RESULT_URI,
                "primary_launch_ledger_v1_sha256": v3_manifest["output"]["primary_launch_ledger_v1_sha256"],
                "v2_ledger_sibling": AMENDMENT_LEDGER_SIBLING,
                "v2_runtime_completion_sibling": RUNTIME_COMPLETION_SIBLING,
                "v3_ledger_sibling": RETRY_LEDGER_SIBLING,
                "v3_runtime_completion_sibling": RETRY_RUNTIME_COMPLETION_SIBLING,
                "v4_ledger_sibling": V4_LEDGER_SIBLING,
                "v4_runtime_completion_sibling": V4_RUNTIME_COMPLETION_SIBLING,
                "writer_lock_sibling": WRITER_LOCK_SIBLING,
                "external_journal_root": EXTERNAL_JOURNAL_ROOT,
                "external_journal_remote_uri": (
                    OUTPUT_BUCKET + "/attempt8/.scriber-1100-final-evaluation-v1.resume-journals-v4"
                ),
                "snapshot": snapshot,
            },
            "budget": _v4_budget(),
            "resume_strategy": {
                "kind": "external_stable_resume_journals_v4",
                "result_root_contains_resume_journals": False,
                "restart_compatible": True,
                "final_ai_gold_action": "verify_only_never_infer_or_evaluate",
                "allowed_external_journals": [
                    "final/critical_regression/predictions.resume.json",
                    "final/regular_test/predictions.resume.json",
                    "base_model/ai_gold/predictions.resume.json",
                    "base_model/critical_regression/predictions.resume.json",
                    "base_model/regular_test/predictions.resume.json",
                ],
                "post_terminal_cleanup": "exact_api_listing_hash_validation_and_nonrecursive_delete",
                "recursive_cleanup_allowed": False,
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
        (packet_inputs / "v4-manifest.json").write_bytes(canonical_json_bytes(core))
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
    return HfFinalEvaluationV4Packet(
        output_dir=target,
        manifest=MappingProxyType(manifest),
    )


def _load_v4_manifest(packet_root: Path) -> tuple[bytes, dict[str, object]]:
    payload, value = _canonical_file(
        packet_root / "job-manifest.json",
        "V4 job manifest",
    )
    return payload, _validate_manifest_schema(value)


def verify_hf_final_evaluation_v4_packet(
    packet_root: str | Path,
    *,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    """Verify the complete V4→V3→V2→V1 packet chain."""

    packet = Path(packet_root).expanduser().resolve()
    if packet.is_symlink() or not packet.is_dir():
        raise FinalEvaluationV4Error("V4 packet is missing or symlinked")
    _, manifest = _load_v4_manifest(packet)
    inventory, tree = _packet_inventory(packet)
    if (
        tree != expected_packet_tree_sha256
        or manifest["packet_tree_sha256"] != tree
        or manifest["file_count"] != len(inventory)
        or manifest["byte_count"] != sum(int(item["bytes"]) for item in inventory)
    ):
        raise FinalEvaluationV4Error("V4 packet differs from its launch-bound inventory")
    retry = Path(retry_packet_root).expanduser().resolve()
    amendment = Path(amendment_packet_root).expanduser().resolve()
    primary = Path(primary_packet_root).expanduser().resolve()
    try:
        v3_manifest = verify_hf_final_evaluation_retry_packet(
            retry,
            amendment_packet_root=amendment,
            primary_packet_root=primary,
            expected_packet_tree_sha256=V3_PACKET_TREE_SHA256,
        )
    except FinalEvaluationRetryError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if manifest["primary_packet"] != v3_manifest["primary_packet"]:
        raise FinalEvaluationV4Error("V4 primary packet binding changed")
    expected_v2 = {
        "packet_tree_sha256": v3_manifest["amendment_packet"]["packet_tree_sha256"],
        "job_manifest_sha256": _sha256(_stable_bytes(amendment / "job-manifest.json", "V2 job manifest")),
        "runner_sha256": _sha256(
            _stable_bytes(
                amendment / "run-final-evaluation-amendment.sh",
                "V2 runner",
            )
        ),
    }
    expected_v3 = {
        "packet_tree_sha256": V3_PACKET_TREE_SHA256,
        "job_manifest_sha256": _sha256(_stable_bytes(retry / "job-manifest.json", "V3 job manifest")),
        "runner_sha256": _sha256(
            _stable_bytes(
                retry / "run-final-evaluation-retry.sh",
                "V3 runner",
            )
        ),
    }
    if manifest["amendment_packet"] != expected_v2 or manifest["retry_packet"] != expected_v3:
        raise FinalEvaluationV4Error("V4 predecessor packet byte binding changed")
    inputs = _load_v4_job_inputs(
        packet / "repo/ml/scriber_polishing/job-input",
        v3_manifest=v3_manifest,
        allow_v4_manifest=True,
    )
    core_payload, core = _canonical_file(
        packet / "repo/ml/scriber_polishing/job-input/v4-manifest.json",
        "V4 core manifest",
    )
    del core_payload
    packet_fields = {"runner_sha256", "packet_tree_sha256", "file_count", "byte_count"}
    if core != {key: value for key, value in manifest.items() if key not in packet_fields}:
        raise FinalEvaluationV4Error("V4 durable core differs from its job manifest")
    primary_runner = _stable_bytes(
        primary / "run-final-evaluation.sh",
        "primary final-evaluation runner",
    )
    runner = _stable_bytes(
        packet / "run-final-evaluation-v4.sh",
        "V4 runner",
    )
    if manifest["runner_sha256"] != _sha256(runner) or runner != v4_runner_payload(primary_runner):
        raise FinalEvaluationV4Error("V4 runner differs from the reviewed transformation")
    expected_fixed = {
        "v3_terminal": inputs["v3_terminal"],
        "v3_terminal_sha256": V3_TERMINAL_EVIDENCE_SHA256,
        "no_active_jobs": inputs["no_active_jobs"],
        "no_active_jobs_sha256": NO_ACTIVE_JOBS_EVIDENCE_SHA256,
        "authorization": inputs["authorization"],
        "authorization_sha256": V4_AUTHORIZATION_SHA256,
        "budget": _v4_budget(),
    }
    if any(manifest.get(key) != value for key, value in expected_fixed.items()):
        raise FinalEvaluationV4Error("V4 exact evidence/budget binding changed")
    return manifest


def build_hf_final_evaluation_v4_launch_contract(
    packet_root: str | Path,
    *,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
) -> dict[str, object]:
    """Build, but never execute, the single authorized V4 L4 launch."""

    packet = Path(packet_root).expanduser().resolve()
    retry = Path(retry_packet_root).expanduser().resolve()
    amendment = Path(amendment_packet_root).expanduser().resolve()
    primary = Path(primary_packet_root).expanduser().resolve()
    _, raw_manifest = _load_v4_manifest(packet)
    manifest = verify_hf_final_evaluation_v4_packet(
        packet,
        retry_packet_root=retry,
        amendment_packet_root=amendment,
        primary_packet_root=primary,
        expected_packet_tree_sha256=str(raw_manifest["packet_tree_sha256"]),
    )
    primary_manifest = json.loads(_stable_bytes(primary / "job-manifest.json", "primary job manifest"))
    v4_volume = f"{packet}:/input/v4:ro"
    retry_volume = f"{retry}:/input/retry:ro"
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
        V4_FLAVOR,
        "--timeout",
        f"{V4_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=final-evaluation-1100-recovery-v4",
        "-v",
        v4_volume,
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
        str(manifest["image"]),
        "bash",
        "/input/v4/run-final-evaluation-v4.sh",
        str(manifest["primary_packet"]["packet_tree_sha256"]),
        str(manifest["amendment_packet"]["packet_tree_sha256"]),
        str(manifest["retry_packet"]["packet_tree_sha256"]),
        str(manifest["packet_tree_sha256"]),
    ]
    return {
        "schema_version": 4,
        "kind": "scriber_hf_final_evaluation_v4_launch_contract",
        "flavor": V4_FLAVOR,
        "timeout": f"{V4_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": V4_MAXIMUM_COST_USD,
        "overall_maximum_usd": OVERALL_MAXIMUM_USD,
        "overall_total_budget_usd": OVERALL_TOTAL_BUDGET_USD,
        "overall_remaining_usd": OVERALL_REMAINING_USD,
        "strictly_below_overall_budget": True,
        "budget_increase_used": True,
        "primary_packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
        "amendment_packet_tree_sha256": manifest["amendment_packet"]["packet_tree_sha256"],
        "retry_packet_tree_sha256": manifest["retry_packet"]["packet_tree_sha256"],
        "v4_packet_tree_sha256": manifest["packet_tree_sha256"],
        "v3_job_id": V3_JOB_ID,
        "v3_actual_cost_usd": V3_ACTUAL_COST_USD,
        "v3_terminal_sha256": V3_TERMINAL_EVIDENCE_SHA256,
        "v3_ledger_sha256": V3_LEDGER_SHA256,
        "post_removal_proof_sha256": POST_REMOVAL_PROOF_SHA256,
        "authorization_sha256": V4_AUTHORIZATION_SHA256,
        "post_removal_tree_sha256": manifest["output"]["snapshot"]["tree_sha256"],
        "preserved_final_ai_gold_prediction_sha256": FINAL_AI_GOLD_PREDICTION_SHA256,
        "final_ai_gold_action": "verify_only_never_infer_or_evaluate",
        "external_journal_root": EXTERNAL_JOURNAL_ROOT,
        "external_cleanup": "post_terminal_exact_nonrecursive_only",
        "training_steps_executed": 0,
        "publication_allowed": False,
        "external_job_started": False,
        "v4_volume": v4_volume,
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


def _snapshot_identity(snapshot: Mapping[str, object]) -> dict[str, object]:
    return {key: snapshot[key] for key in ("directories", "files", "file_count", "byte_count", "tree_sha256")}


def _external_journal_root_for_output(root: Path) -> Path:
    return root.parent / EXTERNAL_JOURNAL_SIBLING


def _load_bound_v3_ledger(
    root: Path,
    *,
    manifest: Mapping[str, object],
) -> tuple[bytes, dict[str, object]]:
    payload, raw = _canonical_file(
        root.parent / RETRY_LEDGER_SIBLING,
        "V3 launch ledger",
    )
    if len(payload) != V3_LEDGER_BYTES or _sha256(payload) != V3_LEDGER_SHA256:
        raise FinalEvaluationV4Error("remote V3 launch ledger differs from the packet binding")
    retry_manifest = manifest["retry_predecessor_manifest"]
    try:
        ledger = _validate_v3_ledger(
            raw,
            manifest=retry_manifest,
            current_job_id=V3_JOB_ID,
            current_launch_id=_launch_id(V3_JOB_ID),
        )
    except FinalEvaluationRetryError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    return payload, ledger


def _assert_v4_preclaim_state(
    root: Path,
    *,
    manifest: Mapping[str, object],
) -> tuple[bytes, dict[str, object]]:
    try:
        inventory = _inventory_tree(root)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if inventory != _snapshot_identity(manifest["output"]["snapshot"]):
        raise FinalEvaluationV4Error("remote V4 result root differs from the post-removal snapshot")
    forbidden = (
        AMENDMENT_LEDGER_SIBLING,
        RUNTIME_COMPLETION_SIBLING,
        RETRY_RUNTIME_COMPLETION_SIBLING,
        V4_LEDGER_SIBLING,
        V4_RUNTIME_COMPLETION_SIBLING,
        WRITER_LOCK_SIBLING,
    )
    present = [name for name in forbidden if (root.parent / name).exists()]
    if present:
        raise FinalEvaluationV4Error("V4 preclaim has forbidden sibling state: " + ", ".join(present))
    external = _external_journal_root_for_output(root)
    if external.exists() or external.is_symlink():
        raise FinalEvaluationV4Error("V4 external journal root must be absent before first claim")
    if (root / "final-evaluation-result.json").exists():
        raise FinalEvaluationV4Error("V4 preclaim result root is already finalized")
    return _load_bound_v3_ledger(root, manifest=manifest)


def _v4_ledger(
    *,
    manifest: Mapping[str, object],
    v3_ledger_payload: bytes,
    job_id: str,
    launch_id: str,
) -> dict[str, object]:
    retry_manifest = manifest["retry_predecessor_manifest"]
    return {
        "schema_version": 4,
        "kind": "scriber_hf_final_evaluation_launch_ledger",
        "overall_total_budget_usd": OVERALL_TOTAL_BUDGET_USD,
        "authorization_sha256": V4_AUTHORIZATION_SHA256,
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "retry_launch_ledger_v3_sha256": _sha256(v3_ledger_payload),
        "v3_terminal_sha256": V3_TERMINAL_EVIDENCE_SHA256,
        "post_removal_proof_sha256": POST_REMOVAL_PROOF_SHA256,
        "evaluation_launches": [
            {
                "sequence": 1,
                "role": "primary",
                "job_id": PRIMARY_JOB_ID,
                "launch_id": _launch_id(PRIMARY_JOB_ID),
                "terminal_status": "CANCELED",
                "cost_status": "unknown",
                "budget_accounting": "full_timeout_reservation",
                "flavor": V4_FLAVOR,
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
                "accounted_cost_usd": FAILED_AMENDMENT_ACTUAL_COST_USD,
                "packet_tree_sha256": manifest["amendment_packet"]["packet_tree_sha256"],
            },
            {
                "sequence": 3,
                "role": "v3_replacement",
                "job_id": V3_JOB_ID,
                "launch_id": _launch_id(V3_JOB_ID),
                "terminal_status": "ERROR",
                "failure_stage": "lane_verification",
                "completed_final_ai_gold_records": FINAL_AI_GOLD_RECORD_COUNT,
                "accounted_cost_usd": V3_ACTUAL_COST_USD,
                "packet_tree_sha256": V3_PACKET_TREE_SHA256,
                "v3_ledger_packet_tree_sha256": retry_manifest["packet_tree_sha256"],
            },
            {
                "sequence": 4,
                "role": "v4_cleanup_replacement",
                "job_id": job_id,
                "launch_id": launch_id,
                "claim_status": "running",
                "flavor": V4_FLAVOR,
                "timeout_minutes": V4_TIMEOUT_MINUTES,
                "accounted_cost_usd": V4_MAXIMUM_COST_USD,
                "packet_tree_sha256": manifest["packet_tree_sha256"],
            },
        ],
        "budget": _v4_budget(),
        "final_ai_gold_inference_allowed": False,
        "final_ai_gold_evaluation_allowed": False,
        "external_journal_root": EXTERNAL_JOURNAL_ROOT,
        "external_journal_cleanup": "required_after_terminal_exact_nonrecursive",
    }


def _validate_v4_ledger(
    value: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    v3_ledger_payload: bytes,
    job_id: str,
    launch_id: str,
) -> dict[str, object]:
    expected = _v4_ledger(
        manifest=manifest,
        v3_ledger_payload=v3_ledger_payload,
        job_id=job_id,
        launch_id=launch_id,
    )
    if dict(value) != expected:
        raise FinalEvaluationV4Error("V4 launch ledger differs from the exact provenance/budget")
    return expected


def _packet_with_runtime_predecessor(
    packet_root: str | Path,
    *,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
) -> dict[str, object]:
    manifest = verify_hf_final_evaluation_v4_packet(
        packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    retry_manifest = verify_hf_final_evaluation_retry_packet(
        retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=V3_PACKET_TREE_SHA256,
    )
    return {**manifest, "retry_predecessor_manifest": retry_manifest}


def verify_preserved_final_ai_gold_for_v4(
    *,
    packet_root: str | Path,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
) -> dict[str, object]:
    """Verify final/ai_gold without permitting inference or evaluation."""

    manifest = _packet_with_runtime_predecessor(
        packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    try:
        verified = verify_final_evaluation_lane(
            manifest_path=Path(primary_packet_root).expanduser().resolve()
            / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json",
            candidate=FINAL_AI_GOLD_CANDIDATE,
            suite_id=FINAL_AI_GOLD_SUITE,
            lane_root=root / "final/ai_gold",
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if verified != manifest["journal_recovery"]["preserved_lane"]:
        raise FinalEvaluationV4Error("preserved final/ai_gold lane changed")
    return {
        "status": "passed",
        "candidate": FINAL_AI_GOLD_CANDIDATE,
        "suite": FINAL_AI_GOLD_SUITE,
        "action": "verify_only",
        "inference_invoked": False,
        "evaluation_invoked": False,
        **verified,
    }


def claim_final_evaluation_v4(
    *,
    packet_root: str | Path,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    job_id: str,
    launch_id: str,
    owner_token: str,
) -> dict[str, object]:
    """Atomically claim V4 only from the exact post-removal V3 state."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or job_id in {PRIMARY_JOB_ID, FAILED_AMENDMENT_JOB_ID, V3_JOB_ID}
        or _LAUNCH_ID.fullmatch(launch_id) is None
        or _OWNER_TOKEN.fullmatch(owner_token) is None
        or launch_id != _launch_id(job_id)
    ):
        raise FinalEvaluationV4Error("V4 runtime job/launch/owner identity is invalid")
    manifest = _packet_with_runtime_predecessor(
        packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    v3_ledger_payload, _ = _assert_v4_preclaim_state(root, manifest=manifest)
    verify_preserved_final_ai_gold_for_v4(
        packet_root=packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
        output_root=root,
    )
    acquired = False
    try:
        _acquire_final_evaluation_writer(
            root,
            launch_id=launch_id,
            owner_token=owner_token,
        )
        acquired = True
        if _inventory_tree(root) != _snapshot_identity(manifest["output"]["snapshot"]):
            raise FinalEvaluationV4Error("result root changed while claiming V4")
        ledger = _v4_ledger(
            manifest=manifest,
            v3_ledger_payload=v3_ledger_payload,
            job_id=job_id,
            launch_id=launch_id,
        )
        ledger_path = root.parent / V4_LEDGER_SIBLING
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
        "schema_version": 4,
        "kind": "scriber_hf_final_evaluation_v4_launch_claim",
        "job_id": job_id,
        "launch_id": launch_id,
        "writer_acquired": True,
        "v3_ledger_sha256": V3_LEDGER_SHA256,
        "v4_ledger_sibling": V4_LEDGER_SIBLING,
        "final_ai_gold_action": "verify_only",
        "maximum_job_cost_usd": V4_MAXIMUM_COST_USD,
        "overall_maximum_usd": OVERALL_MAXIMUM_USD,
        "overall_remaining_usd": OVERALL_REMAINING_USD,
    }


def _validate_external_journal_root(
    *,
    external_root: Path,
    output_root: Path,
    primary_manifest_path: Path,
) -> dict[str, object]:
    if external_root.is_symlink() or not external_root.is_dir():
        raise FinalEvaluationV4Error("V4 external journal root is missing or symlinked")
    try:
        inventory = _inventory_tree(external_root)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    allowed = set(_ALLOWED_EXTERNAL_JOURNALS)
    actual = {str(item["path"]) for item in inventory["files"]}
    if not actual.issubset(allowed):
        raise FinalEvaluationV4Error("V4 external journal root contains an unexpected object")
    validated: list[dict[str, object]] = []
    for relative in sorted(actual):
        candidate, suite_id, filename = relative.split("/")
        if filename != "predictions.resume.json":
            raise FinalEvaluationV4Error("V4 external journal path is invalid")
        lane = output_root / candidate / suite_id
        with tempfile.TemporaryDirectory(prefix="scriber-v4-external-journal-proof-") as temporary:
            proof_lane = Path(temporary) / "lane"
            shutil.copytree(lane, proof_lane)
            shutil.copy2(external_root / relative, proof_lane / filename)
            evidence = validate_stale_resume_journal_candidate(
                manifest_path=primary_manifest_path,
                candidate=candidate,
                suite_id=suite_id,
                lane_root=proof_lane,
            )
        validated.append(
            {
                "relative_path": relative,
                "bytes": next(item["bytes"] for item in inventory["files"] if item["path"] == relative),
                "sha256": next(item["sha256"] for item in inventory["files"] if item["path"] == relative),
                "journal_prefix_count": evidence["journal_prefix_count"],
                "record_count": evidence["record_count"],
                "tail_count": evidence["tail_count"],
            }
        )
    return {
        "inventory": inventory,
        "validated_journals": validated,
        "allowed_paths": list(_ALLOWED_EXTERNAL_JOURNALS),
        "unexpected_paths": [],
        "cleanup_required": bool(validated),
        "cleanup_method": "post_terminal_exact_api_listing_hash_validation_and_nonrecursive_delete",
    }


def record_v4_runtime_completion(
    *,
    packet_root: str | Path,
    retry_packet_root: str | Path,
    amendment_packet_root: str | Path,
    primary_packet_root: str | Path,
    expected_packet_tree_sha256: str,
    output_root: str | Path,
    external_journal_root: str | Path,
    job_id: str,
    launch_id: str,
) -> dict[str, object]:
    """Record runtime completion while leaving external cleanup post-terminal."""

    manifest = _packet_with_runtime_predecessor(
        packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    root = _bound_final_evaluation_output_root(output_root)
    expected_external = _external_journal_root_for_output(root).resolve()
    external = Path(external_journal_root).expanduser().resolve()
    if external != expected_external:
        raise FinalEvaluationV4Error("runtime external journal root differs from the V4 binding")
    v3_ledger_payload, _ = _load_bound_v3_ledger(root, manifest=manifest)
    v4_ledger_payload, v4_ledger_raw = _canonical_file(
        root.parent / V4_LEDGER_SIBLING,
        "V4 launch ledger",
    )
    _validate_v4_ledger(
        v4_ledger_raw,
        manifest=manifest,
        v3_ledger_payload=v3_ledger_payload,
        job_id=job_id,
        launch_id=launch_id,
    )
    preserved = verify_preserved_final_ai_gold_for_v4(
        packet_root=packet_root,
        retry_packet_root=retry_packet_root,
        amendment_packet_root=amendment_packet_root,
        primary_packet_root=primary_packet_root,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
        output_root=root,
    )
    result_payload, result = _canonical_file(
        root / "final-evaluation-result.json",
        "final-evaluation result",
    )
    try:
        result = _validate_result(result)
        provenance = verify_final_evaluation_output_provenance(
            manifest_path=Path(primary_packet_root).expanduser().resolve()
            / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json",
            output_root=root,
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if result.get("status") != "complete" or result.get("packet") != provenance:
        raise FinalEvaluationV4Error("V4 result does not complete the original frozen packet")
    external_evidence = _validate_external_journal_root(
        external_root=external,
        output_root=root,
        primary_manifest_path=Path(primary_packet_root).expanduser().resolve()
        / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json",
    )
    completion = {
        "schema_version": 4,
        "kind": "scriber_hf_final_evaluation_v4_runtime_completion",
        "status": "runtime_complete_pending_terminal_and_external_journal_cleanup",
        "job_id": job_id,
        "launch_id": launch_id,
        "primary_packet_tree_sha256": manifest["primary_packet"]["packet_tree_sha256"],
        "retry_packet_tree_sha256": V3_PACKET_TREE_SHA256,
        "v4_packet_tree_sha256": manifest["packet_tree_sha256"],
        "primary_result_sha256": _sha256(result_payload),
        "primary_launch_ledger_v1_sha256": manifest["output"]["primary_launch_ledger_v1_sha256"],
        "retry_launch_ledger_v3_sha256": _sha256(v3_ledger_payload),
        "v4_launch_ledger_sha256": _sha256(v4_ledger_payload),
        "v3_terminal_sha256": V3_TERMINAL_EVIDENCE_SHA256,
        "post_removal_proof_sha256": POST_REMOVAL_PROOF_SHA256,
        "preserved_final_ai_gold": preserved,
        "external_journals": external_evidence,
        "overall_maximum_usd": OVERALL_MAXIMUM_USD,
        "overall_remaining_usd": OVERALL_REMAINING_USD,
        "training_steps_executed": 0,
        "publication_performed": False,
    }
    destination = root.parent / V4_RUNTIME_COMPLETION_SIBLING
    _write_new_durable_payload(destination, canonical_json_bytes(completion))
    _fsync_parent(destination)
    return completion


def build_v4_external_journal_cleanup_plan(
    *,
    output_root: str | Path,
    external_journal_root: str | Path,
    runtime_completion_path: str | Path,
    terminal_evidence: Mapping[str, object],
) -> dict[str, object]:
    """Plan exact post-terminal API deletes; never delete from this function."""

    root = _bound_final_evaluation_output_root(output_root)
    external = Path(external_journal_root).expanduser().resolve()
    if external != _external_journal_root_for_output(root).resolve():
        raise FinalEvaluationV4Error("cleanup external root differs from the V4 binding")
    completion_payload, completion = _canonical_file(
        runtime_completion_path,
        "V4 runtime completion",
    )
    required_terminal = {
        "schema_version",
        "kind",
        "source",
        "observed_at_utc",
        "job_id",
        "launch_id",
        "terminal_status",
        "v4_packet_tree_sha256",
        "runtime_completion_sha256",
        "writer_released",
    }
    if (
        set(terminal_evidence) != required_terminal
        or terminal_evidence.get("schema_version") != 1
        or terminal_evidence.get("kind") != "scriber_hf_final_evaluation_v4_terminal_evidence"
        or terminal_evidence.get("source") != "hugging_face_jobs_rest_api"
        or terminal_evidence.get("terminal_status") != "COMPLETED"
        or terminal_evidence.get("job_id") != completion.get("job_id")
        or terminal_evidence.get("launch_id") != completion.get("launch_id")
        or terminal_evidence.get("v4_packet_tree_sha256") != completion.get("v4_packet_tree_sha256")
        or terminal_evidence.get("runtime_completion_sha256") != _sha256(completion_payload)
        or terminal_evidence.get("writer_released") is not True
    ):
        raise FinalEvaluationV4Error("external cleanup lacks exact COMPLETED V4 terminal evidence")
    _parse_utc(terminal_evidence["observed_at_utc"], "V4 terminal observation")
    if completion.get("status") != ("runtime_complete_pending_terminal_and_external_journal_cleanup"):
        raise FinalEvaluationV4Error("external cleanup has no exact runtime completion")
    if (root.parent / WRITER_LOCK_SIBLING).exists():
        raise FinalEvaluationV4Error("external cleanup is forbidden while the writer lock exists")
    try:
        current_inventory = _inventory_tree(external)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if {str(item["path"]) for item in current_inventory["files"]}.difference(_ALLOWED_EXTERNAL_JOURNALS):
        raise FinalEvaluationV4Error("external cleanup found an unexpected journal path")
    current = completion.get("external_journals")
    if (
        not isinstance(current, Mapping)
        or current.get("inventory") != current_inventory
        or current.get("unexpected_paths") != []
    ):
        raise FinalEvaluationV4Error("external journals changed after runtime completion")
    remote_root = OUTPUT_BUCKET + "/attempt8/.scriber-1100-final-evaluation-v1.resume-journals-v4"
    targets = [
        {
            **item,
            "uri": f"{remote_root}/{item['relative_path']}",
        }
        for item in current["validated_journals"]
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_external_journal_cleanup_plan",
        "status": "exact_cleanup_required" if targets else "no_cleanup_required",
        "source_mutation_performed": False,
        "job_id": completion["job_id"],
        "launch_id": completion["launch_id"],
        "terminal_evidence_sha256": _sha256(canonical_json_bytes(terminal_evidence)),
        "runtime_completion_sha256": _sha256(completion_payload),
        "external_inventory": current["inventory"],
        "allowed_paths": list(_ALLOWED_EXTERNAL_JOURNALS),
        "targets": targets,
        "target_count": len(targets),
        "unexpected_paths": [],
        "recursive": False,
        "operation": {
            "api": "HfApi.batch_bucket_files",
            "delete": [str(item["relative_path"]) for item in targets],
            "bucket_path": (
                "Buttermilk03/scriber-polishing-private-runs/attempt8/"
                ".scriber-1100-final-evaluation-v1.resume-journals-v4"
            ),
        },
        "requires_authoritative_empty_prefix_relist": True,
    }


def verify_v4_external_journal_cleanup(
    *,
    output_root: str | Path,
    external_journal_root: str | Path,
    cleanup_plan: Mapping[str, object],
) -> dict[str, object]:
    """Verify the authoritative post-delete view is empty and the result remains."""

    root = _bound_final_evaluation_output_root(output_root)
    external = Path(external_journal_root).expanduser().resolve()
    if (
        cleanup_plan.get("kind") != "scriber_hf_final_evaluation_v4_external_journal_cleanup_plan"
        or cleanup_plan.get("recursive") is not False
        or cleanup_plan.get("unexpected_paths") != []
        or cleanup_plan.get("target_count") != len(cleanup_plan.get("targets", []))
        or external != _external_journal_root_for_output(root).resolve()
    ):
        raise FinalEvaluationV4Error("post-cleanup proof differs from the exact V4 plan")
    if (root.parent / WRITER_LOCK_SIBLING).exists():
        raise FinalEvaluationV4Error("post-cleanup proof still has an active writer")
    if external.is_symlink():
        raise FinalEvaluationV4Error("post-cleanup external journal root is symlinked")
    if external.exists():
        try:
            inventory = _inventory_tree(external)
        except FinalEvaluationAmendmentError as error:
            raise FinalEvaluationV4Error(str(error)) from error
        if inventory["files"]:
            raise FinalEvaluationV4Error("post-cleanup external prefix is not empty")
    else:
        inventory = {
            "directories": [],
            "files": [],
            "file_count": 0,
            "byte_count": 0,
            "tree_sha256": _sha256(canonical_json_bytes({"directories": [], "files": []})),
        }
    if not (root / "final-evaluation-result.json").is_file():
        raise FinalEvaluationV4Error("post-cleanup final result is missing")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_external_journal_cleanup_proof",
        "status": "passed",
        "cleanup_plan_sha256": _sha256(canonical_json_bytes(cleanup_plan)),
        "authoritative_prefix_empty": True,
        "external_inventory": inventory,
        "result_preserved": True,
        "recursive_delete_performed": False,
    }


def _canonical_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    payload = _stable_bytes(path, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalEvaluationV4Error(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise FinalEvaluationV4Error(f"{label} must be a JSON object")
    if payload != canonical_json_bytes(value):
        raise FinalEvaluationV4Error(f"{label} is not canonical JSON")
    return payload, value


def _exact_lane_files(lane_root: Path) -> None:
    if lane_root.is_symlink() or not lane_root.is_dir():
        raise FinalEvaluationV4Error("stale-journal lane must be a regular directory")
    actual: set[str] = set()
    for path in lane_root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise FinalEvaluationV4Error(f"stale-journal lane contains an invalid entry: {path.name}")
        actual.add(path.name)
    if actual != _LANE_FILES_WITH_STALE_JOURNAL:
        raise FinalEvaluationV4Error(
            "stale-journal lane inventory differs "
            f"(missing={sorted(_LANE_FILES_WITH_STALE_JOURNAL - actual)}, "
            f"extra={sorted(actual - _LANE_FILES_WITH_STALE_JOURNAL)})"
        )


def _verify_complete_copy_without_journal(
    *,
    manifest_path: Path,
    candidate: str,
    suite_id: str,
    lane_root: Path,
) -> dict[str, object]:
    """Run the unchanged lane verifier against an isolated read-only proof copy."""

    with tempfile.TemporaryDirectory(prefix="scriber-v4-stale-journal-proof-") as temporary:
        proof_root = Path(temporary) / "lane"
        shutil.copytree(lane_root, proof_root)
        copied_journal = proof_root / "predictions.resume.json"
        if copied_journal.is_symlink() or not copied_journal.is_file():
            raise FinalEvaluationV4Error("proof copy has no exact regular resume journal")
        copied_journal.unlink()
        try:
            return verify_final_evaluation_lane(
                manifest_path=manifest_path,
                candidate=candidate,
                suite_id=suite_id,
                lane_root=proof_root,
            )
        except FinalEvaluationError as error:
            raise FinalEvaluationV4Error(
                f"lane artifacts are not independently complete after excluding the copied journal: {error}"
            ) from error


def validate_stale_resume_journal_candidate(
    *,
    manifest_path: str | Path,
    candidate: str,
    suite_id: str,
    lane_root: str | Path,
    maximum_tail_records: int = FINAL_EVALUATION_BATCH_SIZE,
) -> dict[str, object]:
    """Prove that one journal is an exact stale prefix of a complete lane.

    This function never mutates ``lane_root``.  It accepts any valid fixture
    binding for regression tests; the production wrapper below additionally
    enforces the exact frozen model, policy, paths, counts, and artifact hashes.
    """

    manifest_file = Path(manifest_path).expanduser().resolve()
    lane = Path(lane_root).expanduser().resolve()
    _exact_lane_files(lane)
    if isinstance(maximum_tail_records, bool) or not isinstance(maximum_tail_records, int) or maximum_tail_records < 1:
        raise FinalEvaluationV4Error("maximum stale-journal tail must be a positive integer")

    core_file, core = _load_core_manifest(manifest_file)
    suites = core.get("references")
    suite = suites.get(suite_id) if isinstance(suites, Mapping) else None
    if candidate not in {"final", "base_model"} or not isinstance(suite, Mapping):
        raise FinalEvaluationV4Error("stale-journal candidate/suite is not present in the frozen manifest")
    references_path = core_file.parents[1] / "evaluation-input" / str(suite["records_filename"])
    try:
        references = load_evaluation_references(references_path)
        predictions = load_candidate_predictions(lane / "predictions.jsonl")
    except ValueError as error:
        raise FinalEvaluationV4Error(f"stale-journal lane records are invalid: {error}") from error

    journal_payload, journal = _canonical_object(
        lane / "predictions.resume.json",
        "stale resume journal",
    )
    binding = journal.get("binding")
    if not isinstance(binding, Mapping):
        raise FinalEvaluationV4Error("stale resume journal has no request binding")
    try:
        journal = validate_batch_resume_journal(
            journal,
            references=references,
            expected_binding=binding,
        )
    except ValueError as error:
        raise FinalEvaluationV4Error(f"stale resume journal is invalid: {error}") from error

    reference_ids = [str(reference["case_id"]) for reference in references]
    prediction_ids = [str(prediction["case_id"]) for prediction in predictions]
    entries = journal["entries"]
    prefix_count = len(entries)
    record_count = len(predictions)
    tail_count = record_count - prefix_count
    if record_count != len(references) or prediction_ids != reference_ids:
        raise FinalEvaluationV4Error("completed predictions do not exactly cover the frozen references")
    if tail_count < 1 or tail_count > maximum_tail_records:
        raise FinalEvaluationV4Error(
            "completed predictions must extend the stale journal by one non-empty bounded batch"
        )

    prediction_payload = _stable_bytes(lane / "predictions.jsonl", "completed predictions")
    expected_prediction_payload = b"".join(_canonical_json(prediction) for prediction in predictions)
    if prediction_payload != expected_prediction_payload:
        raise FinalEvaluationV4Error("completed predictions are not canonical ordered JSONL")
    journal_prefix_payload = b"".join(_canonical_json(entry["prediction"]) for entry in entries)
    prediction_prefix_payload = b"".join(_canonical_json(prediction) for prediction in predictions[:prefix_count])
    if journal_prefix_payload != prediction_prefix_payload:
        raise FinalEvaluationV4Error("completed prediction prefix differs byte-for-byte from the journal")
    tail_ids = prediction_ids[prefix_count:]
    if tail_ids != reference_ids[prefix_count:]:
        raise FinalEvaluationV4Error("completed prediction tail differs from the frozen reference suffix")

    expected_paths = {
        "predictions_output_path": {
            str((lane / "predictions.jsonl").resolve()),
            f"{OUTPUT_ROOT}/{candidate}/{suite_id}/predictions.jsonl",
        },
        "manifest_output_path": {
            str((lane / "predictions.manifest.json").resolve()),
            f"{OUTPUT_ROOT}/{candidate}/{suite_id}/predictions.manifest.json",
        },
    }
    expected_binding = {
        "candidate": candidate,
        "reference_count": len(references),
        "reference_sha256": _sha256(_stable_bytes(references_path, "frozen references")),
        "reference_case_ids_sha256": _sha256(_canonical_json(reference_ids)),
        "quantization": "none",
        "device": "cuda",
        "max_new_tokens": 512,
        "batch_size": FINAL_EVALUATION_BATCH_SIZE,
    }
    if any(binding.get(key) != value for key, value in expected_binding.items()):
        raise FinalEvaluationV4Error("stale resume journal request differs from the frozen lane")
    if any(binding.get(key) not in allowed for key, allowed in expected_paths.items()):
        raise FinalEvaluationV4Error("stale resume journal output binding differs from the exact lane")

    verified = _verify_complete_copy_without_journal(
        manifest_path=manifest_file,
        candidate=candidate,
        suite_id=suite_id,
        lane_root=lane,
    )
    artifact_paths = {
        "predictions": lane / "predictions.jsonl",
        "prediction_manifest": lane / "predictions.manifest.json",
        "evaluation": lane / "evaluation.json",
        "evaluation_exit": lane / "evaluation-exit.json",
    }
    artifacts = {
        label: {
            "bytes": len(_stable_bytes(path, f"completed {label}")),
            "sha256": _sha256(_stable_bytes(path, f"completed {label}")),
        }
        for label, path in artifact_paths.items()
    }
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_stale_resume_journal_evidence",
        "status": "safe_for_exact_external_single_object_removal",
        "source_mutation_performed": False,
        "candidate": candidate,
        "suite": suite_id,
        "record_count": record_count,
        "journal_prefix_count": prefix_count,
        "tail_count": tail_count,
        "batch_size": FINAL_EVALUATION_BATCH_SIZE,
        "journal": {
            "bytes": len(journal_payload),
            "sha256": _sha256(journal_payload),
            "binding_sha256": journal["binding_sha256"],
            "prefix_prediction_bytes": len(journal_prefix_payload),
            "prefix_prediction_sha256": _sha256(journal_prefix_payload),
        },
        "artifacts": artifacts,
        "tail_case_ids_sha256": _sha256(_canonical_json(tail_ids)),
        "independent_lane_verification": verified,
        "evaluation_exit_preserved": verified["evaluation_exit"],
        "safe_to_remove_exact_journal": True,
    }


def validate_observed_v3_final_ai_gold_stale_journal(
    *,
    manifest_path: str | Path,
    lane_root: str | Path,
) -> dict[str, object]:
    """Apply exact production identities to the observed V3 FUSE race."""

    manifest_file = Path(manifest_path).expanduser().resolve()
    lane = Path(lane_root).expanduser().resolve()
    evidence = validate_stale_resume_journal_candidate(
        manifest_path=manifest_file,
        candidate=FINAL_AI_GOLD_CANDIDATE,
        suite_id=FINAL_AI_GOLD_SUITE,
        lane_root=lane,
    )
    core_file, core = _load_core_manifest(manifest_file)
    suite = core["references"][FINAL_AI_GOLD_SUITE]
    references_path = core_file.parents[1] / "evaluation-input" / str(suite["records_filename"])
    try:
        exact_prefix_count, _ = _read_journal(
            lane / "predictions.resume.json",
            references_path=references_path,
            candidate=FINAL_AI_GOLD_CANDIDATE,
            suite_id=FINAL_AI_GOLD_SUITE,
            core=core,
        )
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error

    expected = {
        "record_count": FINAL_AI_GOLD_RECORD_COUNT,
        "journal_prefix_count": FINAL_AI_GOLD_STALE_PREFIX_COUNT,
        "tail_count": FINAL_AI_GOLD_TAIL_COUNT,
        "batch_size": FINAL_EVALUATION_BATCH_SIZE,
        "evaluation_exit_preserved": 2,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise FinalEvaluationV4Error("observed V3 stale-journal counts differ from the exact recovery")
    if exact_prefix_count != FINAL_AI_GOLD_STALE_PREFIX_COUNT:
        raise FinalEvaluationV4Error("observed V3 journal is not the exact 1744-record prefix")
    expected_artifacts = {
        "predictions": FINAL_AI_GOLD_PREDICTION_SHA256,
        "prediction_manifest": FINAL_AI_GOLD_MANIFEST_SHA256,
        "evaluation": FINAL_AI_GOLD_EVALUATION_SHA256,
        "evaluation_exit": FINAL_AI_GOLD_EXIT_SHA256,
    }
    if (
        evidence["journal"]["bytes"] != FINAL_AI_GOLD_JOURNAL_BYTES
        or evidence["journal"]["sha256"] != FINAL_AI_GOLD_JOURNAL_SHA256
        or any(
            evidence["artifacts"][label]["sha256"] != expected_sha for label, expected_sha in expected_artifacts.items()
        )
    ):
        raise FinalEvaluationV4Error("observed V3 artifact hashes differ from the exact recovery")
    return evidence


def _post_removal_snapshot(
    snapshot: Mapping[str, object],
    *,
    relative_path: str,
) -> dict[str, object]:
    files = snapshot.get("files")
    directories = snapshot.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise FinalEvaluationV4Error("pre-removal snapshot inventory is invalid")
    post_files = [dict(item) for item in files if isinstance(item, Mapping) and item.get("path") != relative_path]
    if len(post_files) != len(files) - 1:
        raise FinalEvaluationV4Error("pre-removal snapshot does not contain exactly one target journal")
    identity = {
        "directories": list(directories),
        "files": post_files,
    }
    return {
        **identity,
        "file_count": len(post_files),
        "byte_count": sum(int(item["bytes"]) for item in post_files),
        "tree_sha256": _sha256(canonical_json_bytes(identity)),
    }


def build_exact_stale_journal_removal_plan(
    *,
    snapshot_root: str | Path,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Build, but never execute, the one-object Bucket API deletion."""

    root = Path(snapshot_root).expanduser().resolve()
    if evidence.get("safe_to_remove_exact_journal") is not True:
        raise FinalEvaluationV4Error("stale-journal evidence does not authorize exact removal")
    try:
        snapshot = _inventory_tree(root)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    matching = [item for item in snapshot["files"] if item["path"] == STALE_JOURNAL_RELATIVE_PATH]
    if (
        len(matching) != 1
        or matching[0]["bytes"] != evidence["journal"]["bytes"]
        or matching[0]["sha256"] != evidence["journal"]["sha256"]
    ):
        raise FinalEvaluationV4Error("snapshot journal differs from the byte-bound removal evidence")
    expected_post = _post_removal_snapshot(
        snapshot,
        relative_path=STALE_JOURNAL_RELATIVE_PATH,
    )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_bucket_exact_stale_resume_journal_removal_plan",
        "status": "validated_not_executed",
        "remote_mutation_performed": False,
        "recursive": False,
        "target_count": 1,
        "target": {
            "uri": STALE_JOURNAL_REMOTE_URI,
            "relative_path": STALE_JOURNAL_RELATIVE_PATH,
            "bytes": matching[0]["bytes"],
            "sha256": matching[0]["sha256"],
        },
        "pre_removal_snapshot": snapshot,
        "expected_post_removal_snapshot": expected_post,
        "stale_journal_evidence_sha256": _sha256(canonical_json_bytes(evidence)),
        "argv": ["hf", "buckets", "rm", STALE_JOURNAL_REMOTE_URI],
        "requires_fresh_post_removal_absence_proof": True,
        "gpu_job_required": False,
    }


def verify_exact_stale_journal_removal(
    *,
    manifest_path: str | Path,
    snapshot_root: str | Path,
    evidence: Mapping[str, object],
    removal_plan: Mapping[str, object],
    observed_at_utc: str,
) -> dict[str, object]:
    """Verify a fresh post-delete mirror without performing the deletion."""

    if not isinstance(observed_at_utc, str) or _UTC_TIMESTAMP.fullmatch(observed_at_utc) is None:
        raise FinalEvaluationV4Error("post-removal observation must be a normalized UTC timestamp")
    try:
        observed = datetime.fromisoformat(observed_at_utc.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise FinalEvaluationV4Error("post-removal observation timestamp is invalid") from error
    if observed.tzinfo != UTC:
        raise FinalEvaluationV4Error("post-removal observation must use UTC")
    root = Path(snapshot_root).expanduser().resolve()
    expected_argv = ["hf", "buckets", "rm", STALE_JOURNAL_REMOTE_URI]
    if (
        removal_plan.get("kind") != "scriber_hf_bucket_exact_stale_resume_journal_removal_plan"
        or removal_plan.get("recursive") is not False
        or removal_plan.get("target_count") != 1
        or removal_plan.get("argv") != expected_argv
        or removal_plan.get("stale_journal_evidence_sha256") != _sha256(canonical_json_bytes(evidence))
        or evidence.get("safe_to_remove_exact_journal") is not True
    ):
        raise FinalEvaluationV4Error("post-removal proof is not bound to the exact one-object plan")
    target = removal_plan.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("uri") != STALE_JOURNAL_REMOTE_URI
        or target.get("relative_path") != STALE_JOURNAL_RELATIVE_PATH
        or target.get("bytes") != FINAL_AI_GOLD_JOURNAL_BYTES
        or target.get("sha256") != FINAL_AI_GOLD_JOURNAL_SHA256
    ):
        raise FinalEvaluationV4Error("post-removal plan target differs from the exact stale journal")
    try:
        snapshot = _inventory_tree(root)
    except FinalEvaluationAmendmentError as error:
        raise FinalEvaluationV4Error(str(error)) from error
    if snapshot != removal_plan.get("expected_post_removal_snapshot"):
        raise FinalEvaluationV4Error("fresh post-removal snapshot differs from the exact expected tree")
    journal_path = root / STALE_JOURNAL_RELATIVE_PATH
    if journal_path.exists() or journal_path.is_symlink():
        raise FinalEvaluationV4Error("fresh post-removal snapshot still contains the stale journal")
    try:
        verified = verify_final_evaluation_lane(
            manifest_path=manifest_path,
            candidate=FINAL_AI_GOLD_CANDIDATE,
            suite_id=FINAL_AI_GOLD_SUITE,
            lane_root=root / "final/ai_gold",
        )
    except FinalEvaluationError as error:
        raise FinalEvaluationV4Error(
            f"preserved final/ai_gold lane fails after exact journal removal: {error}"
        ) from error
    expected_hashes = {
        "prediction_sha256": FINAL_AI_GOLD_PREDICTION_SHA256.removeprefix("sha256:"),
        "prediction_manifest_sha256": FINAL_AI_GOLD_MANIFEST_SHA256.removeprefix("sha256:"),
        "evaluation_sha256": FINAL_AI_GOLD_EVALUATION_SHA256.removeprefix("sha256:"),
    }
    if (
        any(verified.get(key) != value for key, value in expected_hashes.items())
        or verified.get("record_count") != FINAL_AI_GOLD_RECORD_COUNT
        or verified.get("evaluation_exit") != 2
    ):
        raise FinalEvaluationV4Error("preserved final/ai_gold identity changed after exact removal")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_post_removal_proof",
        "status": "passed",
        "source": "fresh_hf_bucket_download",
        "observed_at_utc": observed_at_utc,
        "remote_mutation_performed_by_verifier": False,
        "stale_journal_absent": True,
        "removal_plan_sha256": _sha256(canonical_json_bytes(removal_plan)),
        "stale_journal_evidence_sha256": _sha256(canonical_json_bytes(evidence)),
        "snapshot": snapshot,
        "preserved_lane": verified,
        "final_ai_gold_inference_allowed": False,
        "final_ai_gold_evaluation_allowed": False,
    }


def validate_post_removal_proof(
    value: Mapping[str, object],
    *,
    evidence: Mapping[str, object],
    removal_plan: Mapping[str, object],
) -> dict[str, object]:
    """Validate the canonical proof emitted from a fresh Bucket download."""

    observed_at_utc = value.get("observed_at_utc")
    observed = _parse_utc(observed_at_utc, "post-removal proof observation")
    if observed < _parse_utc(V3_FINISHED_AT_UTC, "V3 finish"):
        raise FinalEvaluationV4Error("post-removal proof predates the V3 terminal")
    preserved = {
        "record_count": FINAL_AI_GOLD_RECORD_COUNT,
        "prediction_sha256": FINAL_AI_GOLD_PREDICTION_SHA256.removeprefix("sha256:"),
        "prediction_manifest_sha256": FINAL_AI_GOLD_MANIFEST_SHA256.removeprefix("sha256:"),
        "evaluation_sha256": FINAL_AI_GOLD_EVALUATION_SHA256.removeprefix("sha256:"),
        "evaluation_exit": 2,
        "gate_status": "failed",
        "gate_passed": False,
    }
    expected = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_post_removal_proof",
        "status": "passed",
        "source": "fresh_hf_bucket_download",
        "observed_at_utc": observed_at_utc,
        "remote_mutation_performed_by_verifier": False,
        "stale_journal_absent": True,
        "removal_plan_sha256": _sha256(canonical_json_bytes(removal_plan)),
        "stale_journal_evidence_sha256": _sha256(canonical_json_bytes(evidence)),
        "snapshot": removal_plan["expected_post_removal_snapshot"],
        "preserved_lane": preserved,
        "final_ai_gold_inference_allowed": False,
        "final_ai_gold_evaluation_allowed": False,
        "input_manifest_sha256": removal_plan.get("input_manifest_sha256"),
        "exact_removal_plan_sha256": _sha256(canonical_json_bytes(removal_plan)),
    }
    # The recovery verifier appends the input-manifest identity from its
    # canonical directory; bind the observed value instead of duplicating the
    # directory manifest inside the removal plan.
    expected["input_manifest_sha256"] = value.get("input_manifest_sha256")
    if not isinstance(expected["input_manifest_sha256"], str) or dict(value) != expected:
        raise FinalEvaluationV4Error("post-removal proof differs from the exact preserved lane")
    return expected

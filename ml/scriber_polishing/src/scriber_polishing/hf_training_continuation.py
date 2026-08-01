"""Fail-closed Hugging Face continuation from the accepted marker repair."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .curriculum import (
    PHASE_RULES_VERSION,
    PHASES,
    canonical_json_bytes,
    classify_curriculum_phase,
    hash_identifier_sequence,
    sha256_bytes,
)
from .hf_marker_repair_job import (
    HF_JOB_HOURLY_RATES_USD,
    HF_MARKER_REPAIR_IMAGE,
)
from .marker_repair import validate_marker_repair_manifest
from .marker_repair_acceptance import (
    validate_marker_repair_acceptance_report,
)
from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from .pilot_selection import validate_pilot_selection_report
from .protected_spans import validate_protection_policy_evidence
from .remediation_experiment import (
    _build_order_plan,
    validate_remediation_order_plan,
)
from .schemas import validate_ai_gold_manifest
from .tokenize import tokenize_example
from .training_integrity import (
    bind_improvement_order_plan,
    bind_parent_checkpoint,
    bind_protection_policy,
    bind_training_dataset,
    capture_tokenizer_identity,
    checkpoint_tree_sha256,
    load_training_config,
    training_code_identity_paths,
)
from .training_runtime import (
    apply_training_protection,
    build_effective_improvement_order,
    resolve_epoch_order_schedule,
)

PILOT_SELECTED_UPDATES = 700
REPAIR_UPDATES = 146
CONTINUATION_START_UPDATES = 846
CONTINUATION_ADDITIONAL_UPDATES = 254
CONTINUATION_TARGET_UPDATES = 1100
CONTINUATION_EFFECTIVE_BATCH_SIZE = 16
CONTINUATION_TRAIN_EXAMPLES = CONTINUATION_ADDITIONAL_UPDATES * CONTINUATION_EFFECTIVE_BATCH_SIZE
CONTINUATION_PHASE_TARGETS = {
    1: 813,
    2: 813,
    3: 813,
    4: 813,
    5: 812,
}
TOTAL_HF_BUDGET_USD = Decimal("5.00")
CONTINUATION_ALLOWED_FLAVORS = frozenset({"l4x1", "a10g-small"})
CONTINUATION_DEFAULT_FLAVOR = "l4x1"
CONTINUATION_TIMEOUT_MINUTES = 30
CONTINUATION_PRIOR_COST_USD = Decimal("0.893")
CONTINUATION_SEED = 270014
CONTINUATION_MAX_SEQUENCE_LENGTH = 768
CONTINUATION_OUTPUT_NAME = "scriber-1100-continuation-v1"
CONTINUATION_REMOTE_PARENT_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
    "attempt6/pilot-marker-lexical-repair-v1/training/"
    "final-3015c63021cf53d8"
)
CONTINUATION_REMOTE_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
CONTINUATION_REMOTE_OUTPUT_PATH = "attempt8/scriber-1100-continuation-v1"
HF_COST_AUDIT_JOBS: tuple[Mapping[str, object], ...] = tuple(
    MappingProxyType(job)
    for job in (
        {
            "job_id": "6a6b4faf23ed89c748ec7a25",
            "flavor": "l4x1",
            "running_seconds": 22,
            "hourly_rate_usd": "0.80",
        },
        {
            "job_id": "6a6b5087b36a6516e96a2b11",
            "flavor": "l4x1",
            "running_seconds": 51,
            "hourly_rate_usd": "0.80",
        },
        {
            "job_id": "6a6b51f3b36a6516e96a2b34",
            "flavor": "l4x1",
            "running_seconds": 52,
            "hourly_rate_usd": "0.80",
        },
        {
            "job_id": "6a6b55d323ed89c748ec7ab1",
            "flavor": "l4x1",
            "running_seconds": 63,
            "hourly_rate_usd": "0.80",
        },
        {
            "job_id": "6a6b59a023ed89c748ec7b2b",
            "flavor": "l4x1",
            "running_seconds": 96,
            "hourly_rate_usd": "0.80",
        },
        {
            "job_id": "6a6b5cc723ed89c748ec7bad",
            "flavor": "l4x1",
            "running_seconds": 567,
            "hourly_rate_usd": "0.80",
            "role": "successful_146_update_training_artifact",
        },
        {
            "job_id": "6a6b60ce23ed89c748ec7c11",
            "flavor": "l4x1",
            "running_seconds": 1245,
            "hourly_rate_usd": "0.80",
            "role": "evaluation_only_recovery_training_steps_0",
        },
        {
            "job_id": "6a6b4d3023ed89c748ec79d9",
            "flavor": "cpu-basic",
            "running_seconds": 7,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b50f7b36a6516e96a2b1b",
            "flavor": "cpu-basic",
            "running_seconds": 3,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b5258b36a6516e96a2b3d",
            "flavor": "cpu-basic",
            "running_seconds": 0,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b5284b36a6516e96a2b41",
            "flavor": "cpu-basic",
            "running_seconds": 60,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b538ab36a6516e96a2b51",
            "flavor": "cpu-basic",
            "running_seconds": 58,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b53e0b36a6516e96a2b53",
            "flavor": "cpu-basic",
            "running_seconds": 197,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b5533b36a6516e96a2b7a",
            "flavor": "cpu-basic",
            "running_seconds": 132,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b588cb36a6516e96a2ba6",
            "flavor": "cpu-basic",
            "running_seconds": 253,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b60a723ed89c748ec7c0d",
            "flavor": "cpu-basic",
            "running_seconds": 4,
            "hourly_rate_usd": "0.01",
        },
        {
            "job_id": "6a6b84adb36a6516e96a2f30",
            "flavor": "a10g-small",
            "status": "ERROR",
            "running_seconds": 49,
            "hourly_rate_usd": "1.00",
            "role": "preflight_cross_platform_path_failure_no_training",
        },
        {
            "job_id": "6a6b87c8b36a6516e96a2f66",
            "flavor": "a10g-small",
            "status": "ERROR",
            "running_seconds": 188,
            "hourly_rate_usd": "1.00",
            "role": "code_identity_dependency_missing_no_training",
        },
        {
            "job_id": "6a6b8a78b36a6516e96a2fb1",
            "flavor": "a10g-small",
            "status": "ERROR",
            "running_seconds": 912,
            "hourly_rate_usd": "1.00",
            "role": "training_complete_publish_io_failure",
        },
        {
            "job_id": "6a6b94e6b36a6516e96a307c",
            "flavor": "a10g-small",
            "status": "ERROR",
            "running_seconds": 93,
            "hourly_rate_usd": "1.00",
            "role": "recovery_eval_complete_finalization_binding_failure_training_steps_0",
        },
    )
)
HF_COST_AUDIT_NON_BILLABLE_JOBS: tuple[Mapping[str, object], ...] = (
    MappingProxyType(
        {
            "job_id": "6a6b7d4fb36a6516e96a2ec7",
            "flavor": "l4x1",
            "status": "CANCELED",
            "started": False,
            "started_at": None,
            "running_seconds": None,
            "billed_minutes": 0,
            "role": "scheduling_only_canceled_unbilled",
        }
    ),
)
_ACCEPTED_PARENT_TREE_SHA256 = "sha256:4b4b758f2fa3e00af282845c42de2887bc714c36a5c8e58e7225a1d9a7d25e06"
_ACCEPTED_PARENT_DIRECTORY_TREE_SHA256 = "sha256:aa2e12b3366363ed6efe728fff0423d2f19c1147704a1ede1f9b1c6ba65f6c28"
_ACCEPTED_CHECKPOINT_INVENTORY_SHA256 = "d6ca606fb7ed972bc3909f5722771b78c4f920f5a39e54a761e5bba7abf15d4e"
_ACCEPTANCE_REPORT_SHA256 = "3dac53b308f900706ab104264ba71b9c0e506edfa0b1242b365db1c306c03d73"
_ACCEPTED_TRAINING_REPORT_SHA256 = "f47d42b8511406e1df180463a1a97868c1920b74df09dd4455ddc28829d2fbc0"
_ACCEPTED_CLOUD_RESULT_SHA256 = "56d655b83ac174323d34c72fa1688cb7ffea6cb6644cd31c6594450da0e45987"
_PROJECT_ROOT = Path(__file__).parents[2]
_REPOSITORY_ROOT = _PROJECT_ROOT.parents[1]

_PREFIXED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINAL_DIRECTORY = re.compile(r"^final-[0-9a-f]{16}$")
_FINAL_TRAIN_SHA256 = "59e6f5ef86ca754da4b671d1b3355aaafd78b8092bae64571f544cc6c2f7606a"
_VALIDATION_SHA256 = "b4840b2d06af923af4f28643b32e5de50deef3c6995ebdcb92f89a6fd3859a86"
_ORDER_PLAN_SHA256 = "de3581bc448833f41ae3f8e8118ab40bcafb9341f7ece31ef886d81e8fcca76a"
_EFFECTIVE_ORDER_SHA256 = "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d"
_FILTERED_PLAN_EFFECTIVE_ORDER_SHA256 = "625bfc2b54d7403dd41532925795f8e081da7522a45346262a22ba2a9c2fbef3"
_POLICY_ARTIFACT_SHA256 = "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
_POLICY_SELF_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_TOKENIZER_IDENTITY_SHA256 = "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"
_CONTINUATION_ARTIFACT_NAMES = frozenset(
    {
        "acceptance-report.json",
        "accepted-cloud-result.json",
        "accepted-training-report.json",
        "ai-gold-manifest.json",
        "dataset-manifest.json",
        "final-data-manifest.json",
        "hf-cost-audit.json",
        "order-plan.json",
        "pilot-data-manifest.json",
        "pilot-selection-report.json",
        "repair-manifest.json",
        "safe-validation.jsonl",
        "selected-train.jsonl",
        "selection-evidence.json",
        "train.yaml",
        "training-policy.json",
    }
)
_PACKET_CREDENTIAL_PATTERNS = (
    re.compile(rb"hf_[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(
        rb"(?i)(?:password|secret|access[_-]?token|api[_-]?key)"
        rb"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}"
    ),
)
_FORBIDDEN_PACKET_BASENAMES = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|credentials?(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)|.*\.(?:pem|key|pfx))$"
)


class TrainingContinuationError(ValueError):
    """The continuation lineage, step count, or budget is not trustworthy."""


@dataclass(frozen=True, slots=True)
class HfTrainingContinuationPacket:
    """One immutable, model-external HF continuation packet."""

    output_dir: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _RankedOffset:
    rank: bytes
    example_id: str
    offset: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class _StableFileBinding:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


def _expected_continuation_config_values() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_kind": "improvement",
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
        "method": "full_sft",
        "seed": CONTINUATION_SEED,
        "epochs": 1,
        "train_examples": CONTINUATION_TRAIN_EXAMPLES,
        "validation_examples": 431,
        "max_sequence_length": CONTINUATION_MAX_SEQUENCE_LENGTH,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "learning_rate": 0.00001,
        "optimizer": "adamw_torch_fused",
        "eval_steps": 25,
        "save_steps": 25,
        "save_total_limit": 4,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "max_grad_norm": 1.0,
        "bf16": True,
        "gradient_checkpointing": True,
        "assistant_output_loss_only": True,
        "early_stopping": False,
        "protection_policy_path": "training-policy.json",
        "protection_policy_sha256": _POLICY_ARTIFACT_SHA256,
        "parent_checkpoint_path": "parent-checkpoint",
        "parent_checkpoint_tree_sha256": _ACCEPTED_PARENT_DIRECTORY_TREE_SHA256,
        "training_order_mode": "shuffled",
        "training_order_plan_path": "order-plan.json",
        "training_order_plan_sha256": _ORDER_PLAN_SHA256,
    }


def validate_continuation_arithmetic(
    *,
    start_updates: int,
    additional_updates: int,
    target_updates: int,
) -> dict[str, int | str]:
    """Require the one approved 846 + 254 = 1100 continuation."""

    if (
        isinstance(start_updates, bool)
        or isinstance(additional_updates, bool)
        or isinstance(target_updates, bool)
        or start_updates != CONTINUATION_START_UPDATES
        or additional_updates != CONTINUATION_ADDITIONAL_UPDATES
        or target_updates != CONTINUATION_TARGET_UPDATES
        or start_updates + additional_updates != target_updates
    ):
        raise TrainingContinuationError(
            "continuation requires exactly 846 accepted updates, 254 new updates, and cumulative target 1100"
        )
    if start_updates != PILOT_SELECTED_UPDATES + REPAIR_UPDATES:
        raise TrainingContinuationError("accepted continuation lineage does not resolve to 846 updates")
    return {
        "start_updates": start_updates,
        "additional_updates": additional_updates,
        "target_updates": target_updates,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "same_run_resume": "identity_bound_recovery_only",
    }


def _money(value: str | int | float | Decimal, label: str) -> Decimal:
    if isinstance(value, bool):
        raise TrainingContinuationError(f"{label} must be a non-negative number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise TrainingContinuationError(f"{label} must be a non-negative number") from error
    if not result.is_finite() or result < 0:
        raise TrainingContinuationError(f"{label} must be a non-negative number")
    return result


def reserve_hf_budget(
    *,
    prior_cost_usd: str | int | float | Decimal,
    flavor: str,
    timeout_minutes: int,
) -> dict[str, float | int]:
    """Reserve one hard-timeout HF job without exceeding the total $5."""

    if flavor not in HF_JOB_HOURLY_RATES_USD:
        raise TrainingContinuationError(f"unsupported bounded HF flavor: {flavor}")
    if isinstance(timeout_minutes, bool) or not isinstance(timeout_minutes, int) or timeout_minutes <= 0:
        raise TrainingContinuationError("HF timeout must be a positive integer number of minutes")
    prior = _money(prior_cost_usd, "prior HF cost")
    hourly = Decimal(str(HF_JOB_HOURLY_RATES_USD[flavor]))
    maximum_job = (hourly * Decimal(timeout_minutes) / Decimal(60)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    maximum_total = prior + maximum_job
    if maximum_total > TOTAL_HF_BUDGET_USD:
        raise TrainingContinuationError(
            f"maximum aggregate HF cost ${maximum_total:.2f} exceeds the ${TOTAL_HF_BUDGET_USD:.2f} budget"
        )
    remaining = TOTAL_HF_BUDGET_USD - maximum_total
    return {
        "total_budget_usd": float(TOTAL_HF_BUDGET_USD),
        "prior_cost_usd": float(prior),
        "hourly_rate_usd": float(hourly),
        "timeout_minutes": timeout_minutes,
        "maximum_job_cost_usd": float(maximum_job),
        "maximum_total_cost_usd": float(maximum_total),
        "remaining_after_reservation_usd": float(remaining),
    }


def _validate_continuation_flavor(value: object) -> str:
    if not isinstance(value, str) or value not in CONTINUATION_ALLOWED_FLAVORS:
        allowed = ", ".join(sorted(CONTINUATION_ALLOWED_FLAVORS))
        raise TrainingContinuationError(f"unsupported continuation HF flavor: {value!r}; expected one of {allowed}")
    return value


def _continuation_budget(flavor: object) -> dict[str, float | int]:
    selected = _validate_continuation_flavor(flavor)
    return reserve_hf_budget(
        prior_cost_usd=CONTINUATION_PRIOR_COST_USD,
        flavor=selected,
        timeout_minutes=CONTINUATION_TIMEOUT_MINUTES,
    )


def build_hf_cost_audit() -> dict[str, object]:
    """Materialize the exact prior HF Jobs ledger with conservative rounding."""

    seen: set[str] = set()
    counts = {"a10g-small": 0, "cpu-basic": 0, "l4x1": 0}
    running_seconds = {"a10g-small": 0, "cpu-basic": 0, "l4x1": 0}
    billed_minutes = {"a10g-small": 0, "cpu-basic": 0, "l4x1": 0}
    calculated = Decimal("0")
    jobs: list[dict[str, object]] = []
    for raw in HF_COST_AUDIT_JOBS:
        job_id = raw.get("job_id")
        flavor = raw.get("flavor")
        seconds = raw.get("running_seconds")
        rate = raw.get("hourly_rate_usd")
        if (
            not isinstance(job_id, str)
            or not re.fullmatch(r"[0-9a-f]{24}", job_id)
            or job_id in seen
            or flavor not in counts
            or isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or seconds < 0
        ):
            raise TrainingContinuationError("prior HF cost ledger contains an invalid job binding")
        expected_rate = {
            "a10g-small": "1.00",
            "cpu-basic": "0.01",
            "l4x1": "0.80",
        }[str(flavor)]
        if rate != expected_rate:
            raise TrainingContinuationError("prior HF cost ledger contains an unexpected hourly rate")
        seen.add(job_id)
        counts[str(flavor)] += 1
        running_seconds[str(flavor)] += seconds
        job_billed_minutes = max(1, (seconds + 59) // 60)
        billed_minutes[str(flavor)] += job_billed_minutes
        cost = Decimal(str(rate)) * Decimal(job_billed_minutes) / Decimal(60)
        calculated += cost
        jobs.append(
            {
                "job_id": job_id,
                "flavor": flavor,
                "running_seconds": seconds,
                "billed_minutes": job_billed_minutes,
                "hourly_rate_usd": rate,
                **({"status": raw["status"]} if isinstance(raw.get("status"), str) else {}),
                **({"role": raw["role"]} if isinstance(raw.get("role"), str) else {}),
            }
        )
    if (
        counts != {"a10g-small": 4, "cpu-basic": 9, "l4x1": 7}
        or running_seconds != {"a10g-small": 1242, "cpu-basic": 714, "l4x1": 2096}
        or billed_minutes != {"a10g-small": 23, "cpu-basic": 18, "l4x1": 38}
    ):
        raise TrainingContinuationError("prior HF cost ledger differs from the reviewed 20-job billed audit")
    non_billable_jobs = [dict(job) for job in HF_COST_AUDIT_NON_BILLABLE_JOBS]
    expected_non_billable_jobs = [
        {
            "job_id": "6a6b7d4fb36a6516e96a2ec7",
            "flavor": "l4x1",
            "status": "CANCELED",
            "started": False,
            "started_at": None,
            "running_seconds": None,
            "billed_minutes": 0,
            "role": "scheduling_only_canceled_unbilled",
        }
    ]
    if non_billable_jobs != expected_non_billable_jobs:
        raise TrainingContinuationError("prior HF non-billable ledger differs from the canceled scheduling-only job")
    non_billable_job_id = str(non_billable_jobs[0]["job_id"])
    if not re.fullmatch(r"[0-9a-f]{24}", non_billable_job_id) or non_billable_job_id in seen:
        raise TrainingContinuationError("prior HF non-billable ledger duplicates an invalid job binding")
    calculated_rounded = calculated.quantize(
        Decimal("0.000001"),
        rounding=ROUND_HALF_UP,
    )
    conservative = calculated_rounded.quantize(
        Decimal("0.001"),
        rounding="ROUND_CEILING",
    )
    if conservative != CONTINUATION_PRIOR_COST_USD:
        raise TrainingContinuationError("prior HF conservative budget reserve differs from $0.893")
    prior_train_runtime = Decimal("400.814")
    projected_train_runtime = prior_train_runtime * Decimal(CONTINUATION_ADDITIONAL_UPDATES) / Decimal(REPAIR_UPDATES)
    observed_nontrain_overhead = Decimal(567) - prior_train_runtime
    projected_total = projected_train_runtime + observed_nontrain_overhead
    timeout_seconds = Decimal(30 * 60)
    projected_margin = timeout_seconds - projected_total
    return {
        "schema_version": 1,
        "kind": "scriber_hf_cost_audit",
        "source": "hf_jobs_running_seconds_reviewed_2026-07-30",
        "jobs": jobs,
        "non_billable_jobs": non_billable_jobs,
        "counts": counts,
        "running_seconds": running_seconds,
        "billed_minutes": billed_minutes,
        "billing_increment": "per_started_minute",
        "zero_second_job_minimum_billed_minutes": 1,
        "non_billable_policy": (
            "scheduling-only jobs with started_at=null and running_seconds=null are billed zero minutes"
        ),
        "calculation": ("sum(hourly_rate_usd * max(1, ceil(running_seconds / 60)) / 60)"),
        "calculated_cost_usd": format(calculated_rounded, "f"),
        "conservative_rounding": "ceiling_to_0.001_usd",
        "conservative_prior_cost_usd": format(conservative, "f"),
        "runtime_projection": {
            "method": "linear_train_runtime_plus_observed_successful_job_overhead",
            "training_artifact_job_id": "6a6b5cc723ed89c748ec7bad",
            "training_artifact_job_running_seconds": 567,
            "training_artifact_updates": REPAIR_UPDATES,
            "training_report_train_runtime_seconds": format(prior_train_runtime, "f"),
            "evaluation_only_job_id": "6a6b60ce23ed89c748ec7c11",
            "evaluation_only_job_running_seconds": 1245,
            "evaluation_only_job_training_steps": 0,
            "projected_continuation_updates": CONTINUATION_ADDITIONAL_UPDATES,
            "projected_continuation_train_seconds": format(
                projected_train_runtime.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                ),
                "f",
            ),
            "observed_nontrain_overhead_seconds": format(observed_nontrain_overhead, "f"),
            "projected_continuation_total_seconds": format(
                projected_total.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                ),
                "f",
            ),
            "timeout_seconds": int(timeout_seconds),
            "projected_timeout_margin_seconds": format(
                projected_margin.quantize(
                    Decimal("0.001"),
                    rounding=ROUND_HALF_UP,
                ),
                "f",
            ),
        },
        "secret_fields_present": False,
    }


def build_hf_job_launch_contract(
    packet_dir: str | Path,
    *,
    expected_flavor: str = CONTINUATION_DEFAULT_FLAVOR,
) -> dict[str, object]:
    """Build the exact three-volume HF CLI contract without launching it."""

    selected_flavor = _validate_continuation_flavor(expected_flavor)
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"HF continuation packet must not be a symlink: {candidate}")
    packet = candidate.resolve()
    if not packet.is_dir():
        raise TrainingContinuationError(f"HF continuation packet must be a directory: {packet}")
    _, _, job_manifest = _stable_json_object(
        packet / "job-manifest.json",
        "HF continuation job manifest",
    )
    expected_packet_tree_sha256 = job_manifest.get("packet_tree_sha256")
    if not isinstance(expected_packet_tree_sha256, str):
        raise TrainingContinuationError("HF continuation job manifest lacks its packet tree SHA-256")
    verify_hf_training_continuation_packet(
        packet,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
        expected_flavor=selected_flavor,
    )
    packet_volume = f"{packet}:/input/repo:ro"
    parent_volume = f"{CONTINUATION_REMOTE_PARENT_URI}:/accepted-parent:ro"
    output_volume = f"{CONTINUATION_REMOTE_OUTPUT_BUCKET}:/outputs:rw"
    output_root = f"/outputs/{CONTINUATION_REMOTE_OUTPUT_PATH}"
    remote_result_uri = CONTINUATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + CONTINUATION_REMOTE_OUTPUT_PATH
    if "attempt8/attempt8" in output_root or "attempt8/attempt8" in remote_result_uri:
        raise TrainingContinuationError("HF continuation output path contains a duplicated attempt prefix")
    argv = [
        "hf",
        "jobs",
        "run",
        "--flavor",
        selected_flavor,
        "--timeout",
        f"{CONTINUATION_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=continuation-1100",
        "-v",
        packet_volume,
        "-v",
        parent_volume,
        "-v",
        output_volume,
        HF_MARKER_REPAIR_IMAGE,
        "bash",
        "/input/repo/run-training-continuation.sh",
        "--expected-packet-tree-sha256",
        expected_packet_tree_sha256,
        "--expected-flavor",
        selected_flavor,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_launch_contract",
        "flavor": selected_flavor,
        "timeout": f"{CONTINUATION_TIMEOUT_MINUTES}m",
        "packet_volume": packet_volume,
        "parent_volume": parent_volume,
        "output_volume": output_volume,
        "output_root": output_root,
        "remote_result_uri": remote_result_uri,
        "expected_packet_tree_sha256": expected_packet_tree_sha256,
        "argv": argv,
        "external_job_started": False,
    }


def continuation_runner_payload() -> bytes:
    """Return the offline, model-external, bounded continuation runner."""

    script = r"""#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

if [ "$#" -ne 4 ] || \
   [ "$1" != "--expected-packet-tree-sha256" ] || \
   [ "$3" != "--expected-flavor" ]; then
  echo "expected packet tree SHA-256 argument is required; expected flavor argument is required" >&2
  exit 1
fi
EXPECTED_PACKET_TREE_SHA256="$2"
if [[ ! "$EXPECTED_PACKET_TREE_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "expected packet tree SHA-256 argument is invalid" >&2
  exit 1
fi
EXPECTED_FLAVOR="$4"
if [ "$EXPECTED_FLAVOR" != "l4x1" ] && [ "$EXPECTED_FLAVOR" != "a10g-small" ]; then
  echo "expected continuation flavor is invalid" >&2
  exit 1
fi

PACKET_ROOT=/input/repo
SOURCE_REPO="$PACKET_ROOT/repo"
REMOTE_PARENT_ROOT=/accepted-parent
WORKSPACE_ROOT=/workspace/scriber-1100-continuation
PACKET_VERIFICATION_REPORT=/workspace/scriber-1100-packet-verification.json
WORK_ROOT="$WORKSPACE_ROOT/repo"
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
LOCAL_PARENT_ROOT="$PROJECT_ROOT/job-input/parent-checkpoint"
OUTPUT_ROOT=/outputs/attempt8/scriber-1100-continuation-v1

if [ ! -d "$SOURCE_REPO" ]; then
  echo "immutable packet repository is missing" >&2
  exit 1
fi
if [ ! -d "$REMOTE_PARENT_ROOT" ]; then
  echo "read-only accepted-parent mount is missing" >&2
  exit 1
fi
if [ -e "$WORKSPACE_ROOT" ]; then
  echo "refusing a non-empty continuation workspace" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends git
fi

python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 \
  jsonschema==4.26.0 \
  numpy==2.5.1 \
  pyyaml==6.0.3 \
  safetensors==0.8.0 \
  sentencepiece==0.2.2 \
  tensorboard==2.21.0 \
  transformers==4.57.6

python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_training_continuation.py" \
  --mode packet \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256" \
  --expected-flavor "$EXPECTED_FLAVOR" \
  --report "$PACKET_VERIFICATION_REPORT"

mkdir -p "$WORKSPACE_ROOT"
cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_CEILING_DIRECTORIES
git -C "$WORK_ROOT" init --quiet
git -C "$WORK_ROOT" config --local user.name "Scriber HF Job"
git -C "$WORK_ROOT" config --local user.email "scriber-hf-job@invalid.local"
git -C "$WORK_ROOT" add \
  "ml/scriber_polishing/src" \
  "ml/scriber_polishing/scripts" \
  "ml/scriber_polishing/contracts" \
  "ml/scriber_polishing/configs" \
  "ml/scriber_polishing/README.md" \
  "ml/scriber_polishing/pyproject.toml"
export GIT_AUTHOR_DATE=2026-07-30T00:00:00Z
export GIT_COMMITTER_DATE=2026-07-30T00:00:00Z
git -C "$WORK_ROOT" commit --quiet \
  -m "chore(ml): materialize immutable 1100-step continuation"

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"

python scripts/verify_hf_training_continuation.py \
  --mode preflight-remote \
  --bundle-dir job-input \
  --parent-model "$REMOTE_PARENT_ROOT" \
  --report "$WORKSPACE_ROOT/preflight-remote.json"

mkdir -p "$(dirname "$LOCAL_PARENT_ROOT")"
cp -a --no-preserve=ownership "$REMOTE_PARENT_ROOT" "$LOCAL_PARENT_ROOT"

python scripts/verify_hf_training_continuation.py \
  --mode preflight-local \
  --bundle-dir job-input \
  --parent-model "$LOCAL_PARENT_ROOT" \
  --report "$WORKSPACE_ROOT/preflight-local.json"

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

OWNER_ID="$(python - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
CONTINUATION_MANIFEST_SHA256="$(python - "$PACKET_VERIFICATION_REPORT" <<'PY'
import json
import pathlib
import sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(value["continuation_manifest_sha256"])
PY
)"
python scripts/verify_hf_training_continuation.py \
  --mode claim-output \
  --output-root "$OUTPUT_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256" \
  --continuation-manifest-sha256 "$CONTINUATION_MANIFEST_SHA256" \
  --owner-id "$OWNER_ID" \
  --report "$WORKSPACE_ROOT/output-claim.json"

cp "$WORKSPACE_ROOT/preflight-remote.json" "$OUTPUT_ROOT/"
cp "$WORKSPACE_ROOT/preflight-local.json" "$OUTPUT_ROOT/"
cp "$PACKET_VERIFICATION_REPORT" "$OUTPUT_ROOT/"
cp "$WORKSPACE_ROOT/output-claim.json" "$OUTPUT_ROOT/"
cp job-input/continuation-manifest.json "$OUTPUT_ROOT/"
cp job-input/hf-cost-audit.json "$OUTPUT_ROOT/"

python scripts/train_sft.py \
  --config job-input/train.yaml \
  --dataset-manifest job-input/dataset-manifest.json \
  --train job-input/selected-train.jsonl \
  --validation job-input/safe-validation.jsonl \
  --output-dir "$OUTPUT_ROOT/training" \
  --report "$OUTPUT_ROOT/training-report.json" \
  --resume none \
  --save-final

python scripts/verify_hf_training_continuation.py \
  --mode postflight \
  --bundle-dir job-input \
  --parent-model "$LOCAL_PARENT_ROOT" \
  --training-report "$OUTPUT_ROOT/training-report.json" \
  --output-root "$OUTPUT_ROOT" \
  --expected-flavor "$EXPECTED_FLAVOR" \
  --report "$OUTPUT_ROOT/continuation-result.json"

echo "cloud_training_continuation=complete" >&2
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def _selection_rank(seed: int, phase: int, example_id: str) -> bytes:
    return hashlib.sha256(f"continuation-selection-v1:{seed}:{phase}:{example_id}".encode()).digest()


def _checked_selection_row(
    value: Mapping[str, object],
    *,
    phase: int,
    source: str,
) -> dict[str, object]:
    example_id = value.get("example_id")
    if not isinstance(example_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,299}", example_id):
        raise TrainingContinuationError(f"{source} continuation row requires a valid example_id")
    if value.get("split") != "train":
        label = "AI-Gold" if source == "ai_gold_train" else "final-data"
        raise TrainingContinuationError(f"{label} continuation input may contain only split=train")
    if value.get("_continuation_phase") != phase:
        raise TrainingContinuationError(f"{source} continuation phase binding is inconsistent")
    if value.get("_continuation_source") != source:
        raise TrainingContinuationError(f"{source} continuation source binding is inconsistent")
    return dict(value)


def select_continuation_mix(
    *,
    eligible_ai_gold_records: Sequence[Mapping[str, object]],
    eligible_final_records_by_phase: Mapping[int, Sequence[Mapping[str, object]]],
    pilot_example_ids: frozenset[str],
    seed: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Select the exact mixed, shuffled 4,064-case continuation cohort."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise TrainingContinuationError("continuation selection seed must be a non-negative integer")
    if any(not isinstance(example_id, str) or not example_id for example_id in pilot_example_ids):
        raise TrainingContinuationError("pilot exclusion IDs must be non-empty strings")
    ai_rows: list[dict[str, object]] = []
    seen_ai: set[str] = set()
    for raw in eligible_ai_gold_records:
        row = _checked_selection_row(
            raw,
            phase=5,
            source="ai_gold_train",
        )
        example_id = str(row["example_id"])
        if example_id in seen_ai:
            raise TrainingContinuationError(f"duplicate AI-Gold continuation example_id: {example_id}")
        seen_ai.add(example_id)
        if example_id not in pilot_example_ids:
            ai_rows.append(row)
    if not ai_rows:
        raise TrainingContinuationError("no policy-safe, non-pilot AI-Gold train examples remain")
    ai_rows.sort(
        key=lambda row: (
            _selection_rank(seed, 5, str(row["example_id"])),
            str(row["example_id"]),
        )
    )
    selected_ai = ai_rows[: CONTINUATION_PHASE_TARGETS[5]]
    selected: list[dict[str, object]] = list(selected_ai)
    selected_ids = {str(row["example_id"]) for row in selected_ai}

    for phase in PHASES:
        target = CONTINUATION_PHASE_TARGETS[phase]
        already = len(selected_ai) if phase == 5 else 0
        needed = target - already
        if needed < 0:
            raise TrainingContinuationError("eligible AI-Gold train exceeds the phase-5 continuation target")
        candidates: list[dict[str, object]] = []
        seen_phase: set[str] = set()
        for raw in eligible_final_records_by_phase.get(phase, ()):
            row = _checked_selection_row(
                raw,
                phase=phase,
                source="final_data_train",
            )
            example_id = str(row["example_id"])
            if example_id in seen_phase:
                raise TrainingContinuationError(f"duplicate final-data continuation example_id: {example_id}")
            seen_phase.add(example_id)
            if example_id in pilot_example_ids or example_id in seen_ai or example_id in selected_ids:
                continue
            candidates.append(row)
        candidates.sort(
            key=lambda row: (
                _selection_rank(seed, phase, str(row["example_id"])),
                str(row["example_id"]),
            )
        )
        if len(candidates) < needed:
            raise TrainingContinuationError(
                f"continuation phase {phase} has only {len(candidates)} eligible final-data rows, needs {needed}"
            )
        chosen = candidates[:needed]
        selected.extend(chosen)
        selected_ids.update(str(row["example_id"]) for row in chosen)

    selected.sort(key=lambda row: str(row["example_id"]))
    if (
        len(selected) != CONTINUATION_TRAIN_EXAMPLES
        or len(selected_ids) != CONTINUATION_TRAIN_EXAMPLES
        or selected_ids & pilot_example_ids
    ):
        raise TrainingContinuationError("continuation mix is not an exact unique pilot-free cohort")
    phase_counts = {str(phase): sum(row["_continuation_phase"] == phase for row in selected) for phase in PHASES}
    if phase_counts != {str(phase): count for phase, count in CONTINUATION_PHASE_TARGETS.items()}:
        raise TrainingContinuationError("continuation mix differs from the fixed phase targets")
    ai_ids = sorted(str(row["example_id"]) for row in selected if row["_continuation_source"] == "ai_gold_train")
    final_ids = sorted(selected_ids - set(ai_ids))
    evidence: dict[str, object] = {
        "algorithm": "pilot_excluded_ai_gold_first_phase_balanced_sha256_v1",
        "seed": seed,
        "record_count": len(selected),
        "ai_gold_train_selected": len(ai_ids),
        "ai_gold_train_example_ids_sha256": hash_identifier_sequence(ai_ids),
        "final_data_train_selected": len(final_ids),
        "final_data_train_example_ids_sha256": hash_identifier_sequence(final_ids),
        "selected_example_ids_sha256": hash_identifier_sequence(sorted(selected_ids)),
        "pilot_overlap_count": 0,
        "test_inputs": False,
        "challenge_inputs": False,
        "phase_counts": phase_counts,
    }
    return selected, evidence


def _decode_training_jsonl_row(
    raw_line: bytes,
    *,
    label: str,
    line_number: int,
) -> dict[str, Any]:
    if not raw_line or raw_line in {b"\n", b"\r\n"}:
        raise TrainingContinuationError(f"blank {label} JSONL line at {line_number}")
    try:
        value = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingContinuationError(f"invalid {label} JSON at line {line_number}: {error}") from error
    if not isinstance(value, dict):
        raise TrainingContinuationError(f"{label} row {line_number} must be an object")
    example_id = value.get("example_id")
    source_text = value.get("source_text")
    target_sst = value.get("target_sst")
    provenance = value.get("provenance")
    if (
        not isinstance(example_id, str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{2,299}",
            example_id,
        )
        or not isinstance(source_text, str)
        or not source_text
        or not isinstance(target_sst, str)
        or not target_sst
        or value.get("split") != "train"
        or not isinstance(provenance, Mapping)
        or provenance.get("synthetic_only") is not True
        or provenance.get("human_curation") is not False
        or provenance.get("generative_api") is not False
    ):
        raise TrainingContinuationError(f"{label} row {line_number} is not an AI-only train pair")
    return value


def _load_training_jsonl(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_count: int,
    label: str,
) -> tuple[list[dict[str, Any]], str]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingContinuationError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with resolved.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                digest.update(raw_line)
                row = _decode_training_jsonl_row(
                    raw_line,
                    label=label,
                    line_number=line_number,
                )
                example_id = str(row["example_id"])
                if example_id in seen:
                    raise TrainingContinuationError(f"duplicate {label} example_id: {example_id}")
                seen.add(example_id)
                records.append(row)
    except OSError as error:
        raise TrainingContinuationError(f"cannot stream {label}: {error}") from error
    after = resolved.stat()
    actual_sha256 = digest.hexdigest()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise TrainingContinuationError(f"{label} changed while it was read")
    if actual_sha256 != expected_sha256 or len(records) != expected_count:
        raise TrainingContinuationError(f"{label} differs from its manifest count or SHA-256")
    return records, actual_sha256


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _physical_file_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
    )


def _stable_file_binding(
    path: str | Path,
    *,
    label: str,
) -> _StableFileBinding:
    """Hash a regular file while binding its physical identity on both sides."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingContinuationError(f"{label} must be a regular file: {resolved}")
    digest = hashlib.sha256()
    try:
        before_path = resolved.stat()
        with resolved.open("rb") as stream:
            before_open = os.fstat(stream.fileno())
            if _physical_file_identity(before_path) != _physical_file_identity(before_open):
                raise TrainingContinuationError(f"{label} changed before its full hash was established")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after_open = os.fstat(stream.fileno())
        after_path = resolved.stat()
    except OSError as error:
        raise TrainingContinuationError(f"cannot hash {label}: {error}") from error
    identity = _stat_identity(before_path)
    if (
        identity != _stat_identity(after_path)
        or _physical_file_identity(before_open) != _physical_file_identity(after_open)
        or _physical_file_identity(before_path) != _physical_file_identity(before_open)
    ):
        raise TrainingContinuationError(f"{label} changed while its full hash was established")
    return _StableFileBinding(
        device=identity[0],
        inode=identity[1],
        size=identity[2],
        mtime_ns=identity[3],
        ctime_ns=identity[4],
        sha256=digest.hexdigest(),
    )


def _verify_full_file_binding(
    path: str | Path,
    expected: _StableFileBinding,
    *,
    label: str,
) -> None:
    """Perform the required full second hash, including inode/device binding."""

    actual = _stable_file_binding(path, label=label)
    if actual != expected:
        raise TrainingContinuationError(
            f"{label} full second hash or physical file identity differs from the first scan"
        )


def _scan_ranked_final_train(
    path: str | Path,
    *,
    expected_sha256: str,
    expected_count: int,
    excluded_ids: frozenset[str],
    seed: int,
) -> tuple[
    Path,
    dict[int, list[_RankedOffset]],
    dict[str, object],
    _StableFileBinding,
]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"final-data train must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingContinuationError(f"final-data train must be a regular file: {resolved}")
    before = resolved.stat()
    digest = hashlib.sha256()
    groups = {phase: [] for phase in PHASES}
    seen: set[str] = set()
    excluded_count = 0
    record_count = 0
    try:
        with resolved.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw_line = stream.readline()
                if not raw_line:
                    break
                record_count += 1
                digest.update(raw_line)
                row = _decode_training_jsonl_row(
                    raw_line,
                    label="final-data train",
                    line_number=record_count,
                )
                example_id = str(row["example_id"])
                if example_id in seen:
                    raise TrainingContinuationError(f"duplicate final-data train example_id: {example_id}")
                seen.add(example_id)
                if example_id in excluded_ids:
                    excluded_count += 1
                    continue
                try:
                    phase = classify_curriculum_phase(
                        row,
                        ai_gold_ids=frozenset(),
                    )
                except ValueError as error:
                    raise TrainingContinuationError(f"cannot classify final-data row {example_id}: {error}") from error
                groups[phase].append(
                    _RankedOffset(
                        rank=_selection_rank(seed, phase, example_id),
                        example_id=example_id,
                        offset=offset,
                        byte_count=len(raw_line),
                    )
                )
    except OSError as error:
        raise TrainingContinuationError(f"cannot scan final-data train: {error}") from error
    after = resolved.stat()
    actual_sha256 = digest.hexdigest()
    if _stat_identity(before) != _stat_identity(after):
        raise TrainingContinuationError("final-data train changed while it was scanned")
    if actual_sha256 != expected_sha256 or record_count != expected_count:
        raise TrainingContinuationError("final-data train differs from its manifest count or SHA-256")
    identity = _stat_identity(before)
    first_binding = _StableFileBinding(
        device=identity[0],
        inode=identity[1],
        size=identity[2],
        mtime_ns=identity[3],
        ctime_ns=identity[4],
        sha256=actual_sha256,
    )
    for rows in groups.values():
        rows.sort(key=lambda item: (item.rank, item.example_id))
    return (
        resolved,
        groups,
        {
            "record_count": record_count,
            "sha256": actual_sha256,
            "excluded_pilot_or_ai_gold_count": excluded_count,
            "eligible_phase_counts": {str(phase): len(groups[phase]) for phase in PHASES},
        },
        first_binding,
    )


def _read_ranked_rows(
    stream: Any,
    ranked: Sequence[_RankedOffset],
    *,
    start: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(
        ranked[start : start + limit],
        start=start,
    ):
        stream.seek(item.offset)
        raw_line = stream.read(item.byte_count)
        row = _decode_training_jsonl_row(
            raw_line,
            label="ranked final-data train",
            line_number=position + 1,
        )
        if row.get("example_id") != item.example_id:
            raise TrainingContinuationError("ranked final-data offset no longer resolves to its example")
        rows.append(row)
    return rows


def _token_length(
    tokenizer: Any,
    record: Mapping[str, Any],
) -> int:
    try:
        encoded = tokenize_example(
            tokenizer,
            transcript=str(record["source_text"]),
            completion=str(record["target_sst"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TrainingContinuationError(
            f"cannot tokenize continuation row {record.get('example_id')}: {error}"
        ) from error
    return len(encoded["input_ids"])


def prepare_continuation_training_mix(
    *,
    tokenizer: Any,
    training_policy: Mapping[str, object],
    ai_gold_manifest_path: str | Path,
    ai_gold_train_path: str | Path,
    final_manifest_path: str | Path,
    final_train_path: str | Path,
    pilot_manifest_path: str | Path,
    pilot_train_path: str | Path,
    seed: int = CONTINUATION_SEED,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Audit and select the exact 4,064-row train-only continuation mix."""

    try:
        policy = validate_protection_policy_evidence(training_policy)
    except ValueError as error:
        raise TrainingContinuationError(f"continuation training policy is invalid: {error}") from error
    if policy.get("policy_id") != "gemma_lexical_v1":
        raise TrainingContinuationError("continuation requires the accepted gemma_lexical_v1 policy")

    _, ai_manifest_payload, ai_manifest_raw = _stable_json_object(
        ai_gold_manifest_path,
        "AI-Gold manifest",
    )
    try:
        ai_manifest = validate_ai_gold_manifest(ai_manifest_raw)
    except ValueError as error:
        raise TrainingContinuationError(f"AI-Gold manifest is invalid: {error}") from error
    isolation = _mapping(
        ai_manifest.get("isolation"),
        "AI-Gold isolation",
    )
    ai_train_binding = _mapping(
        _mapping(ai_manifest.get("splits"), "AI-Gold splits").get("train"),
        "AI-Gold train binding",
    )
    if (
        ai_manifest.get("split_counts", {}).get("train") != 1500
        or ai_train_binding.get("count") != 1500
        or isolation.get("test_and_challenge_excluded_from_training") is not True
        or isolation.get("parent_split_leakage_count") != 0
    ):
        raise TrainingContinuationError("AI-Gold manifest does not prove isolated 1,500-row train data")

    _, final_manifest_payload, final_manifest = _stable_json_object(
        final_manifest_path,
        "final-data manifest",
    )
    _, pilot_manifest_payload, pilot_manifest = _stable_json_object(
        pilot_manifest_path,
        "pilot-data manifest",
    )
    for label, manifest, expected_count in (
        ("final-data", final_manifest, 120000),
        ("pilot-data", pilot_manifest, 7000),
    ):
        if (
            manifest.get("synthetic_only") is not True
            or manifest.get("human_curation") is not False
            or manifest.get("generative_api") is not False
            or manifest.get("split_leakage") is not False
            or _mapping(
                manifest.get("split_counts"),
                f"{label} split counts",
            ).get("train")
            != expected_count
        ):
            raise TrainingContinuationError(f"{label} manifest is not an isolated synthetic train source")

    pilot_hash = str(
        _mapping(
            pilot_manifest.get("split_sha256"),
            "pilot-data split hashes",
        ).get("train")
    )
    final_hash = str(
        _mapping(
            final_manifest.get("split_sha256"),
            "final-data split hashes",
        ).get("train")
    )
    ai_hash = str(ai_train_binding.get("records_sha256"))
    for value, label in (
        (pilot_hash, "pilot train SHA-256"),
        (final_hash, "final-data train SHA-256"),
        (ai_hash, "AI-Gold train SHA-256"),
    ):
        if not _RAW_SHA256.fullmatch(value):
            raise TrainingContinuationError(f"{label} is invalid")

    pilot_records, _ = _load_training_jsonl(
        pilot_train_path,
        expected_sha256=pilot_hash,
        expected_count=7000,
        label="pilot train",
    )
    pilot_ids = frozenset(str(row["example_id"]) for row in pilot_records)
    ai_records, _ = _load_training_jsonl(
        ai_gold_train_path,
        expected_sha256=ai_hash,
        expected_count=1500,
        label="AI-Gold train",
    )
    ai_ids = frozenset(str(row["example_id"]) for row in ai_records)
    ai_pilot_overlap = ai_ids & pilot_ids
    nonpilot_ai = [row for row in ai_records if row["example_id"] not in pilot_ids]
    try:
        protected_ai = apply_training_protection(
            nonpilot_ai,
            policy=policy,
        )
    except ValueError as error:
        raise TrainingContinuationError(f"cannot audit AI-Gold train protection: {error}") from error
    ai_by_id = {str(row["example_id"]): row for row in nonpilot_ai}
    safe_ai: list[dict[str, Any]] = []
    ai_oversize: list[str] = []
    for transformed in protected_ai.records:
        example_id = str(transformed["example_id"])
        if _token_length(tokenizer, transformed) > CONTINUATION_MAX_SEQUENCE_LENGTH:
            ai_oversize.append(example_id)
        else:
            original = dict(ai_by_id[example_id])
            original["_continuation_phase"] = 5
            original["_continuation_source"] = "ai_gold_train"
            safe_ai.append(original)
    if (
        len(ai_pilot_overlap) != 97
        or len(nonpilot_ai) != 1403
        or len(protected_ai.records) != 767
        or len(protected_ai.rejected_example_ids) != 636
        or ai_oversize
        or len(safe_ai) != 767
    ):
        raise TrainingContinuationError(
            "AI-Gold continuation audit differs from the reviewed 97-overlap/767-safe cohort"
        )

    excluded_final_ids = frozenset(pilot_ids | ai_ids)
    final_path, ranked_groups, final_scan, first_final_binding = _scan_ranked_final_train(
        final_train_path,
        expected_sha256=final_hash,
        expected_count=120000,
        excluded_ids=excluded_final_ids,
        seed=seed,
    )
    final_by_phase: dict[int, list[dict[str, Any]]] = {phase: [] for phase in PHASES}
    final_filter_evidence: dict[str, object] = {}
    before = final_path.stat()
    with final_path.open("rb") as stream:
        for phase in PHASES:
            needed = CONTINUATION_PHASE_TARGETS[phase]
            if phase == 5:
                needed -= len(safe_ai)
            scanned = 0
            policy_rejected = 0
            oversize = 0
            ranked = ranked_groups[phase]
            while len(final_by_phase[phase]) < needed and scanned < len(ranked):
                rows = _read_ranked_rows(
                    stream,
                    ranked,
                    start=scanned,
                    limit=128,
                )
                scanned += len(rows)
                try:
                    protected = apply_training_protection(
                        rows,
                        policy=policy,
                    )
                except ValueError as error:
                    if str(error) != ("protection policy rejected every training record"):
                        raise TrainingContinuationError(
                            f"cannot audit ranked final-data protection: {error}"
                        ) from error
                    policy_rejected += len(rows)
                    continue
                policy_rejected += len(protected.rejected_example_ids)
                originals = {str(row["example_id"]): row for row in rows}
                for transformed in protected.records:
                    if len(final_by_phase[phase]) >= needed:
                        break
                    if _token_length(tokenizer, transformed) > CONTINUATION_MAX_SEQUENCE_LENGTH:
                        oversize += 1
                        continue
                    example_id = str(transformed["example_id"])
                    original = dict(originals[example_id])
                    original["_continuation_phase"] = phase
                    original["_continuation_source"] = "final_data_train"
                    final_by_phase[phase].append(original)
            if len(final_by_phase[phase]) != needed:
                raise TrainingContinuationError(
                    f"final-data phase {phase} produced {len(final_by_phase[phase])} safe rows, needs {needed}"
                )
            final_filter_evidence[str(phase)] = {
                "ranked_records_scanned": scanned,
                "policy_rejected": policy_rejected,
                "sequence_rejected": oversize,
                "selected": len(final_by_phase[phase]),
            }
    after = final_path.stat()
    if _stat_identity(before) != _stat_identity(after):
        raise TrainingContinuationError("final-data train changed while ranked rows were reread")
    _verify_full_file_binding(
        final_path,
        first_final_binding,
        label="final-data train",
    )

    selected, mix_evidence = select_continuation_mix(
        eligible_ai_gold_records=safe_ai,
        eligible_final_records_by_phase=final_by_phase,
        pilot_example_ids=pilot_ids,
        seed=seed,
    )
    assignments = {str(row["example_id"]): int(row["_continuation_phase"]) for row in selected}
    training_rows = [
        {
            "example_id": str(row["example_id"]),
            "source_text": str(row["source_text"]),
            "target_sst": str(row["target_sst"]),
        }
        for row in selected
    ]
    evidence: dict[str, object] = {
        "schema_version": 1,
        "kind": "scriber_training_continuation_mix",
        "phase_rules_version": PHASE_RULES_VERSION,
        "selection": mix_evidence,
        "assignments": assignments,
        "ai_gold": {
            "manifest_sha256": "sha256:" + sha256_bytes(ai_manifest_payload),
            "train_sha256": ai_hash,
            "train_record_count": 1500,
            "pilot_overlap_count": 97,
            "nonpilot_record_count": 1403,
            "policy_accepted_count": 767,
            "policy_rejected_count": 636,
            "sequence_rejected_count": 0,
            "selected_count": 767,
            "test_opened": False,
            "challenge_opened": False,
        },
        "pilot_exclusion": {
            "manifest_sha256": "sha256:" + sha256_bytes(pilot_manifest_payload),
            "train_sha256": pilot_hash,
            "excluded_id_count": 7000,
        },
        "final_data": {
            "manifest_sha256": "sha256:" + sha256_bytes(final_manifest_payload),
            "scan": final_scan,
            "filter_by_phase": final_filter_evidence,
            "selected_count": 3297,
        },
        "holdout_inputs": False,
        "test_inputs": False,
        "challenge_inputs": False,
    }
    return training_rows, evidence


def _pair_jsonl_payload(
    records: Sequence[Mapping[str, str]],
) -> bytes:
    return b"".join(canonical_json_bytes(dict(record)) for record in records)


def _stable_bytes(
    path: str | Path,
    label: str,
) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingContinuationError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise TrainingContinuationError(f"cannot read {label}: {error}") from error
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
        raise TrainingContinuationError(f"{label} changed while it was read")
    return resolved, payload


def _copy_project_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise TrainingContinuationError(f"continuation packet source is missing: {source}")
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise TrainingContinuationError(f"continuation packet source may not contain a symlink: {path}")
    shutil.copytree(
        source,
        destination,
        copy_function=shutil.copy2,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def _copy_training_code_identity_files(
    source_project: str | Path,
    destination_project: str | Path,
) -> tuple[Path, ...]:
    """Copy every file the training entrypoint will bind before model loading."""

    source_root = Path(source_project).expanduser().resolve()
    destination_root = Path(destination_project).expanduser().resolve()
    copied: list[Path] = []
    for source in training_code_identity_paths(
        source_root,
        source_root / "scripts" / "train_sft.py",
    ):
        resolved, _ = _stable_bytes(
            source,
            "training code identity dependency",
        )
        try:
            relative = resolved.relative_to(source_root)
        except ValueError as error:
            raise TrainingContinuationError(
                f"training code identity dependency escapes the project root: {resolved}"
            ) from error
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination)
        copied.append(destination)
    return tuple(copied)


def _verify_training_code_identity_files(
    project_root: str | Path,
) -> tuple[Path, ...]:
    """Fail before launch when a file required by capture_code_identity is absent."""

    root_candidate = Path(project_root).expanduser()
    if root_candidate.is_symlink():
        raise TrainingContinuationError(f"training code identity project must not be a symlink: {root_candidate}")
    root = root_candidate.resolve()
    if not root.is_dir():
        raise TrainingContinuationError(f"training code identity project must be a directory: {root}")
    verified: list[Path] = []
    for path in training_code_identity_paths(
        root,
        root / "scripts" / "train_sft.py",
    ):
        try:
            resolved, _ = _stable_bytes(
                path,
                "training code identity dependency",
            )
        except TrainingContinuationError as error:
            raise TrainingContinuationError(
                f"training code identity dependency is missing or unsafe: {path}"
            ) from error
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise TrainingContinuationError(
                f"training code identity dependency escapes the packet project: {resolved}"
            ) from error
        verified.append(resolved)
    return tuple(verified)


def _source_git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    head = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise TrainingContinuationError("cannot bind the source Git revision for the continuation packet")
    return head


def _packet_inventory(
    root: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, object]], str]:
    if root.is_symlink() or not root.is_dir():
        raise TrainingContinuationError(f"continuation packet must be a regular directory: {root}")
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise TrainingContinuationError(f"continuation packet may not contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded_paths:
            continue
        payload = path.read_bytes()
        parts = Path(relative).parts
        if relative.endswith(".safetensors") or "parent-checkpoint" in parts:
            raise TrainingContinuationError("continuation packet must not embed the accepted parent model")
        if _FORBIDDEN_PACKET_BASENAMES.fullmatch(path.name):
            raise TrainingContinuationError(f"continuation packet contains a credential-like filename: {relative}")
        if any(pattern.search(payload) is not None for pattern in _PACKET_CREDENTIAL_PATTERNS):
            raise TrainingContinuationError(f"continuation packet contains credential material: {relative}")
        files.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": "sha256:" + sha256_bytes(payload),
            }
        )
    if not files:
        raise TrainingContinuationError("continuation packet inventory is empty")
    return files, "sha256:" + sha256_bytes(canonical_json_bytes(files))


def verify_hf_training_continuation_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_flavor: str,
) -> dict[str, object]:
    """Verify every packet byte against the tree supplied outside the mount."""

    selected_flavor = _validate_continuation_flavor(expected_flavor)
    expected_budget = _continuation_budget(selected_flavor)
    if not isinstance(expected_packet_tree_sha256, str) or not _PREFIXED_SHA256.fullmatch(expected_packet_tree_sha256):
        raise TrainingContinuationError(
            "expected packet tree SHA-256 is required and must use sha256:<64 lowercase hex>"
        )
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"HF continuation packet must not be a symlink: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise TrainingContinuationError(f"HF continuation packet must be a directory: {root}")
    _, manifest_payload, manifest = _stable_json_object(
        root / "job-manifest.json",
        "HF continuation job manifest",
    )
    if manifest_payload != canonical_json_bytes(manifest):
        raise TrainingContinuationError("HF continuation job manifest must be canonical JSON")
    expected_keys = {
        "schema_version",
        "kind",
        "source_git_head",
        "continuation_manifest_sha256",
        "image",
        "flavor",
        "timeout_minutes",
        "maximum_job_cost_usd",
        "maximum_aggregate_cost_usd",
        "total_budget_usd",
        "private_output_required",
        "parent_model_embedded",
        "external_parent_mount_required",
        "quantization",
        "publication",
        "packet_tree_sha256",
        "inventory_excludes",
        "file_count",
        "byte_count",
        "output_root",
        "remote_result_uri",
    }
    expected_values = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_job_packet",
        "image": HF_MARKER_REPAIR_IMAGE,
        "flavor": selected_flavor,
        "timeout_minutes": CONTINUATION_TIMEOUT_MINUTES,
        "maximum_job_cost_usd": expected_budget["maximum_job_cost_usd"],
        "maximum_aggregate_cost_usd": expected_budget["maximum_total_cost_usd"],
        "total_budget_usd": 5.0,
        "private_output_required": True,
        "parent_model_embedded": False,
        "external_parent_mount_required": True,
        "quantization": False,
        "publication": False,
        "inventory_excludes": ["job-manifest.json"],
        "output_root": "/outputs/" + CONTINUATION_REMOTE_OUTPUT_PATH,
        "remote_result_uri": (CONTINUATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + CONTINUATION_REMOTE_OUTPUT_PATH),
    }
    if set(manifest) != expected_keys:
        raise TrainingContinuationError("HF continuation job manifest differs from the closed launch contract")
    if manifest.get("flavor") != selected_flavor:
        raise TrainingContinuationError(
            "HF continuation job manifest flavor differs from the externally bound launch flavor"
        )
    if any(manifest.get(key) != value for key, value in expected_values.items()):
        raise TrainingContinuationError("HF continuation job manifest differs from the closed launch contract")
    source_git_head = manifest.get("source_git_head")
    if not isinstance(source_git_head, str) or not re.fullmatch(r"[0-9a-f]{40}", source_git_head):
        raise TrainingContinuationError("HF continuation job manifest has an invalid source Git revision")
    inventory, actual_tree_sha256 = _packet_inventory(
        root,
        excluded_paths=frozenset({"job-manifest.json"}),
    )
    if (
        manifest.get("packet_tree_sha256") != expected_packet_tree_sha256
        or actual_tree_sha256 != expected_packet_tree_sha256
        or manifest.get("file_count") != len(inventory)
        or manifest.get("byte_count") != sum(int(item["bytes"]) for item in inventory)
    ):
        raise TrainingContinuationError("HF continuation packet tree differs from the externally bound launch tree")
    packet_project = root / "repo" / "ml" / "scriber_polishing"
    _verify_training_code_identity_files(packet_project)
    continuation_path = root / "repo" / "ml" / "scriber_polishing" / "job-input" / "continuation-manifest.json"
    _, continuation_payload, continuation = _stable_json_object(
        continuation_path,
        "packet continuation manifest",
    )
    if (
        continuation_payload != canonical_json_bytes(continuation)
        or continuation.get("kind") != "scriber_hf_training_continuation"
        or continuation.get("status") != "ready"
        or manifest.get("continuation_manifest_sha256") != "sha256:" + sha256_bytes(continuation_payload)
    ):
        raise TrainingContinuationError("packet continuation manifest differs from the job manifest")
    _, cost_payload, cost = _stable_json_object(
        continuation_path.parent / "hf-cost-audit.json",
        "packet HF cost audit",
    )
    if cost != build_hf_cost_audit() or cost_payload != canonical_json_bytes(cost):
        raise TrainingContinuationError(
            "packet HF cost audit differs from the reviewed 20 billed plus 1 non-billable job ledger"
        )
    continuation_budget = _mapping(
        continuation.get("budget"),
        "packet continuation budget",
    )
    exact_continuation_budget = {
        **expected_budget,
        "flavor": selected_flavor,
        "cost_audit_sha256": "sha256:" + sha256_bytes(cost_payload),
    }
    if continuation_budget != exact_continuation_budget:
        raise TrainingContinuationError(
            "packet continuation flavor or budget differs from the externally bound launch contract"
        )
    _, runner_payload = _stable_bytes(
        root / "run-training-continuation.sh",
        "packet continuation runner",
    )
    if runner_payload != continuation_runner_payload():
        raise TrainingContinuationError("packet continuation runner differs from the reviewed runner")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_packet_verification",
        "packet_tree_sha256": actual_tree_sha256,
        "file_count": len(inventory),
        "byte_count": sum(int(item["bytes"]) for item in inventory),
        "continuation_manifest_sha256": ("sha256:" + sha256_bytes(continuation_payload)),
        "flavor": selected_flavor,
        "credential_material_present": False,
        "parent_model_embedded": False,
    }


def claim_continuation_output_guard(
    output_root: str | Path,
    *,
    expected_packet_tree_sha256: str,
    continuation_manifest_sha256: str,
    owner_id: str,
) -> dict[str, object]:
    """Atomically claim the fixed output once for one externally bound packet."""

    for value, label in (
        (expected_packet_tree_sha256, "expected packet tree"),
        (continuation_manifest_sha256, "continuation manifest"),
    ):
        if not isinstance(value, str) or not _PREFIXED_SHA256.fullmatch(value):
            raise TrainingContinuationError(f"{label} SHA-256 must use sha256:<64 lowercase hex>")
    if not isinstance(owner_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        owner_id,
    ):
        raise TrainingContinuationError("continuation output owner ID must be a UUIDv4")
    candidate = Path(output_root).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"continuation output must not be a symlink: {candidate}")
    root = candidate.resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    guard = {
        "schema_version": 1,
        "kind": "scriber_training_continuation_launch_guard",
        "status": "active",
        "output_root": str(root),
        "packet_tree_sha256": expected_packet_tree_sha256,
        "continuation_manifest_sha256": continuation_manifest_sha256,
        "owner_id": owner_id,
        "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
        "resume_policy": "identity_bound_recovery_only_no_second_reservation",
    }
    try:
        root.mkdir()
    except FileExistsError as error:
        try:
            _, _, existing = _stable_json_object(
                root / "continuation-launch-guard.json",
                "existing continuation launch guard",
            )
        except TrainingContinuationError as guard_error:
            raise TrainingContinuationError("continuation output has a foreign or concurrent owner") from guard_error
        same_run = (
            existing.get("packet_tree_sha256") == expected_packet_tree_sha256
            and existing.get("continuation_manifest_sha256") == continuation_manifest_sha256
        )
        if same_run:
            raise TrainingContinuationError(
                "identity-bound partial output preserved; a second cost reservation "
                "is forbidden and recovery must continue in place"
            ) from error
        raise TrainingContinuationError("continuation output has a foreign or concurrent owner") from error
    guard_path = root / "continuation-launch-guard.json"
    try:
        with guard_path.open("xb") as stream:
            stream.write(canonical_json_bytes(guard))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise TrainingContinuationError("could not write the exclusive continuation launch guard") from error
    return guard


def _build_training_order_and_config(
    *,
    tokenizer: Any,
    training_rows: Sequence[Mapping[str, str]],
    assignments: Mapping[str, int],
    training_policy: Mapping[str, object],
    training_policy_sha256: str,
    validation_payload: bytes,
) -> tuple[bytes, bytes, bytes, dict[str, object]]:
    train_payload = _pair_jsonl_payload(training_rows)
    train_sha256 = sha256_bytes(train_payload)
    validation_sha256 = sha256_bytes(validation_payload)
    train_ids = [str(row["example_id"]) for row in training_rows]
    if (
        train_ids != sorted(train_ids)
        or len(train_ids) != CONTINUATION_TRAIN_EXAMPLES
        or len(set(train_ids)) != CONTINUATION_TRAIN_EXAMPLES
        or set(assignments) != set(train_ids)
    ):
        raise TrainingContinuationError("continuation train rows/phase assignments are not exact")
    protected = apply_training_protection(
        training_rows,
        policy=training_policy,
    )
    if len(protected.records) != CONTINUATION_TRAIN_EXAMPLES or protected.rejected_example_ids:
        raise TrainingContinuationError("selected continuation train contains a policy rejection")
    sequence_lengths = [_token_length(tokenizer, record) for record in protected.records]
    if not sequence_lengths or max(sequence_lengths) > CONTINUATION_MAX_SEQUENCE_LENGTH:
        raise TrainingContinuationError("selected continuation train contains an oversize sequence")
    empty_hash = hash_identifier_sequence([])
    training_contract = {
        "max_sequence_length": CONTINUATION_MAX_SEQUENCE_LENGTH,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "effective_batch_size": CONTINUATION_EFFECTIVE_BATCH_SIZE,
        "learning_rate": 0.00001,
        "optimizer": "adamw_torch_fused",
        "scheduler": "linear",
        "eval_steps": 25,
        "save_steps": 25,
        "save_total_limit": 4,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "max_grad_norm": 1.0,
        "bf16": True,
        "gradient_checkpointing": True,
        "assistant_output_loss_only": True,
        "early_stopping": False,
    }
    comparison_pair_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "kind": "scriber_training_continuation_order_identity",
                "accepted_parent_model_tree_sha256": (_ACCEPTED_PARENT_TREE_SHA256),
                "accepted_parent_directory_tree_sha256": (_ACCEPTED_PARENT_DIRECTORY_TREE_SHA256),
                "selected_train_sha256": train_sha256,
                "mode": "shuffled",
                "seed": CONTINUATION_SEED,
            }
        )
    )
    plan = _build_order_plan(
        mode="shuffled",
        seed=CONTINUATION_SEED,
        comparison_pair_sha256=comparison_pair_sha256,
        source_binding={
            "filename": "selected-train.jsonl",
            "sha256": train_sha256,
            "record_count": CONTINUATION_TRAIN_EXAMPLES,
            "split": "train",
            "selected_safe_example_ids_sha256": (hash_identifier_sequence(train_ids)),
        },
        safety_source_binding={
            "filename": "selected-train.jsonl",
            "sha256": train_sha256,
            "record_count": CONTINUATION_TRAIN_EXAMPLES,
            "split": "train",
        },
        policy_binding={
            "policy_id": training_policy["policy_id"],
            "policy_sha256": training_policy["policy_sha256"],
            "artifact_sha256": training_policy_sha256,
        },
        parent_binding={
            "candidate_id": "accepted_marker_lexical_repair",
            "training_report_sha256": "sha256:" + _ACCEPTED_TRAINING_REPORT_SHA256,
            "tree_sha256": _ACCEPTED_PARENT_DIRECTORY_TREE_SHA256,
        },
        safety_filter={
            "loaded_record_count": CONTINUATION_TRAIN_EXAMPLES,
            "accepted_record_count": CONTINUATION_TRAIN_EXAMPLES,
            "rejected_record_count": 0,
            "rejected_example_ids_sha256": empty_hash,
            "url_overlap_rejected_example_ids": [],
            "url_overlap_rejected_example_ids_sha256": empty_hash,
        },
        training_contract=training_contract,
        assignments=assignments,
    )
    plan = validate_remediation_order_plan(plan)
    plan_payload = canonical_json_bytes(plan)
    plan_sha256 = sha256_bytes(plan_payload)
    effective = build_effective_improvement_order(
        plan,
        plan_binding={
            "path": "order-plan.json",
            "sha256": "sha256:" + plan_sha256,
        },
        loaded_example_ids=train_ids,
        safe_example_ids=protected.accepted_example_ids,
        policy_rejected_example_ids=protected.rejected_example_ids,
        policy_rejection_evidence=protected.evidence,
        used_example_ids=train_ids,
        sequence_rejected_example_ids=[],
        sequence_lengths=sequence_lengths,
    )
    effective_ids = [train_ids[index] for index in effective.epoch_indices[0]]
    effective_order_sha256 = hash_identifier_sequence(effective_ids)
    if (
        train_sha256 != _FINAL_TRAIN_SHA256
        or validation_sha256 != _VALIDATION_SHA256
        or plan_sha256 != _ORDER_PLAN_SHA256
        or effective_order_sha256 != _EFFECTIVE_ORDER_SHA256
    ):
        raise TrainingContinuationError(
            "continuation train, validation, plan, or effective order differs from the reviewed dry-bind locks"
        )
    dataset_manifest = {
        "schema_version": 1,
        "kind": "scriber_training_continuation_dataset",
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
        "split_leakage": False,
        "holdout_inputs": False,
        "opened_splits": ["train", "validation"],
        "sealed_splits": ["test", "challenge"],
        "split_counts": {
            "train": CONTINUATION_TRAIN_EXAMPLES,
            "validation": 431,
        },
        "split_sha256": {
            "train": train_sha256,
            "validation": validation_sha256,
        },
    }
    dataset_payload = canonical_json_bytes(dataset_manifest)
    config = _expected_continuation_config_values()
    if training_policy_sha256 != _POLICY_ARTIFACT_SHA256 or plan_sha256 != _ORDER_PLAN_SHA256:
        raise TrainingContinuationError("materialized continuation config inputs differ from the reviewed locks")
    config_payload = (
        yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
        )
        .replace("\r\n", "\n")
        .encode("utf-8")
    )
    return (
        train_payload,
        plan_payload,
        dataset_payload,
        {
            "config_payload": config_payload,
            "train_sha256": train_sha256,
            "validation_sha256": validation_sha256,
            "plan_sha256": plan_sha256,
            "declared_order_sha256": plan["epoch_orders"][0]["example_ids_sha256"],
            "effective_order_sha256": effective_order_sha256,
            "max_sequence_length": max(sequence_lengths),
            "sequence_lengths_sha256": hash_identifier_sequence([str(length) for length in sequence_lengths]),
            "training_contract": training_contract,
        },
    )


def build_hf_training_continuation_packet(
    *,
    output_dir: str | Path,
    tokenizer: Any,
    flavor: str = CONTINUATION_DEFAULT_FLAVOR,
    acceptance_report_path: str | Path,
    repair_bundle_dir: str | Path,
    training_report_path: str | Path,
    cloud_result_path: str | Path,
    accepted_parent_dir: str | Path,
    pilot_selection_report_path: str | Path,
    ai_gold_manifest_path: str | Path,
    ai_gold_train_path: str | Path,
    final_manifest_path: str | Path,
    final_train_path: str | Path,
    pilot_manifest_path: str | Path,
    pilot_train_path: str | Path,
) -> HfTrainingContinuationPacket:
    """Build one immutable packet while keeping the 575 MB parent external."""

    selected_flavor = _validate_continuation_flavor(flavor)
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise TrainingContinuationError(f"refusing to overwrite HF continuation packet: {target}")
    repair_root = Path(repair_bundle_dir).expanduser().resolve()
    if repair_root.is_symlink() or not repair_root.is_dir():
        raise TrainingContinuationError(f"repair bundle must be a regular directory: {repair_root}")
    parent_root = Path(accepted_parent_dir).expanduser().resolve()
    parent_binding = verify_accepted_repair_parent(
        acceptance_report_path=acceptance_report_path,
        repair_manifest_path=repair_root / "repair-manifest.json",
        training_report_path=training_report_path,
        cloud_result_path=cloud_result_path,
        accepted_parent_dir=parent_root,
        pilot_selection_report_path=pilot_selection_report_path,
    )
    try:
        tokenizer_identity = capture_tokenizer_identity(
            tokenizer,
            model_id=BASE_MODEL_ID,
            revision=BASE_MODEL_REVISION,
            local_source_path=parent_root,
        )
    except ValueError as error:
        raise TrainingContinuationError(f"accepted-parent tokenizer identity is invalid: {error}") from error

    _, policy_payload, policy_raw = _stable_json_object(
        repair_root / "training-policy.json",
        "accepted lexical training policy",
    )
    try:
        training_policy = validate_protection_policy_evidence(
            policy_raw,
            tokenizer=tokenizer,
        )
    except ValueError as error:
        raise TrainingContinuationError(f"accepted lexical training policy is invalid: {error}") from error
    policy_sha256 = sha256_bytes(policy_payload)
    if (
        policy_sha256 != "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
        or training_policy.get("policy_id") != "gemma_lexical_v1"
        or _mapping(
            training_policy.get("tokenizer"),
            "training-policy tokenizer",
        ).get("identity_sha256")
        != tokenizer_identity.get("identity_sha256")
    ):
        raise TrainingContinuationError("continuation policy/tokenizer differs from the accepted repair")

    _, repair_manifest_payload, repair_manifest_raw = _stable_json_object(
        repair_root / "repair-manifest.json",
        "marker-repair manifest",
    )
    repair_manifest = validate_marker_repair_manifest(repair_manifest_raw)
    validation_binding = _mapping(
        _mapping(repair_manifest.get("data"), "repair data").get("validation"),
        "repair validation binding",
    )
    _, validation_payload = _stable_bytes(
        repair_root / str(validation_binding["filename"]),
        "accepted repair safe validation",
    )
    validation_lines = validation_payload.splitlines()
    if (
        len(validation_lines) != 431
        or validation_binding.get("record_count") != 431
        or sha256_bytes(validation_payload) != validation_binding.get("sha256")
    ):
        raise TrainingContinuationError("safe validation differs from the accepted 431-row repair split")
    for line_number, raw_line in enumerate(validation_lines, start=1):
        try:
            row = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrainingContinuationError(f"safe validation row {line_number} is invalid JSON") from error
        if not isinstance(row, Mapping) or not all(
            isinstance(row.get(field), str) and row.get(field) for field in ("example_id", "source_text", "target_sst")
        ):
            raise TrainingContinuationError(f"safe validation row {line_number} is not a training pair")

    training_rows, selection_evidence = prepare_continuation_training_mix(
        tokenizer=tokenizer,
        training_policy=training_policy,
        ai_gold_manifest_path=ai_gold_manifest_path,
        ai_gold_train_path=ai_gold_train_path,
        final_manifest_path=final_manifest_path,
        final_train_path=final_train_path,
        pilot_manifest_path=pilot_manifest_path,
        pilot_train_path=pilot_train_path,
    )
    assignments_raw = selection_evidence.get("assignments")
    if not isinstance(assignments_raw, Mapping):
        raise TrainingContinuationError("continuation selection lacks exact phase assignments")
    assignments = {str(key): int(value) for key, value in assignments_raw.items()}
    (
        train_payload,
        plan_payload,
        dataset_payload,
        training_evidence,
    ) = _build_training_order_and_config(
        tokenizer=tokenizer,
        training_rows=training_rows,
        assignments=assignments,
        training_policy=training_policy,
        training_policy_sha256=policy_sha256,
        validation_payload=validation_payload,
    )
    config_payload = training_evidence.pop("config_payload")
    assert isinstance(config_payload, bytes)
    cost_audit = build_hf_cost_audit()
    cost_payload = canonical_json_bytes(cost_audit)
    if cost_audit["conservative_prior_cost_usd"] != format(
        CONTINUATION_PRIOR_COST_USD,
        "f",
    ):
        raise TrainingContinuationError("reviewed HF cost audit differs from the continuation prior-cost binding")
    budget = _continuation_budget(selected_flavor)
    arithmetic = validate_continuation_arithmetic(
        start_updates=CONTINUATION_START_UPDATES,
        additional_updates=CONTINUATION_ADDITIONAL_UPDATES,
        target_updates=CONTINUATION_TARGET_UPDATES,
    )

    evidence_sources = {
        "acceptance-report.json": acceptance_report_path,
        "repair-manifest.json": repair_root / "repair-manifest.json",
        "accepted-training-report.json": training_report_path,
        "accepted-cloud-result.json": cloud_result_path,
        "pilot-selection-report.json": pilot_selection_report_path,
        "ai-gold-manifest.json": ai_gold_manifest_path,
        "final-data-manifest.json": final_manifest_path,
        "pilot-data-manifest.json": pilot_manifest_path,
    }
    evidence_payloads = {name: _stable_bytes(path, name)[1] for name, path in evidence_sources.items()}
    generated_payloads: dict[str, bytes] = {
        **evidence_payloads,
        "hf-cost-audit.json": cost_payload,
        "selection-evidence.json": canonical_json_bytes(selection_evidence),
        "selected-train.jsonl": train_payload,
        "safe-validation.jsonl": validation_payload,
        "training-policy.json": policy_payload,
        "order-plan.json": plan_payload,
        "dataset-manifest.json": dataset_payload,
        "train.yaml": config_payload,
    }
    artifact_bindings = {
        name: {
            "filename": name,
            "bytes": len(payload),
            "sha256": "sha256:" + sha256_bytes(payload),
        }
        for name, payload in sorted(generated_payloads.items())
    }
    continuation_manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation",
        "status": "ready",
        "arithmetic": arithmetic,
        "parent": parent_binding,
        "data": {
            "train_examples": CONTINUATION_TRAIN_EXAMPLES,
            "validation_examples": 431,
            "ai_gold_train_examples": 767,
            "final_data_train_examples": 3297,
            "phase_counts": {str(phase): count for phase, count in CONTINUATION_PHASE_TARGETS.items()},
            "test_inputs": False,
            "challenge_inputs": False,
            "pilot_overlap_count": 0,
        },
        "training": {
            **training_evidence,
            "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
            "cumulative_updates_after_success": (CONTINUATION_TARGET_UPDATES),
            "resume_from_checkpoint": None,
            "parent_relationship": "warm_start_parent_not_resume",
            "optimizer_state": "fresh",
            "scheduler_state": "fresh",
            "implicit_trainer_shuffling": False,
            "quantization": None,
            "publication": False,
        },
        "budget": {
            **budget,
            "flavor": selected_flavor,
            "cost_audit_sha256": "sha256:" + sha256_bytes(cost_payload),
        },
        "storage": {
            "parent_model_embedded": False,
            "parent_volume": CONTINUATION_REMOTE_PARENT_URI + ":/accepted-parent:ro",
            "output_volume": CONTINUATION_REMOTE_OUTPUT_BUCKET + ":/outputs:rw",
            "output_root": "/outputs/" + CONTINUATION_REMOTE_OUTPUT_PATH,
            "remote_result_uri": (
                CONTINUATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + CONTINUATION_REMOTE_OUTPUT_PATH
            ),
        },
        "artifacts": artifact_bindings,
    }
    continuation_payload = canonical_json_bytes(continuation_manifest)
    generated_payloads["continuation-manifest.json"] = continuation_payload

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        project = temp_root / "repo" / "ml" / "scriber_polishing"
        project.mkdir(parents=True)
        _copy_project_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_project_tree(
            _PROJECT_ROOT / "contracts",
            project / "contracts",
        )
        scripts = project / "scripts"
        scripts.mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "scripts" / "verify_hf_training_continuation.py",
            scripts / "verify_hf_training_continuation.py",
        )
        _copy_training_code_identity_files(_PROJECT_ROOT, project)
        configs = project / "configs"
        configs.mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "configs" / "evaluation.yaml",
            configs / "evaluation.yaml",
        )
        for filename in ("pyproject.toml", "README.md"):
            shutil.copy2(_PROJECT_ROOT / filename, project / filename)
        job_input = project / "job-input"
        job_input.mkdir()
        for filename, payload in generated_payloads.items():
            (job_input / filename).write_bytes(payload)

        config_snapshot = load_training_config(job_input / "train.yaml")
        if config_snapshot.values["epochs"] != 1:
            raise TrainingContinuationError("materialized continuation config changed its one-epoch schedule")
        (temp_root / "run-training-continuation.sh").write_bytes(continuation_runner_payload())
        inventory, packet_tree_sha256 = _packet_inventory(temp_root)
        job_manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_training_continuation_job_packet",
            "source_git_head": _source_git_head(),
            "continuation_manifest_sha256": "sha256:" + sha256_bytes(continuation_payload),
            "image": HF_MARKER_REPAIR_IMAGE,
            "flavor": selected_flavor,
            "timeout_minutes": CONTINUATION_TIMEOUT_MINUTES,
            "maximum_job_cost_usd": budget["maximum_job_cost_usd"],
            "maximum_aggregate_cost_usd": budget["maximum_total_cost_usd"],
            "total_budget_usd": 5.0,
            "private_output_required": True,
            "parent_model_embedded": False,
            "external_parent_mount_required": True,
            "quantization": False,
            "publication": False,
            "packet_tree_sha256": packet_tree_sha256,
            "inventory_excludes": ["job-manifest.json"],
            "file_count": len(inventory),
            "byte_count": sum(int(item["bytes"]) for item in inventory),
            "output_root": "/outputs/" + CONTINUATION_REMOTE_OUTPUT_PATH,
            "remote_result_uri": (
                CONTINUATION_REMOTE_OUTPUT_BUCKET.rstrip("/") + "/" + CONTINUATION_REMOTE_OUTPUT_PATH
            ),
        }
        (temp_root / "job-manifest.json").write_bytes(canonical_json_bytes(job_manifest))
        temp_root.replace(target)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return HfTrainingContinuationPacket(
        output_dir=target,
        manifest=MappingProxyType(job_manifest),
    )


def verify_hf_training_continuation_bundle(
    bundle_dir: str | Path,
    *,
    parent_model: str | Path,
    mode: str,
    tokenizer: Any | None = None,
) -> dict[str, object]:
    """Dry-bind the packet remotely or fully after the stable local copy."""

    if mode not in {"preflight-remote", "preflight-local"}:
        raise TrainingContinuationError("continuation preflight mode is invalid")
    candidate = Path(bundle_dir).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"continuation bundle must not be a symlink: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise TrainingContinuationError(f"continuation bundle must be a directory: {root}")
    _, manifest_payload, manifest = _stable_json_object(
        root / "continuation-manifest.json",
        "continuation manifest",
    )
    if (
        manifest_payload != canonical_json_bytes(manifest)
        or manifest.get("kind") != "scriber_hf_training_continuation"
        or manifest.get("status") != "ready"
    ):
        raise TrainingContinuationError("continuation manifest is not canonical ready evidence")
    artifacts = _mapping(
        manifest.get("artifacts"),
        "continuation artifacts",
    )
    if set(artifacts) != _CONTINUATION_ARTIFACT_NAMES:
        unsafe = next(
            (
                name
                for name in artifacts
                if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
            ),
            None,
        )
        if unsafe is not None:
            raise TrainingContinuationError(f"continuation manifest has an unsafe artifact name: {unsafe}")
        raise TrainingContinuationError("continuation manifest artifact set differs from the closed packet")
    for name, raw_binding in artifacts.items():
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            raise TrainingContinuationError(f"continuation manifest has an unsafe artifact name: {name}")
        binding = _mapping(raw_binding, f"continuation artifact {name}")
        if binding.get("filename") != name:
            raise TrainingContinuationError(f"continuation artifact filename mismatch: {name}")
        artifact_path, payload = _stable_bytes(root / name, name)
        if artifact_path.parent != root:
            raise TrainingContinuationError(f"continuation manifest has an unsafe artifact path: {name}")
        if binding.get("bytes") != len(payload) or binding.get("sha256") != "sha256:" + sha256_bytes(payload):
            raise TrainingContinuationError(f"continuation artifact differs from manifest: {name}")
    _, cost_payload, cost = _stable_json_object(
        root / "hf-cost-audit.json",
        "HF cost audit",
    )
    if cost != build_hf_cost_audit() or cost_payload != canonical_json_bytes(cost):
        raise TrainingContinuationError(
            "HF cost audit differs from the reviewed 20 billed plus 1 non-billable job ledger"
        )
    budget = _mapping(manifest.get("budget"), "continuation budget")
    selected_flavor = _validate_continuation_flavor(budget.get("flavor"))
    expected_budget = {
        **_continuation_budget(selected_flavor),
        "flavor": selected_flavor,
        "cost_audit_sha256": "sha256:" + sha256_bytes(cost_payload),
    }
    if budget != expected_budget:
        raise TrainingContinuationError(
            "continuation flavor or budget reservation differs from the closed launch contract"
        )
    parent = verify_accepted_repair_parent(
        acceptance_report_path=root / "acceptance-report.json",
        repair_manifest_path=root / "repair-manifest.json",
        training_report_path=root / "accepted-training-report.json",
        cloud_result_path=root / "accepted-cloud-result.json",
        accepted_parent_dir=parent_model,
        pilot_selection_report_path=root / "pilot-selection-report.json",
        parent_inventory_mode="remote_metadata",
    )
    config = load_training_config(root / "train.yaml")
    if dict(config.values) != _expected_continuation_config_values():
        raise TrainingContinuationError("continuation config differs from the reviewed 254-update contract")
    dataset = bind_training_dataset(
        root / "dataset-manifest.json",
        train=root / "selected-train.jsonl",
        validation=root / "safe-validation.jsonl",
    )
    if (
        dataset.train.total_records != CONTINUATION_TRAIN_EXAMPLES
        or dataset.validation.total_records != 431
        or dataset.train.sha256 != "sha256:" + _FINAL_TRAIN_SHA256
        or dataset.validation.sha256 != "sha256:" + _VALIDATION_SHA256
    ):
        raise TrainingContinuationError("continuation dry bind changed train or validation count")
    _, plan_payload, plan_raw = _stable_json_object(
        root / "order-plan.json",
        "continuation order plan",
    )
    plan = validate_remediation_order_plan(plan_raw)
    if (
        plan_payload != canonical_json_bytes(plan)
        or plan.get("mode") != "shuffled"
        or plan.get("holdout_inputs") is not False
        or sha256_bytes(plan_payload) != _ORDER_PLAN_SHA256
    ):
        raise TrainingContinuationError("continuation order plan is not the exact shuffled train-only plan")
    _, policy_payload, policy_raw = _stable_json_object(
        root / "training-policy.json",
        "continuation training policy",
    )
    try:
        policy = validate_protection_policy_evidence(policy_raw)
    except ValueError as error:
        raise TrainingContinuationError(f"continuation training policy is invalid: {error}") from error
    policy_tokenizer = _mapping(
        policy.get("tokenizer"),
        "continuation policy tokenizer",
    )
    if (
        sha256_bytes(policy_payload) != _POLICY_ARTIFACT_SHA256
        or policy.get("policy_sha256") != _POLICY_SELF_SHA256
        or policy_tokenizer.get("identity_sha256") != _TOKENIZER_IDENTITY_SHA256
    ):
        raise TrainingContinuationError("continuation policy or tokenizer differs from the accepted repair")
    expected_training = _mapping(
        manifest.get("training"),
        "continuation training",
    )
    if (
        expected_training.get("train_sha256") != _FINAL_TRAIN_SHA256
        or expected_training.get("validation_sha256") != _VALIDATION_SHA256
        or expected_training.get("plan_sha256") != _ORDER_PLAN_SHA256
        or expected_training.get("effective_order_sha256") != _EFFECTIVE_ORDER_SHA256
        or expected_training.get("additional_updates") != CONTINUATION_ADDITIONAL_UPDATES
    ):
        raise TrainingContinuationError("continuation manifest differs from the reviewed dry-bind hashes")
    input_binding = {
        "continuation_manifest_sha256": ("sha256:" + sha256_bytes(manifest_payload)),
        "hf_flavor": selected_flavor,
        "config_sha256": config.sha256,
        "dataset_manifest_sha256": dataset.manifest["sha256"],
        "train_sha256": dataset.train.sha256,
        "validation_sha256": dataset.validation.sha256,
        "order_plan_sha256": "sha256:" + sha256_bytes(plan_payload),
        "effective_order_sha256": _EFFECTIVE_ORDER_SHA256,
        "protection_policy_artifact_sha256": ("sha256:" + sha256_bytes(policy_payload)),
        "protection_policy_sha256": _POLICY_SELF_SHA256,
        "tokenizer_identity_sha256": _TOKENIZER_IDENTITY_SHA256,
        "parent_directory_tree_sha256": (_ACCEPTED_PARENT_DIRECTORY_TREE_SHA256),
        "base_model": BASE_MODEL_ID,
        "base_revision": BASE_MODEL_REVISION,
    }
    if mode == "preflight-remote":
        return {
            "schema_version": 1,
            "kind": "scriber_training_continuation_preflight",
            "mode": mode,
            "ready_for_parent_copy": True,
            "ready_for_model_load": False,
            "large_remote_files_hashed": False,
            "parent": parent,
            "train_examples": CONTINUATION_TRAIN_EXAMPLES,
            "validation_examples": 431,
            "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
            "input_binding": input_binding,
        }
    if tokenizer is None:
        raise TrainingContinuationError("local continuation preflight requires the accepted tokenizer")
    expected_parent = (root / "parent-checkpoint").resolve()
    if Path(parent_model).expanduser().resolve() != expected_parent:
        raise TrainingContinuationError("local accepted parent must be copied to job-input/parent-checkpoint")
    parent_checkpoint = bind_parent_checkpoint(config)
    if parent_checkpoint is None or parent_checkpoint.tree_sha256 != _ACCEPTED_PARENT_DIRECTORY_TREE_SHA256:
        raise TrainingContinuationError("local parent full content hash differs from acceptance")
    protection = bind_protection_policy(config, tokenizer=tokenizer)
    if protection is None:
        raise TrainingContinuationError("local continuation policy did not bind")
    train_protected = apply_training_protection(
        dataset.train.records,
        policy=protection.values,
    )
    validation_protected = apply_training_protection(
        dataset.validation.records,
        policy=protection.values,
    )
    if train_protected.rejected_example_ids or validation_protected.rejected_example_ids:
        raise TrainingContinuationError("local continuation dry bind produced a policy rejection")
    bound_plan = bind_improvement_order_plan(
        config,
        dataset,
        protection_policy=protection,
        parent_checkpoint=parent_checkpoint,
        protection_application=train_protected.evidence,
    )
    train_ids: list[str] = []
    train_lengths: list[int] = []
    for record in train_protected.records:
        length = _token_length(tokenizer, record)
        if length > CONTINUATION_MAX_SEQUENCE_LENGTH:
            raise TrainingContinuationError("local continuation dry bind produced an oversize train row")
        train_ids.append(str(record["example_id"]))
        train_lengths.append(length)
    for record in validation_protected.records:
        if _token_length(tokenizer, record) > CONTINUATION_MAX_SEQUENCE_LENGTH:
            raise TrainingContinuationError("local continuation dry bind produced an oversize validation row")
    effective = build_effective_improvement_order(
        bound_plan.values,
        plan_binding=bound_plan.binding(),
        loaded_example_ids=[str(record["example_id"]) for record in dataset.train.records],
        safe_example_ids=train_protected.accepted_example_ids,
        policy_rejected_example_ids=train_protected.rejected_example_ids,
        policy_rejection_evidence=train_protected.evidence,
        used_example_ids=train_ids,
        sequence_rejected_example_ids=[],
        sequence_lengths=train_lengths,
    )
    effective_ids = [train_ids[index] for index in effective.epoch_indices[0]]
    schedule = resolve_epoch_order_schedule(
        config.values,
        train_examples_used=len(train_ids),
    )
    if (
        schedule.get("max_steps") != CONTINUATION_ADDITIONAL_UPDATES
        or schedule.get("update_steps_per_epoch") != CONTINUATION_ADDITIONAL_UPDATES
        or expected_training.get("effective_order_sha256") != hash_identifier_sequence(effective_ids)
    ):
        raise TrainingContinuationError("local continuation dry bind changed schedule or explicit order")
    return {
        "schema_version": 1,
        "kind": "scriber_training_continuation_preflight",
        "mode": mode,
        "ready_for_parent_copy": False,
        "ready_for_model_load": True,
        "large_remote_files_hashed": False,
        "local_parent_full_content_hashes": True,
        "parent": parent,
        "parent_directory_tree_sha256": parent_checkpoint.tree_sha256,
        "train_examples": len(train_ids),
        "validation_examples": len(validation_protected.records),
        "additional_updates": int(schedule["max_steps"]),
        "effective_order_sha256": hash_identifier_sequence(effective_ids),
        "protection_policy_id": protection.values["policy_id"],
        "protection_policy_sha256": protection.values["policy_sha256"],
        "input_binding": input_binding,
    }


def build_continuation_postflight_result(
    *,
    training_report_path: str | Path,
    output_root: str | Path,
    input_binding: Mapping[str, object],
    expected_flavor: str = CONTINUATION_DEFAULT_FLAVOR,
) -> dict[str, object]:
    """Verify the exact new run and bind its final BF16 model."""

    selected_flavor = _validate_continuation_flavor(expected_flavor)
    if input_binding.get("hf_flavor") != selected_flavor:
        raise TrainingContinuationError(
            "continuation postflight flavor differs from the externally bound launch flavor"
        )
    root_candidate = Path(output_root).expanduser()
    if root_candidate.is_symlink():
        raise TrainingContinuationError(f"continuation output must not be a symlink: {root_candidate}")
    root = root_candidate.resolve()
    report_candidate = Path(training_report_path).expanduser()
    expected_report = root / "training-report.json"
    if report_candidate.is_symlink() or report_candidate.resolve() != expected_report or not expected_report.is_file():
        raise TrainingContinuationError("continuation postflight requires exactly <output>/training-report.json")
    training_root = root / "training"
    if training_root.is_symlink():
        raise TrainingContinuationError(f"continuation training directory must not be a symlink: {training_root}")
    if not training_root.is_dir():
        raise TrainingContinuationError(f"continuation training directory is missing: {training_root}")
    result = verify_continuation_training_report(
        expected_report,
        expected_parent_tree_sha256=(_ACCEPTED_PARENT_DIRECTORY_TREE_SHA256),
        expected_input_binding=input_binding,
        output_root=root,
    )
    report_path, report_payload, report = _stable_json_object(
        expected_report,
        "continuation training report",
    )
    final_directory = str(result["final_model_directory"])
    final_candidate = training_root / final_directory
    if final_candidate.is_symlink():
        raise TrainingContinuationError(f"continuation final directory must not be a symlink: {final_candidate}")
    final_root = final_candidate.resolve()
    if report_path != expected_report or final_root.parent != training_root or not final_root.is_dir():
        raise TrainingContinuationError("continuation postflight output paths are inconsistent")
    try:
        from .inference import _inventory_local_model_tree

        inventory = _inventory_local_model_tree(final_root)
    except (OSError, TypeError, ValueError) as error:
        raise TrainingContinuationError(f"continuation final model inventory is invalid: {error}") from error
    declared_inventory = _mapping(
        _mapping(report.get("final_model"), "final model").get("inventory"),
        "final model inventory",
    )
    if (
        inventory.get("tree_sha256") != declared_inventory.get("tree_sha256")
        or inventory.get("file_count") != declared_inventory.get("file_count")
        or inventory.get("total_bytes") != declared_inventory.get("total_bytes")
    ):
        raise TrainingContinuationError("continuation final model differs from its training report")
    if inventory.get("tree_sha256") == _ACCEPTED_PARENT_TREE_SHA256:
        raise TrainingContinuationError("continuation final model must differ from accepted parent")
    return {
        **result,
        "status": "complete",
        "flavor": selected_flavor,
        "training_report_sha256": "sha256:" + sha256_bytes(report_payload),
        "final_model_file_count": inventory["file_count"],
        "final_model_total_bytes": inventory["total_bytes"],
        "quantization_performed": False,
        "publication_performed": False,
    }


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TrainingContinuationError(f"{label} must be an object")
    return value


def _stable_json_object(
    path: str | Path,
    label: str,
) -> tuple[Path, bytes, dict[str, Any]]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise TrainingContinuationError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise TrainingContinuationError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    try:
        payload = resolved.read_bytes()
    except OSError as error:
        raise TrainingContinuationError(f"cannot read {label}: {error}") from error
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
        raise TrainingContinuationError(f"{label} changed while it was read")

    def reject_constant(value: str) -> None:
        raise TrainingContinuationError(f"{label} contains non-finite JSON value {value}")

    def unique_object(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TrainingContinuationError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingContinuationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise TrainingContinuationError(f"{label} must contain an object")
    return resolved, payload, value


def _verify_parent_metadata_without_large_remote_reads(
    root: Path,
) -> dict[str, object]:
    """Bind the remote inventory and file metadata without reading 536 MB."""

    if root.is_symlink() or not root.is_dir():
        raise TrainingContinuationError(f"accepted parent must be a regular directory: {root}")
    _, payload, inventory = _stable_json_object(
        root / "checkpoint-inventory.json",
        "accepted-parent checkpoint inventory",
    )
    if (
        len(payload) != 1937
        or sha256_bytes(payload) != _ACCEPTED_CHECKPOINT_INVENTORY_SHA256
        or payload != canonical_json_bytes(inventory)
        or inventory.get("tree_sha256") != _ACCEPTED_PARENT_TREE_SHA256
        or inventory.get("file_count") != 12
        or inventory.get("total_bytes") != 575466744
    ):
        raise TrainingContinuationError("accepted-parent checkpoint inventory differs from acceptance")
    raw_files = inventory.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != 12:
        raise TrainingContinuationError("accepted-parent checkpoint inventory has invalid files")
    expected_sizes: dict[str, int] = {}
    previous = ""
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise TrainingContinuationError("accepted-parent checkpoint file binding is invalid")
        relative = item.get("path")
        size = item.get("bytes")
        digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative <= previous
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(digest, str)
            or not _PREFIXED_SHA256.fullmatch(digest)
        ):
            raise TrainingContinuationError("accepted-parent checkpoint inventory paths are unsafe")
        previous = relative
        expected_sizes[relative] = size
    actual_sizes: dict[str, int] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise TrainingContinuationError(f"accepted-parent remote tree contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            actual_sizes[relative] = path.stat().st_size
    expected_sizes["checkpoint-inventory.json"] = len(payload)
    if actual_sizes != expected_sizes:
        raise TrainingContinuationError("accepted-parent remote paths/sizes differ from its inventory")
    return {
        "verification_mode": "inventory_bytes_and_path_sizes_only",
        "large_remote_files_hashed": False,
        "checkpoint_inventory_sha256": "sha256:" + _ACCEPTED_CHECKPOINT_INVENTORY_SHA256,
        "physical_file_count": 13,
        "physical_total_bytes": sum(actual_sizes.values()),
        "accepted_model_tree_sha256": _ACCEPTED_PARENT_TREE_SHA256,
    }


def verify_accepted_repair_parent(
    *,
    acceptance_report_path: str | Path,
    repair_manifest_path: str | Path,
    training_report_path: str | Path,
    cloud_result_path: str | Path,
    accepted_parent_dir: str | Path,
    pilot_selection_report_path: str | Path,
    parent_inventory_mode: str = "full",
) -> dict[str, object]:
    """Prove the accepted 700-step pilot plus 146-step repair lineage."""

    _, acceptance_payload, acceptance_raw = _stable_json_object(
        acceptance_report_path,
        "marker-repair acceptance report",
    )
    acceptance_sha256 = sha256_bytes(acceptance_payload)
    if acceptance_sha256 != _ACCEPTANCE_REPORT_SHA256:
        raise TrainingContinuationError("marker-repair acceptance report differs from the immutable accepted decision")
    try:
        acceptance = validate_marker_repair_acceptance_report(acceptance_raw)
    except ValueError as error:
        raise TrainingContinuationError(f"marker-repair acceptance report is invalid: {error}") from error
    if (
        acceptance.get("accepted") is not True
        or acceptance.get("decision") != "ACCEPT_REPAIR"
        or acceptance.get("status") != "complete"
    ):
        raise TrainingContinuationError("marker-repair acceptance decision is not ACCEPT_REPAIR")

    _, repair_payload, repair_raw = _stable_json_object(
        repair_manifest_path,
        "marker-repair manifest",
    )
    repair_sha256 = sha256_bytes(repair_payload)
    try:
        repair = validate_marker_repair_manifest(repair_raw)
    except ValueError as error:
        raise TrainingContinuationError(f"marker-repair manifest is invalid: {error}") from error
    experiment = _mapping(
        acceptance.get("experiment"),
        "acceptance experiment",
    )
    if (
        experiment.get("repair_manifest_sha256") != repair_sha256
        or repair.get("schedule", {}).get("update_steps") != REPAIR_UPDATES
    ):
        raise TrainingContinuationError("accepted repair manifest does not bind the 146-step repair")

    _, training_payload, training = _stable_json_object(
        training_report_path,
        "accepted repair training report",
    )
    training_sha256 = sha256_bytes(training_payload)
    _, cloud_payload, cloud = _stable_json_object(
        cloud_result_path,
        "accepted repair cloud result",
    )
    cloud_sha256 = sha256_bytes(cloud_payload)
    candidate_inputs = _mapping(
        _mapping(acceptance.get("inputs"), "acceptance inputs").get("candidate"),
        "acceptance candidate inputs",
    )
    if (
        training_sha256 != _ACCEPTED_TRAINING_REPORT_SHA256
        or cloud_sha256 != _ACCEPTED_CLOUD_RESULT_SHA256
        or candidate_inputs.get("training_report_sha256") != training_sha256
        or candidate_inputs.get("cloud_result_sha256") != cloud_sha256
    ):
        raise TrainingContinuationError("accepted repair report/cloud hashes differ from acceptance")
    schedule = _mapping(training.get("schedule"), "accepted repair schedule")
    checkpoint = _mapping(
        training.get("checkpoint_state"),
        "accepted repair checkpoint state",
    )
    final_model = _mapping(
        training.get("final_model"),
        "accepted repair final model",
    )
    final_inventory = _mapping(
        final_model.get("inventory"),
        "accepted repair final inventory",
    )
    if (
        training.get("training_completed") is not True
        or training.get("run_kind") != "improvement"
        or training.get("max_steps") != REPAIR_UPDATES
        or schedule.get("max_steps") != REPAIR_UPDATES
        or schedule.get("update_steps_per_epoch") != REPAIR_UPDATES
        or checkpoint.get("global_step") != REPAIR_UPDATES
        or checkpoint.get("last_checkpoint") != "checkpoint-146"
        or training.get("resume_from_checkpoint") is not None
        or final_model.get("directory") != "final-3015c63021cf53d8"
        or _mapping(
            final_model.get("reload_verification"),
            "accepted repair reload verification",
        ).get("status")
        != "passed"
        or final_inventory.get("tree_sha256") != _ACCEPTED_PARENT_TREE_SHA256
        or final_inventory.get("file_count") != 12
        or final_inventory.get("total_bytes") != 575466744
    ):
        raise TrainingContinuationError(
            "accepted repair training report does not prove the exact completed 146-step model"
        )
    if (
        cloud.get("kind") != "scriber_hf_marker_repair_result"
        or cloud.get("training_completed") is not True
        or cloud.get("final_model_directory") != "final-3015c63021cf53d8"
    ):
        raise TrainingContinuationError("accepted repair cloud result is incomplete")

    _, pilot_payload, pilot_raw = _stable_json_object(
        pilot_selection_report_path,
        "pilot selection report",
    )
    try:
        pilot = validate_pilot_selection_report(pilot_raw)
    except ValueError as error:
        raise TrainingContinuationError(f"pilot selection report is invalid: {error}") from error
    remediation_parent = _mapping(
        pilot.get("remediation_parent"),
        "pilot remediation parent",
    )
    outcomes = _mapping(pilot.get("fairness"), "pilot fairness").get("candidate_outcomes")
    if not isinstance(outcomes, list):
        raise TrainingContinuationError("pilot fairness candidate outcomes are missing")
    selected_outcome = next(
        (item for item in outcomes if isinstance(item, Mapping) and item.get("candidate_id") == "lr5e5"),
        None,
    )
    if (
        remediation_parent.get("candidate_id") != "lr5e5"
        or remediation_parent.get("scope") != "remediation_only_not_release"
        or selected_outcome is None
        or selected_outcome.get("best_checkpoint") != "checkpoint-700"
        or selected_outcome.get("best_checkpoint_step") != PILOT_SELECTED_UPDATES
    ):
        raise TrainingContinuationError("pilot selection does not bind checkpoint-700 as the repair parent")

    if parent_inventory_mode not in {"full", "remote_metadata"}:
        raise TrainingContinuationError("parent inventory mode must be full or remote_metadata")
    parent_root = Path(accepted_parent_dir).expanduser().resolve()
    remote_metadata = _verify_parent_metadata_without_large_remote_reads(parent_root)
    if parent_inventory_mode == "full":
        try:
            from .inference import _inventory_local_model_tree

            model_inventory = _inventory_local_model_tree(parent_root)
            directory_tree = checkpoint_tree_sha256(parent_root)
        except (OSError, TypeError, ValueError) as error:
            raise TrainingContinuationError(f"accepted repair model inventory is invalid: {error}") from error
        if (
            model_inventory.get("tree_sha256") != _ACCEPTED_PARENT_TREE_SHA256
            or model_inventory.get("file_count") != 12
            or model_inventory.get("total_bytes") != 575466744
            or directory_tree != _ACCEPTED_PARENT_DIRECTORY_TREE_SHA256
        ):
            raise TrainingContinuationError("accepted repair model differs from the bound parent inventory")
        parent_verification = {
            "verification_mode": "complete_local_content_hash",
            "large_remote_files_hashed": False,
            "model_tree_sha256": model_inventory["tree_sha256"],
            "directory_tree_sha256": directory_tree,
        }
    else:
        parent_verification = remote_metadata
    validate_continuation_arithmetic(
        start_updates=CONTINUATION_START_UPDATES,
        additional_updates=CONTINUATION_ADDITIONAL_UPDATES,
        target_updates=CONTINUATION_TARGET_UPDATES,
    )
    return {
        "schema_version": 1,
        "kind": "scriber_accepted_repair_parent_binding",
        "pilot_selected_updates": PILOT_SELECTED_UPDATES,
        "repair_updates": REPAIR_UPDATES,
        "cumulative_updates": CONTINUATION_START_UPDATES,
        "accepted_parent_model_tree_sha256": _ACCEPTED_PARENT_TREE_SHA256,
        "accepted_parent_directory_tree_sha256": (_ACCEPTED_PARENT_DIRECTORY_TREE_SHA256),
        "accepted_parent_file_count": 12,
        "accepted_parent_total_bytes": 575466744,
        "acceptance_report_sha256": "sha256:" + acceptance_sha256,
        "repair_manifest_sha256": "sha256:" + repair_sha256,
        "training_report_sha256": "sha256:" + training_sha256,
        "cloud_result_sha256": "sha256:" + cloud_sha256,
        "pilot_selection_report_sha256": "sha256:" + sha256_bytes(pilot_payload),
        "parent_initialization": "warm_start_weights_only",
        "replayed_updates": 0,
        "parent_verification": parent_verification,
    }


def _exact_int(value: object, expected: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise TrainingContinuationError(f"{label} must equal {expected}")


def normalize_continuation_training_order(
    value: Mapping[str, object],
    *,
    require_actual_epoch_evidence: bool = False,
) -> dict[str, object]:
    """Add the report lock projected from the actual one-epoch order evidence."""

    order = copy.deepcopy(dict(value))
    epoch_orders = order.get("effective_epoch_orders")
    if epoch_orders is None:
        if require_actual_epoch_evidence:
            raise TrainingContinuationError("continuation order requires exactly one effective epoch")
        lock = order.get("effective_order_lock")
        if not isinstance(lock, Mapping):
            raise TrainingContinuationError("continuation effective order lock must be an object")
        return order
    if not isinstance(epoch_orders, list) or len(epoch_orders) != 1:
        raise TrainingContinuationError("continuation order requires exactly one effective epoch")
    epoch = epoch_orders[0]
    if not isinstance(epoch, Mapping):
        raise TrainingContinuationError("continuation effective epoch evidence must be an object")
    if epoch.get("epoch") != 0:
        raise TrainingContinuationError("continuation effective order must bind epoch zero")
    if epoch.get("record_count") != CONTINUATION_TRAIN_EXAMPLES:
        raise TrainingContinuationError("continuation effective order record count is invalid")
    if epoch.get("filtered_plan_example_ids_sha256") != _FILTERED_PLAN_EFFECTIVE_ORDER_SHA256:
        raise TrainingContinuationError("continuation filtered plan order SHA-256 is invalid")
    if epoch.get("effective_example_ids_sha256") != _EFFECTIVE_ORDER_SHA256:
        raise TrainingContinuationError("continuation effective order SHA-256 is invalid")
    algorithm = order.get("bucket_algorithm")
    if algorithm != "fixed_window_length_descending_v1":
        raise TrainingContinuationError("continuation effective order algorithm is invalid")
    derived_lock = {
        "algorithm": algorithm,
        "example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
    }
    existing_lock = order.get("effective_order_lock")
    if existing_lock is not None and existing_lock != derived_lock:
        raise TrainingContinuationError("continuation effective order lock differs from epoch evidence")
    order["effective_order_lock"] = derived_lock
    return order


def _run_identity_bindings(
    report_bindings: Mapping[str, object],
) -> dict[str, object]:
    """Remove the report-only lock projection before hashing run identity."""

    bindings = copy.deepcopy(dict(report_bindings))
    order = bindings.get("training_order")
    if not isinstance(order, dict) or "effective_epoch_orders" not in order:
        return bindings
    normalized = normalize_continuation_training_order(
        order,
        require_actual_epoch_evidence=True,
    )
    if order.get("effective_order_lock") != normalized["effective_order_lock"]:
        raise TrainingContinuationError("continuation report-only effective order lock is invalid")
    order.pop("effective_order_lock", None)
    return bindings


def verify_continuation_training_report(
    report_path: str | Path,
    *,
    expected_parent_tree_sha256: str,
    expected_input_binding: Mapping[str, object] | None = None,
    output_root: str | Path | None = None,
) -> dict[str, object]:
    """Verify a completed continuation and derive its cumulative step count."""

    if not _PREFIXED_SHA256.fullmatch(expected_parent_tree_sha256):
        raise TrainingContinuationError("expected parent tree must be a sha256:-prefixed SHA-256")
    _, _, report = _stable_json_object(
        report_path,
        "continuation training report",
    )
    if report.get("schema_version") != 2 or report.get("base_model") != BASE_MODEL_ID:
        raise TrainingContinuationError("continuation base model or report schema is invalid")
    if report.get("base_revision") != BASE_MODEL_REVISION:
        raise TrainingContinuationError("continuation base revision is invalid")
    if report.get("training_completed") is not True or report.get("run_kind") != "improvement":
        raise TrainingContinuationError("continuation training report is not a completed improvement run")
    exact_top = {
        "seed": CONTINUATION_SEED,
        "method": "full_sft",
        "optimizer": "adamw_torch_fused",
        "learning_rate": 0.00001,
        "max_sequence_length": CONTINUATION_MAX_SEQUENCE_LENGTH,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "effective_batch_size": CONTINUATION_EFFECTIVE_BATCH_SIZE,
        "bf16": True,
        "sdpa": True,
        "early_stopping": False,
        "save_total_limit": 4,
    }
    for key, expected in exact_top.items():
        if report.get(key) != expected:
            raise TrainingContinuationError(f"continuation {key.replace('_', ' ')} differs from the exact contract")
    _exact_int(
        report.get("max_steps"),
        CONTINUATION_ADDITIONAL_UPDATES,
        "training max steps",
    )
    schedule = _mapping(report.get("schedule"), "continuation schedule")
    expected_schedule = {
        "declared_epochs": 1.0,
        "effective_examples_per_step": CONTINUATION_EFFECTIVE_BATCH_SIZE,
        "epoch_boundaries_preserved": True,
        "max_steps": CONTINUATION_ADDITIONAL_UPDATES,
        "schedule_source": "epochs",
        "train_batches_per_epoch": 2032,
        "update_steps_per_epoch": CONTINUATION_ADDITIONAL_UPDATES,
    }
    if schedule != expected_schedule:
        raise TrainingContinuationError("continuation schedule differs from the exact 254-update epoch")
    checkpoint = _mapping(
        report.get("checkpoint_state"),
        "continuation checkpoint state",
    )
    if checkpoint.get("global_step") != CONTINUATION_ADDITIONAL_UPDATES:
        raise TrainingContinuationError("continuation global step must equal 254")
    if checkpoint.get("last_checkpoint") != "checkpoint-254":
        raise TrainingContinuationError("continuation last checkpoint must be checkpoint-254")
    if report.get("resume_from_checkpoint") is not None:
        raise TrainingContinuationError("continuation must not resume or replay an earlier run")
    parent = _mapping(
        report.get("warm_start_parent_checkpoint"),
        "continuation warm-start parent",
    )
    if (
        parent.get("relationship") != "warm_start_parent_not_resume"
        or parent.get("tree_sha256") != expected_parent_tree_sha256
    ):
        raise TrainingContinuationError("continuation warm-start parent differs from the accepted model")
    for key, expected in (
        ("train_examples_loaded", CONTINUATION_TRAIN_EXAMPLES),
        ("train_examples_used", CONTINUATION_TRAIN_EXAMPLES),
        ("validation_examples_loaded", 431),
        ("validation_examples_used", 431),
    ):
        if report.get(key) != expected:
            raise TrainingContinuationError(f"continuation {key} must use exactly {expected}")
    if report.get("oversize_rejections") != {"train": 0, "validation": 0} or report.get("protection_rejections") != {
        "train": 0,
        "validation": 0,
    }:
        raise TrainingContinuationError("continuation requires zero protection rejections and zero oversize rejections")
    bindings = _mapping(report.get("bindings"), "continuation bindings")
    config = _mapping(bindings.get("config"), "continuation config binding")
    config_values = _mapping(
        config.get("values"),
        "continuation config values",
    )
    if config_values != _expected_continuation_config_values():
        if config_values.get("seed") != CONTINUATION_SEED:
            raise TrainingContinuationError("continuation config seed is invalid")
        raise TrainingContinuationError("continuation config values differ from the exact contract")
    dataset = _mapping(bindings.get("dataset"), "continuation dataset binding")
    dataset_manifest = _mapping(
        dataset.get("manifest"),
        "continuation dataset manifest binding",
    )
    train_binding = _mapping(
        dataset.get("train"),
        "continuation train binding",
    )
    validation_binding = _mapping(
        dataset.get("validation"),
        "continuation validation binding",
    )
    if train_binding.get("sha256") != "sha256:" + _FINAL_TRAIN_SHA256:
        raise TrainingContinuationError("continuation train SHA-256 is invalid")
    if validation_binding.get("sha256") != "sha256:" + _VALIDATION_SHA256:
        raise TrainingContinuationError("continuation validation SHA-256 is invalid")
    if (
        train_binding.get("total_records") != CONTINUATION_TRAIN_EXAMPLES
        or train_binding.get("selected_records") != CONTINUATION_TRAIN_EXAMPLES
        or validation_binding.get("total_records") != 431
        or validation_binding.get("selected_records") != 431
        or dataset_manifest.get("declared_train_sha256") != "sha256:" + _FINAL_TRAIN_SHA256
        or dataset_manifest.get("declared_validation_sha256") != "sha256:" + _VALIDATION_SHA256
    ):
        raise TrainingContinuationError("continuation dataset count or declared hash binding is invalid")
    order = _mapping(
        report.get("remediation_order"),
        "continuation explicit order",
    )
    bound_order = _mapping(
        bindings.get("training_order"),
        "continuation bound explicit order",
    )
    normalized_order = normalize_continuation_training_order(order)
    if (
        order != bound_order
        or order != normalized_order
        or order.get("implicit_trainer_shuffling") is not False
        or order.get("full_epoch_required") is not True
        or order.get("used_record_count") != CONTINUATION_TRAIN_EXAMPLES
    ):
        raise TrainingContinuationError("continuation did not use one exact explicit full-epoch order")
    if order.get("source_sha256") != _FINAL_TRAIN_SHA256:
        raise TrainingContinuationError("continuation explicit order train SHA-256 is invalid")
    if order.get("plan_sha256") != _ORDER_PLAN_SHA256:
        raise TrainingContinuationError("continuation order plan SHA-256 is invalid")
    effective_lock = _mapping(
        order.get("effective_order_lock"),
        "continuation effective order lock",
    )
    if effective_lock.get("example_ids_sha256") != _EFFECTIVE_ORDER_SHA256:
        raise TrainingContinuationError("continuation effective order SHA-256 is invalid")
    if (
        order.get("seed") != CONTINUATION_SEED
        or order.get("record_count") != CONTINUATION_TRAIN_EXAMPLES
        or order.get("safe_record_count") != CONTINUATION_TRAIN_EXAMPLES
        or order.get("selected_policy_rejected_record_count") != 0
        or order.get("sequence_rejected_record_count") != 0
        or order.get("effective_phase_counts")
        != {str(phase): count for phase, count in CONTINUATION_PHASE_TARGETS.items()}
    ):
        raise TrainingContinuationError("continuation explicit order counts or seed are invalid")
    policy = _mapping(
        bindings.get("protection_policy"),
        "continuation protection policy binding",
    )
    if policy.get("sha256") != "sha256:" + _POLICY_ARTIFACT_SHA256:
        raise TrainingContinuationError("continuation policy artifact SHA-256 is invalid")
    if policy.get("policy_sha256") != _POLICY_SELF_SHA256:
        raise TrainingContinuationError("continuation policy self SHA-256 is invalid")
    tokenizer = _mapping(
        bindings.get("tokenizer"),
        "continuation tokenizer binding",
    )
    if tokenizer.get("identity_sha256") != _TOKENIZER_IDENTITY_SHA256:
        raise TrainingContinuationError("continuation tokenizer identity is invalid")
    if (
        tokenizer.get("model_id") != BASE_MODEL_ID
        or tokenizer.get("revision") != BASE_MODEL_REVISION
        or _mapping(policy.get("tokenizer"), "policy tokenizer").get("identity_sha256") != _TOKENIZER_IDENTITY_SHA256
    ):
        raise TrainingContinuationError("continuation tokenizer/base revision binding is invalid")
    bound_parent = _mapping(
        bindings.get("parent_checkpoint"),
        "continuation bound parent",
    )
    if bound_parent != parent:
        raise TrainingContinuationError("continuation parent binding differs between report and run identity")
    application = _mapping(
        bindings.get("protection_application"),
        "continuation protection application",
    )
    for split, count in (("train", CONTINUATION_TRAIN_EXAMPLES), ("validation", 431)):
        split_application = _mapping(
            application.get(split),
            f"continuation {split} protection application",
        )
        if (
            split_application.get("loaded_record_count") != count
            or split_application.get("accepted_record_count") != count
            or split_application.get("rejected_record_count") != 0
        ):
            raise TrainingContinuationError("continuation protection application counts are invalid")
    if (
        report.get("warm_start_parent_checkpoint") != bound_parent
        or report.get("protection_policy") != policy
        or report.get("protection_application") != application
    ):
        raise TrainingContinuationError("continuation top-level inputs differ from run-identity bindings")
    if expected_input_binding is not None:
        expected_inputs = _mapping(
            dict(expected_input_binding),
            "expected continuation input binding",
        )
        comparisons = {
            "config_sha256": config.get("sha256"),
            "dataset_manifest_sha256": dataset_manifest.get("sha256"),
            "train_sha256": train_binding.get("sha256"),
            "validation_sha256": validation_binding.get("sha256"),
            "order_plan_sha256": "sha256:" + str(order.get("plan_sha256")),
            "effective_order_sha256": effective_lock.get("example_ids_sha256"),
            "protection_policy_artifact_sha256": policy.get("sha256"),
            "protection_policy_sha256": policy.get("policy_sha256"),
            "tokenizer_identity_sha256": tokenizer.get("identity_sha256"),
            "parent_directory_tree_sha256": parent.get("tree_sha256"),
            "base_model": report.get("base_model"),
            "base_revision": report.get("base_revision"),
        }
        for key, actual in comparisons.items():
            if expected_inputs.get(key) != actual:
                raise TrainingContinuationError(f"continuation report does not cross-bind bundle {key}")
        manifest_sha = expected_inputs.get("continuation_manifest_sha256")
        if not isinstance(manifest_sha, str) or not _PREFIXED_SHA256.fullmatch(manifest_sha):
            raise TrainingContinuationError("expected continuation manifest SHA-256 is invalid")
    identity_core = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": _run_identity_bindings(bindings),
    }
    expected_run_fingerprint = "sha256:" + sha256_bytes(canonical_json_bytes(identity_core))
    if report.get("run_fingerprint") != expected_run_fingerprint:
        raise TrainingContinuationError("continuation run fingerprint differs from its exact bindings")
    identity = {
        **identity_core,
        "run_fingerprint": expected_run_fingerprint,
    }
    identity_payload = canonical_json_bytes(identity)
    run_identity = _mapping(
        report.get("run_identity"),
        "continuation run identity",
    )
    if run_identity.get("sha256") != "sha256:" + sha256_bytes(identity_payload):
        raise TrainingContinuationError("continuation run identity hash differs from its content")
    if output_root is not None:
        root_candidate = Path(output_root).expanduser()
        if root_candidate.is_symlink():
            raise TrainingContinuationError(f"continuation output must not be a symlink: {root_candidate}")
        root = root_candidate.resolve()
        identity_path = root / "training" / "run-identity.json"
        physical_path, physical_payload, physical_identity = _stable_json_object(
            identity_path,
            "physical continuation run identity",
        )
        if (
            physical_path != identity_path
            or physical_payload != identity_payload
            or physical_identity != identity
            or Path(str(run_identity.get("path"))).resolve() != identity_path
        ):
            raise TrainingContinuationError("physical continuation run identity differs from the report")
    final_model = _mapping(report.get("final_model"), "continuation final model")
    final_directory = final_model.get("directory")
    if not isinstance(final_directory, str) or not _FINAL_DIRECTORY.fullmatch(final_directory):
        raise TrainingContinuationError("continuation final model directory is invalid")
    expected_final_directory = "final-" + expected_run_fingerprint.removeprefix("sha256:")[:16]
    if final_directory != expected_final_directory or final_model.get("run_fingerprint") != expected_run_fingerprint:
        raise TrainingContinuationError("continuation final model directory/run fingerprint is inconsistent")
    reload_verification = _mapping(
        final_model.get("reload_verification"),
        "continuation final reload verification",
    )
    expected_reload = {
        "loader": "transformers_local",
        "model_type": "gemma3_text",
        "parameter_count": 268098176,
        "protection_policy_sha256": ("sha256:" + _POLICY_ARTIFACT_SHA256),
        "run_fingerprint": expected_run_fingerprint,
        "schema_version": 1,
        "status": "passed",
        "tokenizer_class": ("transformers.models.gemma.tokenization_gemma_fast.GemmaTokenizerFast"),
    }
    if reload_verification != expected_reload:
        raise TrainingContinuationError("continuation final reload verification is invalid")
    inventory = _mapping(
        final_model.get("inventory"),
        "continuation final model inventory",
    )
    final_tree = inventory.get("tree_sha256")
    if not isinstance(final_tree, str) or not _PREFIXED_SHA256.fullmatch(final_tree):
        raise TrainingContinuationError("continuation final model tree SHA-256 is invalid")
    if final_tree == _ACCEPTED_PARENT_TREE_SHA256:
        raise TrainingContinuationError("continuation final model tree differs from accepted parent requirement")
    if (
        isinstance(inventory.get("file_count"), bool)
        or not isinstance(inventory.get("file_count"), int)
        or int(inventory["file_count"]) <= 0
        or isinstance(inventory.get("total_bytes"), bool)
        or not isinstance(inventory.get("total_bytes"), int)
        or int(inventory["total_bytes"]) <= 0
    ):
        raise TrainingContinuationError("continuation final model inventory counts are invalid")
    if output_root is not None:
        root = Path(output_root).expanduser().resolve()
        expected_final_path = root / "training" / final_directory
        if Path(str(final_model.get("path"))).resolve() != expected_final_path:
            raise TrainingContinuationError("continuation final model path is inconsistent")
    return {
        "schema_version": 1,
        "kind": "scriber_training_continuation_result",
        **validate_continuation_arithmetic(
            start_updates=CONTINUATION_START_UPDATES,
            additional_updates=CONTINUATION_ADDITIONAL_UPDATES,
            target_updates=CONTINUATION_TARGET_UPDATES,
        ),
        "cumulative_updates": CONTINUATION_TARGET_UPDATES,
        "final_model_directory": final_directory,
        "final_model_tree_sha256": final_tree,
    }

"""Fail-closed Hugging Face plan for five verified full-model variants."""

from __future__ import annotations

import base64
import csv
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from .hf_llama_cpp_toolchain import (
    CONFIG_DIGEST,
    INDEX_DIGEST,
    MANIFEST_DIGEST,
    POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD,
    REVIEWED_SCRIBER_GIT_HEAD,
    SELECTED_LAYERS,
    TAG,
    TOOLCHAIN_ATTEMPT,
    TOOLCHAIN_COMMIT,
    TOOLCHAIN_FLAVOR,
    TOOLCHAIN_HOURLY_RATES_USD,
    TOOLCHAIN_IMAGE,
    TOOLCHAIN_MAXIMUM_COST_USD,
    TOOLCHAIN_REMOTE_OUTPUT_URI,
    TOOLCHAIN_TIMEOUT_MINUTES,
    HfLlamaCppToolchainError,
    canonical_private_bucket_uri,
    reviewed_toolchain_build_binding,
    toolchain_materialization_binding,
    validate_no_writable_read_only_uri_overlap,
)
from .llama_cpp_bundle import (
    CUDA_HOST_DRIVER_SONAMES,
    LlamaCppBundleError,
    load_verified_llama_cpp_bundle,
    verify_llama_cpp_runtime_closure,
)
from .policy_artifact import (
    PolicyArtifactError,
    frozen_lexical_policy_binding,
    verify_frozen_lexical_policy_artifact,
)
from .quality_delta import QualityDeltaError, verify_quality_delta_report
from .variant_artifacts import (
    VariantArtifactError,
    verify_variant_artifact_manifest,
)

TOTAL_HF_BUDGET_USD = Decimal("13.500")
TOTAL_HF_BUDGET_AUTHORIZATION_SHA256 = "sha256:ea566717d68b8372a4c5f39c6279e24efa36f906c1dfe06c048339bd6ae86c0d"
REMAINING_HF_CREDITS_AUTHORIZATION_USD = Decimal("5.500")
FINAL_EVALUATION_COST_ANCHOR_USD = Decimal("0.893167")
QUANTIZATION_HOURLY_RATE_USD = Decimal("0.800")
FINAL_EVALUATION_HOURLY_RATES_USD = {
    "l4x1": QUANTIZATION_HOURLY_RATE_USD,
    "l40sx1": Decimal("1.800"),
}
# The verified full evaluation needed ~96 minutes for two 3,250-case lanes.
# Five fresh variant lanes plus conversion therefore need a bounded five-hour
# window; three hours would predictably risk losing a nearly complete job.
QUANTIZATION_TIMEOUT_MINUTES = 285
FINAL_EVALUATION_TIMEOUT_MINUTES = 120
ACTUAL_COST_MAX_AGE_SECONDS = 15 * 60
ACTUAL_COST_FUTURE_SKEW_SECONDS = 2 * 60
QUANTIZATION_FLAVOR = "l4x1"
QUANTIZATION_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
QUANTIZATION_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
QUANTIZATION_OUTPUT_MOUNT_RELATIVE = "attempt13/scriber-full-120k-v2-quantization-v2"
QUANTIZATION_OUTPUT_MOUNT_URI = (
    f"{QUANTIZATION_OUTPUT_BUCKET}/{QUANTIZATION_OUTPUT_MOUNT_RELATIVE}"
)
QUANTIZATION_OUTPUT_RELATIVE = f"{QUANTIZATION_OUTPUT_MOUNT_RELATIVE}/final"
QUANTIZATION_OUTPUT_ROOT = "/outputs/final"
FULL_MODEL_SOURCE_REMOTE_URI = (
    f"{QUANTIZATION_OUTPUT_BUCKET}/attempt11/scriber-full-120k-v2/"
    "training/final-84458762af7b00ac"
)
FULL_MODEL_SOURCE_TREE_SHA256 = (
    "sha256:5f6caf237fc3e1c3092363d6c17d405463f1cf99a84b0bbbec1720cf125fe3e2"
)
FULL_MODEL_WEIGHT_FILENAME = "model.safetensors"
FULL_MODEL_BF16_BASELINE_REMOTE_URI = (
    f"{QUANTIZATION_OUTPUT_BUCKET}/attempt12/"
    "scriber-full-120k-v2-final-evaluation-v1/final"
)
FULL_MODEL_BF16_BASELINE_ROOT = "/bf16-baseline"
QUANTIZATION_JOB_NAME = "scriber-full-120k-v2-quantized-evaluation"
QUANTIZATION_CAMPAIGN_LABEL = "campaign=full-120k-v2-quantized-eval"
QUANTIZATION_ROLE_LABEL = "role=quantized-evaluation"
QUANTIZATION_ATTEMPT = "v2"
QUANTIZATION_ATTEMPT_LABEL = f"attempt={QUANTIZATION_ATTEMPT}"
QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER = "__QUANTIZATION_V2_LAUNCH_CLAIM_OWNER__"
QUANTIZATION_DIRECT_DEPENDENCIES = {
    "accelerate": "1.14.0",
    "bitsandbytes": "0.49.2",
    "huggingface-hub": "0.36.0",
    "jsonschema": "4.26.0",
    "numpy": "2.5.1",
    "pyyaml": "6.0.3",
    "safetensors": "0.8.0",
    "sentencepiece": "0.2.2",
    "transformers": "4.57.6",
}
PRIVATE_BUCKET_PREFIX = "hf://buckets/Buttermilk03/"
_COST_ANCHOR_CONTINUATION_RESULT_SHA256 = "sha256:9e2755ac5411317da0bc70ddfb79716ef9b6704fbf923522f66421c26f343e7d"
_COST_ANCHOR_FIRST_FINAL_EVALUATION_PACKET_TREE_SHA256 = (
    "sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"
)
_COST_ANCHOR_FIRST_FINAL_EVALUATION_BUDGET_SHA256 = (
    "sha256:1bb8c91c0669cd86f58e0f6add1ace1cee85f32226667fd792206082d48ab1c9"
)
_FAILED_FINAL_EVALUATION_JOB_ID = "6a6ba10023ed89c748ec83f1"
_FAILED_FINAL_EVALUATION_RUNNING_SECONDS = 63
_SECOND_FAILED_FINAL_EVALUATION_JOB_ID = "6a6ba85b23ed89c748ec8491"
_SECOND_FAILED_FINAL_EVALUATION_RUNNING_SECONDS = 45
_SECOND_FAILED_FINAL_EVALUATION_PACKET_TREE_SHA256 = (
    "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"
)
_AUXILIARY_OBSERVED_INVENTORY_TREE_SHA256 = "sha256:da912da24b0861de5ebf0ea4a8040f583351966d5af8846a7df049120629113b"
_KNOWN_AUXILIARY_JOBS = (
    (
        "6a6ba991b36a6516e96a31bc",
        23,
        _SECOND_FAILED_FINAL_EVALUATION_PACKET_TREE_SHA256,
        _AUXILIARY_OBSERVED_INVENTORY_TREE_SHA256,
    ),
    (
        "6a6ba9d8b36a6516e96a31be",
        24,
        _SECOND_FAILED_FINAL_EVALUATION_PACKET_TREE_SHA256,
        _AUXILIARY_OBSERVED_INVENTORY_TREE_SHA256,
    ),
    (
        "6a6bae3cb36a6516e96a320c",
        44,
        "sha256:bc396bdcf45b57d8d12ed9d31a407f9862f96c33eca0f971984ccf2c192ca8ba",
        None,
    ),
)
_CANCELED_PRIMARY_FINAL_EVALUATION_JOB_ID = "6a6baf33b36a6516e96a3210"
_CANCELED_PRIMARY_FINAL_EVALUATION_PACKET_TREE_SHA256 = (
    "sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"
)
_CANCELED_PRIMARY_LAST_PRE_TERMINAL_RUNNING_SECONDS = 2810
_FAILED_AMENDMENT_FINAL_EVALUATION_JOB_ID = "6a6bc2edb36a6516e96a3347"
_FAILED_AMENDMENT_FINAL_EVALUATION_RUNNING_SECONDS = 78
_FAILED_AMENDMENT_FINAL_EVALUATION_PACKET_TREE_SHA256 = (
    "sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"
)
VARIANTS = ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
QUANTIZED_VARIANTS = ("int8", "int4_nf4", "Q8_0", "Q4_K_M")
REFERENCE_SUITES = ("ai_gold", "critical_regression", "regular_test")
FULL_MODEL_REFERENCE_FILES = {
    "ai_gold": (
        "ai_gold_automatic_test_challenge.references.jsonl",
        "ai_gold_automatic_test_challenge.references.manifest.json",
        1750,
        {"challenge": 750, "test": 1000},
    ),
    "critical_regression": (
        "critical_regression_as_challenge.references.jsonl",
        "critical_regression_as_challenge.references.manifest.json",
        500,
        {"challenge": 500},
    ),
    "regular_test": (
        "regular_test_1000.references.jsonl",
        "regular_test_1000.references.manifest.json",
        1000,
        {"test": 1000},
    ),
}
QUALITY_DELTA_BUDGETS = {
    "bf16": {
        "maximum_absolute_metric_drop": 0.0,
        "maximum_new_critical_errors": 0,
    },
    "int8": {
        "maximum_absolute_metric_drop": 0.005,
        "maximum_new_critical_errors": 0,
    },
    "int4_nf4": {
        "maximum_absolute_metric_drop": 0.01,
        "maximum_new_critical_errors": 0,
    },
    "Q8_0": {
        "maximum_absolute_metric_drop": 0.002,
        "maximum_new_critical_errors": 0,
    },
    "Q4_K_M": {
        "maximum_absolute_metric_drop": 0.01,
        "maximum_new_critical_errors": 0,
    },
}

_PREFIXED_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OWNER_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[^/\\]+$")
_DISTRIBUTION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_CUDA_DEVICE_COUNT = re.compile(
    r"found\s+([0-9]+)\s+CUDA devices?",
    re.IGNORECASE,
)
_CUDA_DEVICE = re.compile(
    r"Device\s+0:\s*([^,\r\n]+),\s*"
    r"compute capability\s+([0-9]+\.[0-9]+)",
    re.IGNORECASE,
)
_CUDA_OFFLOAD = re.compile(
    r"offloaded\s+([0-9]+)\s*/\s*([0-9]+)\s+"
    r"layers\s+to\s+GPU",
    re.IGNORECASE,
)
_CUDA_MODEL_BUFFER = re.compile(
    r"CUDA0\s+model buffer size\s*=",
    re.IGNORECASE,
)


class HfQuantizationError(ValueError):
    """A cloud quantization input, output, or cost binding is not trustworthy."""


@dataclass(frozen=True, slots=True)
class HfQuantizationPacket:
    """One immutable local packet which has not been uploaded or executed."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HfQuantizationError(f"{label} must be an object")
    return value


def _prefixed_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _PREFIXED_HASH.fullmatch(value) is None:
        raise HfQuantizationError(f"{label} must use sha256:<64 lowercase hex>")
    return value


def _raw_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _RAW_HASH.fullmatch(value) is None:
        raise HfQuantizationError(f"{label} must use 64 lowercase hex characters")
    return value


def _private_bucket_uri(value: object, label: str) -> str:
    try:
        return canonical_private_bucket_uri(value, label)
    except HfLlamaCppToolchainError as error:
        raise HfQuantizationError(str(error)) from error


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise HfQuantizationError(f"{label} is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def verify_llama_cpp_cuda_offload(
    stderr_tail: str,
    *,
    requested_gpu_layers: int,
    runtime_instance_id: str,
) -> dict[str, object]:
    """Derive fail-closed CUDA evidence from one live llama-server instance."""

    instance = _prefixed_hash(
        runtime_instance_id,
        "llama.cpp runtime instance",
    )
    if requested_gpu_layers != 999:
        raise HfQuantizationError("llama.cpp CUDA offload must request the reviewed 999-layer cap")
    if not isinstance(stderr_tail, str):
        raise HfQuantizationError("llama.cpp CUDA offload stderr must be text")
    normalized = stderr_tail.replace("\r\n", "\n").replace("\r", "\n")
    count_match = _CUDA_DEVICE_COUNT.search(normalized)
    device_match = _CUDA_DEVICE.search(normalized)
    offload_matches = list(_CUDA_OFFLOAD.finditer(normalized))
    if (
        count_match is None
        or device_match is None
        or not offload_matches
        or _CUDA_MODEL_BUFFER.search(normalized) is None
    ):
        raise HfQuantizationError("llama.cpp CUDA offload evidence is incomplete")
    device_count = int(count_match.group(1))
    gpu_name = device_match.group(1).strip()
    capability = device_match.group(2)
    offloaded_layers = int(offload_matches[-1].group(1))
    total_layers = int(offload_matches[-1].group(2))
    if (
        device_count != 1
        or gpu_name != "NVIDIA L4"
        or capability != "8.9"
        or offloaded_layers < 1
        or offloaded_layers != total_layers
    ):
        raise HfQuantizationError("llama.cpp CUDA offload is not a complete single-L4 offload")
    return _validate_llama_cpp_cuda_offload_evidence(
        {
            "schema_version": 1,
            "kind": "scriber_llama_cpp_cuda_offload_evidence",
            "status": "verified",
            "device": "cuda",
            "backend": "llama_cpp_cuda",
            "runtime_instance_id": instance,
            "requested_gpu_layers": requested_gpu_layers,
            "cuda_device_count": device_count,
            "gpu_name": gpu_name,
            "compute_capability": capability,
            "offloaded_layers": offloaded_layers,
            "total_layers": total_layers,
            "stderr_sha256": _sha256(normalized.encode("utf-8")),
        }
    )


def _validate_llama_cpp_cuda_offload_evidence(
    value: Mapping[str, object],
) -> dict[str, object]:
    evidence = dict(_mapping(value, "llama.cpp CUDA offload evidence"))
    if set(evidence) != {
        "schema_version",
        "kind",
        "status",
        "device",
        "backend",
        "runtime_instance_id",
        "requested_gpu_layers",
        "cuda_device_count",
        "gpu_name",
        "compute_capability",
        "offloaded_layers",
        "total_layers",
        "stderr_sha256",
    }:
        raise HfQuantizationError("llama.cpp CUDA offload evidence is not closed")
    offloaded = evidence.get("offloaded_layers")
    total = evidence.get("total_layers")
    capability = evidence.get("compute_capability")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "scriber_llama_cpp_cuda_offload_evidence"
        or evidence.get("status") != "verified"
        or evidence.get("device") != "cuda"
        or evidence.get("backend") != "llama_cpp_cuda"
        or evidence.get("requested_gpu_layers") != 999
        or evidence.get("cuda_device_count") != 1
        or evidence.get("gpu_name") != "NVIDIA L4"
        or capability != "8.9"
        or isinstance(offloaded, bool)
        or not isinstance(offloaded, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or offloaded < 1
        or offloaded != total
    ):
        raise HfQuantizationError("llama.cpp CUDA offload evidence is invalid")
    _prefixed_hash(
        evidence.get("runtime_instance_id"),
        "llama.cpp runtime instance",
    )
    _prefixed_hash(
        evidence.get("stderr_sha256"),
        "llama.cpp stderr evidence",
    )
    return evidence


def bind_llama_cpp_cuda_offload_evidence(
    offload_evidence: Mapping[str, object],
    *,
    variant: str,
    suite_id: str,
    model_sha256: str,
    llama_server_sha256: str,
    reference_sha256: str,
    prediction_sha256: str,
) -> dict[str, object]:
    """Bind one live offload proof to its exact model and evaluation lane."""

    offload = _validate_llama_cpp_cuda_offload_evidence(offload_evidence)
    if variant not in {"Q8_0", "Q4_K_M"}:
        raise HfQuantizationError("llama.cpp CUDA evidence has an invalid variant")
    if suite_id not in REFERENCE_SUITES:
        raise HfQuantizationError("llama.cpp CUDA evidence has an invalid suite")
    binding: dict[str, object] = {
        "variant": variant,
        "suite": suite_id,
        "model_sha256": _prefixed_hash(
            model_sha256,
            "GGUF model hash",
        ),
        "llama_server_sha256": _prefixed_hash(
            llama_server_sha256,
            "llama-server hash",
        ),
        "reference_sha256": _prefixed_hash(
            reference_sha256,
            "GGUF reference hash",
        ),
        "prediction_sha256": _prefixed_hash(
            prediction_sha256,
            "GGUF prediction hash",
        ),
        "offload": offload,
    }
    return {
        **binding,
        "binding_sha256": _sha256(_canonical_bytes(binding)),
    }


def _validate_llama_cpp_cuda_lane_evidence(
    value: Mapping[str, object],
    *,
    variant: str,
    suite_id: str,
    model_sha256: str | None = None,
    llama_server_sha256: str | None = None,
    reference_sha256: str | None = None,
    prediction_sha256: str | None = None,
) -> dict[str, object]:
    evidence = dict(_mapping(value, "llama.cpp CUDA lane evidence"))
    if set(evidence) != {
        "variant",
        "suite",
        "model_sha256",
        "llama_server_sha256",
        "reference_sha256",
        "prediction_sha256",
        "offload",
        "binding_sha256",
    }:
        raise HfQuantizationError("llama.cpp CUDA lane evidence is not closed")
    canonical = bind_llama_cpp_cuda_offload_evidence(
        _mapping(evidence["offload"], "llama.cpp offload proof"),
        variant=variant,
        suite_id=suite_id,
        model_sha256=(model_sha256 or str(evidence["model_sha256"])),
        llama_server_sha256=(llama_server_sha256 or str(evidence["llama_server_sha256"])),
        reference_sha256=(reference_sha256 or str(evidence["reference_sha256"])),
        prediction_sha256=(prediction_sha256 or str(evidence["prediction_sha256"])),
    )
    if evidence != canonical:
        raise HfQuantizationError("llama.cpp CUDA lane evidence differs from its exact binding")
    return evidence


def _billed_job_cost(
    *,
    hourly_rate_usd: Decimal,
    billed_minutes: int,
) -> Decimal:
    return (hourly_rate_usd * Decimal(billed_minutes) / Decimal(60)).quantize(
        Decimal("0.000001"),
        rounding=ROUND_HALF_UP,
    )


def hf_pre_final_evaluation_cost_anchor() -> dict[str, object]:
    """Return the immutable, hash-bound $0.893167 campaign cost anchor."""

    anchor = {
        "schema_version": 1,
        "kind": "scriber_hf_cost_anchor_before_final_evaluation",
        "historical_cost_usd": "0.593",
        "continuation_result_sha256": _COST_ANCHOR_CONTINUATION_RESULT_SHA256,
        "chain_jobs": [
            {
                "role": "training_continuation",
                "job_id": "6a6b8a78b36a6516e96a2fb1",
                "owner": "Buttermilk03",
                "terminal_status": "ERROR",
                "flavor": "a10g-small",
                "running_seconds": 912,
                "billed_minutes": 16,
                "hourly_rate_usd": "1.00",
                "actual_cost_usd": "0.266667",
                "packet_tree_sha256": ("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
            },
            {
                "role": "continuation_recovery",
                "job_id": "6a6b94e6b36a6516e96a307c",
                "owner": "Buttermilk03",
                "terminal_status": "ERROR",
                "flavor": "a10g-small",
                "running_seconds": 93,
                "billed_minutes": 2,
                "hourly_rate_usd": "1.00",
                "actual_cost_usd": "0.033333",
                "packet_tree_sha256": ("sha256:8f37fcfdf1ee7b542c2f078eee7dd57b2f9ea5a2573d265890d5c0d7d6ae31ea"),
            },
            {
                "role": "continuation_finalization",
                "job_id": "6a6b9ee723ed89c748ec83a1",
                "owner": "Buttermilk03",
                "terminal_status": "COMPLETED",
                "flavor": "cpu-basic",
                "running_seconds": 20,
                "billed_minutes": 1,
                "hourly_rate_usd": "0.01",
                "actual_cost_usd": "0.000167",
                "packet_tree_sha256": ("sha256:2db2a308dcfe5807209100fd823453619428435dbeff5b4679d5dc25dabd80a5"),
            },
        ],
        "actual_before_first_final_evaluation_usd": "0.893167",
        "first_final_evaluation_packet_tree_sha256": (_COST_ANCHOR_FIRST_FINAL_EVALUATION_PACKET_TREE_SHA256),
        "first_final_evaluation_budget_sha256": (_COST_ANCHOR_FIRST_FINAL_EVALUATION_BUDGET_SHA256),
    }
    chain_cost = sum(
        (Decimal(str(job["actual_cost_usd"])) for job in anchor["chain_jobs"]),
        Decimal("0"),
    )
    if Decimal(str(anchor["historical_cost_usd"])) + chain_cost != FINAL_EVALUATION_COST_ANCHOR_USD:
        raise HfQuantizationError("reviewed pre-evaluation cost anchor arithmetic differs")
    return anchor


def bind_hf_final_evaluation_cost_attempt(
    *,
    job_id: str,
    terminal_status: str,
    flavor: str,
    running_seconds: int | None,
    packet_tree_sha256: str,
    result_sha256: str | None,
    timeout_minutes: int = FINAL_EVALUATION_TIMEOUT_MINUTES,
    last_pre_terminal_running_seconds: int | None = None,
) -> dict[str, object]:
    """Bind one terminal L4 evaluation attempt without inventing unavailable cost."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or terminal_status not in {"ERROR", "COMPLETED", "CANCELED"}
        or flavor not in FINAL_EVALUATION_HOURLY_RATES_USD
        or isinstance(timeout_minutes, bool)
        or not isinstance(timeout_minutes, int)
        or timeout_minutes not in {120, 180}
    ):
        raise HfQuantizationError(
            "final-evaluation attempt must be a terminal ERROR/COMPLETED/CANCELED l4x1 job "
            "with a reviewed 120- or 180-minute timeout on L4 or L40S"
        )
    packet_hash = _prefixed_hash(
        packet_tree_sha256,
        "final-evaluation attempt packet tree",
    )
    duration_available = running_seconds is not None
    primary_cancellation_identity = (
        job_id == _CANCELED_PRIMARY_FINAL_EVALUATION_JOB_ID
        or packet_hash == _CANCELED_PRIMARY_FINAL_EVALUATION_PACKET_TREE_SHA256
    )
    if primary_cancellation_identity:
        if (
            job_id != _CANCELED_PRIMARY_FINAL_EVALUATION_JOB_ID
            or terminal_status != "CANCELED"
            or packet_hash != _CANCELED_PRIMARY_FINAL_EVALUATION_PACKET_TREE_SHA256
            or running_seconds is not None
            or timeout_minutes != FINAL_EVALUATION_TIMEOUT_MINUTES
            or last_pre_terminal_running_seconds != _CANCELED_PRIMARY_LAST_PRE_TERMINAL_RUNNING_SECONDS
            or result_sha256 is not None
        ):
            raise HfQuantizationError(
                "the duration-unavailable primary cancellation must retain its exact terminal "
                "identity and full-timeout reservation"
            )
    elif (
        isinstance(running_seconds, bool)
        or not isinstance(running_seconds, int)
        or running_seconds < 0
        or running_seconds > timeout_minutes * 60
        or last_pre_terminal_running_seconds is not None
        or (terminal_status == "COMPLETED" and running_seconds == 0)
    ):
        raise HfQuantizationError(
            "duration-available final-evaluation attempts require exact runtime within their reviewed timeout"
        )
    if (
        job_id == _FAILED_FINAL_EVALUATION_JOB_ID
        or packet_hash == _COST_ANCHOR_FIRST_FINAL_EVALUATION_PACKET_TREE_SHA256
    ) and (
        job_id != _FAILED_FINAL_EVALUATION_JOB_ID
        or terminal_status != "ERROR"
        or flavor != QUANTIZATION_FLAVOR
        or running_seconds != _FAILED_FINAL_EVALUATION_RUNNING_SECONDS
        or timeout_minutes != FINAL_EVALUATION_TIMEOUT_MINUTES
        or packet_hash != _COST_ANCHOR_FIRST_FINAL_EVALUATION_PACKET_TREE_SHA256
        or result_sha256 is not None
    ):
        raise HfQuantizationError(
            "known first final-evaluation attempt must remain ERROR with its exact 63-second binding"
        )
    if job_id == _SECOND_FAILED_FINAL_EVALUATION_JOB_ID and (
        terminal_status != "ERROR"
        or flavor != QUANTIZATION_FLAVOR
        or running_seconds != _SECOND_FAILED_FINAL_EVALUATION_RUNNING_SECONDS
        or timeout_minutes != FINAL_EVALUATION_TIMEOUT_MINUTES
        or packet_hash != _SECOND_FAILED_FINAL_EVALUATION_PACKET_TREE_SHA256
        or result_sha256 is not None
    ):
        raise HfQuantizationError(
            "known second final-evaluation attempt must remain ERROR with its exact 45-second binding"
        )
    if (
        job_id == _FAILED_AMENDMENT_FINAL_EVALUATION_JOB_ID
        or packet_hash == _FAILED_AMENDMENT_FINAL_EVALUATION_PACKET_TREE_SHA256
    ) and (
        job_id != _FAILED_AMENDMENT_FINAL_EVALUATION_JOB_ID
        or terminal_status != "ERROR"
        or flavor != QUANTIZATION_FLAVOR
        or running_seconds != _FAILED_AMENDMENT_FINAL_EVALUATION_RUNNING_SECONDS
        or timeout_minutes != 180
        or packet_hash != _FAILED_AMENDMENT_FINAL_EVALUATION_PACKET_TREE_SHA256
        or result_sha256 is not None
    ):
        raise HfQuantizationError("known failed amendment must remain ERROR with its exact 78-second terminal binding")
    if terminal_status == "COMPLETED":
        result_hash: str | None = _prefixed_hash(
            result_sha256,
            "completed final-evaluation result",
        )
    elif result_sha256 is not None:
        raise HfQuantizationError("non-completed final-evaluation attempt cannot claim a completed result")
    else:
        result_hash = None
    if duration_available:
        assert isinstance(running_seconds, int)
        billed_minutes: int | None = (running_seconds + 59) // 60
        actual_cost: Decimal | None = _billed_job_cost(
            hourly_rate_usd=FINAL_EVALUATION_HOURLY_RATES_USD[flavor],
            billed_minutes=billed_minutes,
        )
        reserved_minutes = 0
        reserved_cost = Decimal("0")
        cost_basis = "observed_minute_billing"
        accounted_cost = actual_cost
    else:
        billed_minutes = None
        actual_cost = None
        reserved_minutes = timeout_minutes
        reserved_cost = _billed_job_cost(
            hourly_rate_usd=FINAL_EVALUATION_HOURLY_RATES_USD[flavor],
            billed_minutes=reserved_minutes,
        )
        cost_basis = "full_timeout_reservation"
        accounted_cost = reserved_cost
    return {
        "role": "final_evaluation",
        "job_id": job_id,
        "owner": "Buttermilk03",
        "terminal_status": terminal_status,
        "flavor": flavor,
        "timeout_minutes": timeout_minutes,
        "terminal_duration_available": duration_available,
        "running_seconds": running_seconds,
        "billed_minutes": billed_minutes,
        "hourly_rate_usd": format(
            FINAL_EVALUATION_HOURLY_RATES_USD[flavor],
            ".3f",
        ),
        "actual_cost_usd": None if actual_cost is None else format(actual_cost, ".6f"),
        "cost_basis": cost_basis,
        "reserved_minutes": reserved_minutes,
        "reserved_cost_usd": format(reserved_cost, ".6f"),
        "accounted_cost_usd": format(accounted_cost, ".6f"),
        "last_pre_terminal_running_seconds": last_pre_terminal_running_seconds,
        "packet_tree_sha256": packet_hash,
        "result_sha256": result_hash,
    }


def bind_hf_auxiliary_cost_job(
    *,
    job_id: str,
    role: str,
    terminal_status: str,
    flavor: str,
    running_seconds: int,
    packet_tree_sha256: str,
    observed_inventory_tree_sha256: str | None,
    training_steps_executed: int,
    evaluation_reports_written: int,
    prediction_records_written: int,
) -> dict[str, object]:
    """Bind one terminal auxiliary job without retagging it as evaluation."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or role
        not in {
            "packet_mount_inventory",
            "packet_cross_platform_self_verify",
            "toolchain_git_status_probe",
            "toolchain_cuda_library_probe",
            "toolchain_materialization",
            "full_training",
        }
        or isinstance(running_seconds, bool)
        or not isinstance(running_seconds, int)
        or running_seconds < 0
        or isinstance(training_steps_executed, bool)
        or not isinstance(training_steps_executed, int)
        or training_steps_executed < 0
        or isinstance(evaluation_reports_written, bool)
        or not isinstance(evaluation_reports_written, int)
        or evaluation_reports_written < 0
        or isinstance(prediction_records_written, bool)
        or not isinstance(prediction_records_written, int)
        or prediction_records_written < 0
    ):
        raise HfQuantizationError("auxiliary cost job identity or zero-side-effect counters are invalid")
    packet_hash = _prefixed_hash(
        packet_tree_sha256,
        "auxiliary job packet tree",
    )
    if observed_inventory_tree_sha256 is None:
        observed_hash = None
    else:
        observed_hash = _prefixed_hash(
            observed_inventory_tree_sha256,
            "auxiliary observed inventory tree",
        )
    cpu_diagnostic = role in {
        "packet_mount_inventory",
        "packet_cross_platform_self_verify",
    }
    toolchain_materialization = role == "toolchain_materialization"
    git_status_probe = role == "toolchain_git_status_probe"
    cuda_library_probe = role == "toolchain_cuda_library_probe"
    full_training = role == "full_training"
    if cpu_diagnostic:
        valid_role_state = (
            terminal_status == "COMPLETED"
            and flavor == "cpu-basic"
            and 1 <= running_seconds <= 10 * 60
            and training_steps_executed == 0
            and evaluation_reports_written == 0
            and prediction_records_written == 0
        )
        hourly_rate = Decimal("0.010")
    elif toolchain_materialization:
        canceled_runtime_is_bounded = (
            terminal_status == "CANCELED" and running_seconds <= 30 * 60
        )
        valid_role_state = (
            terminal_status in {"ERROR", "COMPLETED", "CANCELED"}
            and flavor in TOOLCHAIN_HOURLY_RATES_USD
            and (
                canceled_runtime_is_bounded
                or (
                    terminal_status in {"ERROR", "COMPLETED"}
                    and 1 <= running_seconds <= 30 * 60
                )
            )
            and (
                (terminal_status == "COMPLETED" and observed_hash is not None)
                or (
                    terminal_status in {"ERROR", "CANCELED"}
                    and observed_hash is None
                )
            )
            and training_steps_executed == 0
            and evaluation_reports_written == 0
            and prediction_records_written == 0
        )
        hourly_rate = TOOLCHAIN_HOURLY_RATES_USD[flavor]
    elif git_status_probe:
        valid_role_state = (
            terminal_status in {"ERROR", "COMPLETED"}
            and flavor == "cpu-basic"
            and 1 <= running_seconds <= 5 * 60
            and observed_hash is None
            and training_steps_executed == 0
            and evaluation_reports_written == 0
            and prediction_records_written == 0
        )
        hourly_rate = Decimal("0.010")
    elif cuda_library_probe:
        valid_role_state = (
            terminal_status == "COMPLETED"
            and flavor == "cpu-basic"
            and 0 <= running_seconds <= 5 * 60
            and observed_hash is None
            and training_steps_executed == 0
            and evaluation_reports_written == 0
            and prediction_records_written == 0
        )
        hourly_rate = Decimal("0.010")
    elif full_training:
        valid_role_state = (
            terminal_status in {"CANCELED", "COMPLETED"}
            and flavor == "l40sx1"
            and 1 <= running_seconds <= 6 * 60 * 60
            and training_steps_executed > 0
            and evaluation_reports_written == 0
            and prediction_records_written == 0
            and (
                (terminal_status == "COMPLETED" and observed_hash is not None)
                or (terminal_status == "CANCELED" and observed_hash is None)
            )
        )
        hourly_rate = Decimal("1.800")
    if not valid_role_state:
        raise HfQuantizationError("auxiliary job role, terminal status, flavor, runtime, or output claim is invalid")
    known = {
        known_job_id: (seconds, known_packet_hash, known_observed_hash)
        for known_job_id, seconds, known_packet_hash, known_observed_hash in _KNOWN_AUXILIARY_JOBS
    }
    if job_id in known and (
        role != "packet_mount_inventory"
        or running_seconds != known[job_id][0]
        or packet_hash != known[job_id][1]
        or observed_hash != known[job_id][2]
    ):
        raise HfQuantizationError("known packet-mount diagnostic differs from its terminal CPU binding")
    billed_minutes = (running_seconds + 59) // 60
    cost = _billed_job_cost(
        hourly_rate_usd=hourly_rate,
        billed_minutes=billed_minutes,
    )
    return {
        "role": role,
        "job_id": job_id,
        "owner": "Buttermilk03",
        "terminal_status": terminal_status,
        "flavor": flavor,
        "running_seconds": running_seconds,
        "billed_minutes": billed_minutes,
        "hourly_rate_usd": format(hourly_rate, ".3f"),
        "actual_cost_usd": format(cost, ".6f"),
        "packet_tree_sha256": packet_hash,
        "observed_inventory_tree_sha256": observed_hash,
        "training_steps_executed": training_steps_executed,
        "evaluation_reports_written": evaluation_reports_written,
        "prediction_records_written": prediction_records_written,
    }


def build_hf_actual_cost_evidence(
    *,
    retrieved_at_utc: str,
    final_evaluation_attempts: Sequence[Mapping[str, object]],
    auxiliary_jobs: Sequence[Mapping[str, object]],
    no_other_campaign_jobs_running: bool,
) -> dict[str, object]:
    """Build canonical local evidence from already observed terminal attempts."""

    if not isinstance(retrieved_at_utc, str) or _UTC_TIMESTAMP.fullmatch(retrieved_at_utc) is None:
        raise HfQuantizationError("HF actual-cost evidence timestamp is invalid")
    try:
        datetime.strptime(retrieved_at_utc, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise HfQuantizationError("HF actual-cost evidence timestamp is invalid") from error
    if no_other_campaign_jobs_running is not True:
        raise HfQuantizationError("active or unaccounted campaign jobs make cost evidence incomplete")
    if (
        isinstance(final_evaluation_attempts, str | bytes)
        or not isinstance(final_evaluation_attempts, Sequence)
        or not final_evaluation_attempts
    ):
        raise HfQuantizationError("at least one terminal final-evaluation attempt is required")
    normalized: list[dict[str, object]] = []
    seen_job_ids: set[str] = set()
    for raw_attempt in final_evaluation_attempts:
        attempt = dict(_mapping(raw_attempt, "final-evaluation cost attempt"))
        bound = bind_hf_final_evaluation_cost_attempt(
            job_id=str(attempt.get("job_id", "")),
            terminal_status=str(attempt.get("terminal_status", "")),
            flavor=str(attempt.get("flavor", "")),
            running_seconds=attempt.get("running_seconds"),  # type: ignore[arg-type]
            packet_tree_sha256=str(attempt.get("packet_tree_sha256", "")),
            result_sha256=attempt.get("result_sha256"),  # type: ignore[arg-type]
            timeout_minutes=attempt.get("timeout_minutes"),  # type: ignore[arg-type]
            last_pre_terminal_running_seconds=attempt.get("last_pre_terminal_running_seconds"),  # type: ignore[arg-type]
        )
        if attempt != bound:
            raise HfQuantizationError("final-evaluation cost attempt is not canonical or is not closed")
        job_id = str(bound["job_id"])
        if job_id in seen_job_ids:
            raise HfQuantizationError("final-evaluation job IDs must be unique")
        seen_job_ids.add(job_id)
        normalized.append(bound)
    if [attempt["job_id"] for attempt in normalized[:4]] != [
        _FAILED_FINAL_EVALUATION_JOB_ID,
        _SECOND_FAILED_FINAL_EVALUATION_JOB_ID,
        _CANCELED_PRIMARY_FINAL_EVALUATION_JOB_ID,
        _FAILED_AMENDMENT_FINAL_EVALUATION_JOB_ID,
    ]:
        raise HfQuantizationError(
            "cost evidence must begin with the exact two preflight failures, canceled primary, and failed amendment"
        )
    if not any(
        attempt["terminal_status"] == "COMPLETED"
        for attempt in normalized
    ):
        raise HfQuantizationError(
            "cost evidence requires at least one completed final-evaluation attempt"
        )
    if (
        isinstance(auxiliary_jobs, str | bytes)
        or not isinstance(auxiliary_jobs, Sequence)
        or len(auxiliary_jobs) < len(_KNOWN_AUXILIARY_JOBS)
    ):
        raise HfQuantizationError("all known terminal auxiliary jobs are required")
    normalized_auxiliary: list[dict[str, object]] = []
    for raw_job in auxiliary_jobs:
        job = dict(_mapping(raw_job, "auxiliary cost job"))
        bound_job = bind_hf_auxiliary_cost_job(
            job_id=str(job.get("job_id", "")),
            role=str(job.get("role", "")),
            terminal_status=str(job.get("terminal_status", "")),
            flavor=str(job.get("flavor", "")),
            running_seconds=job.get("running_seconds"),  # type: ignore[arg-type]
            packet_tree_sha256=str(job.get("packet_tree_sha256", "")),
            observed_inventory_tree_sha256=job.get("observed_inventory_tree_sha256"),  # type: ignore[arg-type]
            training_steps_executed=job.get("training_steps_executed"),  # type: ignore[arg-type]
            evaluation_reports_written=job.get("evaluation_reports_written"),  # type: ignore[arg-type]
            prediction_records_written=job.get("prediction_records_written"),  # type: ignore[arg-type]
        )
        if job != bound_job:
            raise HfQuantizationError("auxiliary cost job is not canonical or is not closed")
        job_id = str(bound_job["job_id"])
        if job_id in seen_job_ids:
            raise HfQuantizationError("campaign cost job IDs must be unique")
        seen_job_ids.add(job_id)
        normalized_auxiliary.append(bound_job)
    if [job["job_id"] for job in normalized_auxiliary[:3]] != [job_id for job_id, _, _, _ in _KNOWN_AUXILIARY_JOBS]:
        raise HfQuantizationError("cost evidence must include all three known CPU diagnostics")
    anchor = hf_pre_final_evaluation_cost_anchor()
    return {
        "schema_version": 3,
        "kind": "scriber_hf_actual_cost_evidence",
        "currency": "USD",
        "billing_quantum_seconds": 60,
        "pricing_source": "https://huggingface.co/docs/hub/jobs-pricing",
        "retrieved_at_utc": retrieved_at_utc,
        "cost_anchor": anchor,
        "cost_anchor_sha256": _sha256(_canonical_bytes(anchor)),
        "attempt_sequence_complete": True,
        "no_other_campaign_jobs_running": True,
        "final_evaluation_attempts": normalized,
        "auxiliary_jobs": normalized_auxiliary,
    }


def write_hf_actual_cost_evidence(
    path: str | Path,
    *,
    retrieved_at_utc: str,
    final_evaluation_attempts: Sequence[Mapping[str, object]],
    auxiliary_jobs: Sequence[Mapping[str, object]],
    no_other_campaign_jobs_running: bool,
) -> Path:
    """Write one immutable canonical cost-evidence document locally."""

    evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc=retrieved_at_utc,
        final_evaluation_attempts=final_evaluation_attempts,
        auxiliary_jobs=auxiliary_jobs,
        no_other_campaign_jobs_running=no_other_campaign_jobs_running,
    )
    return _write_immutable_json(path, evidence)


def build_hf_quantization_budget(
    evidence: Mapping[str, object],
    *,
    evidence_sha256: str,
    expected_continuation_result_sha256: str,
    expected_final_evaluation_result_sha256: str,
    expected_toolchain_materialization: Mapping[str, object],
    use_remaining_credit_authorization: bool = False,
) -> dict[str, object]:
    """Rebind every terminal eval attempt, then reserve one 180-minute L4 job."""

    actual = dict(_mapping(evidence, "HF actual-cost evidence"))
    expected_fields = {
        "schema_version",
        "kind",
        "currency",
        "billing_quantum_seconds",
        "pricing_source",
        "retrieved_at_utc",
        "cost_anchor",
        "cost_anchor_sha256",
        "attempt_sequence_complete",
        "no_other_campaign_jobs_running",
        "final_evaluation_attempts",
        "auxiliary_jobs",
    }
    if set(actual) != expected_fields:
        raise HfQuantizationError("HF actual-cost evidence is not closed")
    bound_hash = _prefixed_hash(
        evidence_sha256,
        "HF actual-cost evidence hash",
    )
    if _sha256(_canonical_bytes(actual)) != bound_hash:
        raise HfQuantizationError("HF actual-cost evidence hash differs")
    if (
        actual.get("schema_version") != 3
        or actual.get("kind") != "scriber_hf_actual_cost_evidence"
        or actual.get("currency") != "USD"
        or actual.get("billing_quantum_seconds") != 60
        or actual.get("pricing_source") != "https://huggingface.co/docs/hub/jobs-pricing"
        or not isinstance(actual.get("retrieved_at_utc"), str)
        or _UTC_TIMESTAMP.fullmatch(str(actual["retrieved_at_utc"])) is None
        or actual.get("attempt_sequence_complete") is not True
        or actual.get("no_other_campaign_jobs_running") is not True
    ):
        raise HfQuantizationError("HF actual-cost evidence identity is invalid")
    try:
        datetime.strptime(
            str(actual["retrieved_at_utc"]),
            "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError as error:
        raise HfQuantizationError("HF actual-cost evidence timestamp is invalid") from error
    anchor = dict(_mapping(actual.get("cost_anchor"), "pre-evaluation cost anchor"))
    expected_anchor = hf_pre_final_evaluation_cost_anchor()
    expected_anchor_hash = _sha256(_canonical_bytes(expected_anchor))
    if (
        anchor != expected_anchor
        or actual.get("cost_anchor_sha256") != expected_anchor_hash
        or _prefixed_hash(
            expected_continuation_result_sha256,
            "continuation result hash",
        )
        != anchor.get("continuation_result_sha256")
    ):
        raise HfQuantizationError("pre-evaluation cost anchor differs from its reviewed hash binding")
    expected_final_result = _prefixed_hash(
        expected_final_evaluation_result_sha256,
        "final-evaluation result hash",
    )
    attempts = actual.get("final_evaluation_attempts")
    auxiliary_jobs = actual.get("auxiliary_jobs")
    if not isinstance(attempts, list) or not attempts or not isinstance(auxiliary_jobs, list):
        raise HfQuantizationError("HF actual-cost evidence has incomplete terminal job lists")
    rebuilt_evidence = build_hf_actual_cost_evidence(
        retrieved_at_utc=str(actual["retrieved_at_utc"]),
        final_evaluation_attempts=attempts,
        auxiliary_jobs=auxiliary_jobs,
        no_other_campaign_jobs_running=True,
    )
    if rebuilt_evidence != actual:
        raise HfQuantizationError("HF actual-cost evidence differs from its canonical terminal-job binding")
    normalized_attempts = list(rebuilt_evidence["final_evaluation_attempts"])
    normalized_auxiliary = list(rebuilt_evidence["auxiliary_jobs"])
    completed = [attempt for attempt in normalized_attempts if attempt["terminal_status"] == "COMPLETED"]
    if not completed or completed[-1]["result_sha256"] != expected_final_result:
        raise HfQuantizationError(
            "latest completed cost attempt must bind the selected final evaluation"
        )
    materialization = _validate_toolchain_materialization_binding(
        expected_toolchain_materialization,
    )
    toolchain_attempts = [job for job in normalized_auxiliary if job["role"] == "toolchain_materialization"]
    completed_toolchains = [job for job in toolchain_attempts if job["terminal_status"] == "COMPLETED"]
    if (
        len(completed_toolchains) != 1
        or not toolchain_attempts
        or toolchain_attempts[-1]["terminal_status"] != "COMPLETED"
        or completed_toolchains[0]["packet_tree_sha256"] != materialization["packet_tree_sha256"]
        or completed_toolchains[0]["observed_inventory_tree_sha256"] != materialization["bundle_payload_tree_sha256"]
        or completed_toolchains[0]["flavor"] != materialization["flavor"]
    ):
        raise HfQuantizationError(
            "quantization requires one final completed toolchain job bound to the exact packet and bundle inventory"
        )
    external_provenance = materialization.get("external_provenance")
    if use_remaining_credit_authorization and (
        not isinstance(external_provenance, Mapping)
        or external_provenance.get("producer_job_id")
        != completed_toolchains[0]["job_id"]
        or external_provenance.get("result_sha256")
        != materialization["result_sha256"]
    ):
        raise HfQuantizationError(
            "full-model quantization requires externally bound v19 result, job, and claim provenance"
        )
    attempt_actual_cost = sum(
        (
            Decimal(str(attempt["actual_cost_usd"]))
            for attempt in normalized_attempts
            if attempt["actual_cost_usd"] is not None
        ),
        Decimal("0"),
    )
    attempt_reserved_cost = sum(
        (Decimal(str(attempt["reserved_cost_usd"])) for attempt in normalized_attempts),
        Decimal("0"),
    )
    attempt_accounted_cost = sum(
        (Decimal(str(attempt["accounted_cost_usd"])) for attempt in normalized_attempts),
        Decimal("0"),
    )
    auxiliary_cost = sum(
        (Decimal(str(job["actual_cost_usd"])) for job in normalized_auxiliary),
        Decimal("0"),
    )
    accounted_before = FINAL_EVALUATION_COST_ANCHOR_USD + attempt_accounted_cost + auxiliary_cost
    quantization_job = _billed_job_cost(
        hourly_rate_usd=QUANTIZATION_HOURLY_RATE_USD,
        billed_minutes=QUANTIZATION_TIMEOUT_MINUTES,
    )
    maximum = accounted_before + quantization_job
    reconstructed_post_authorization_actual = sum(
        (
            Decimal(str(job["actual_cost_usd"]))
            for job in normalized_auxiliary
            if job["role"]
            in {
                "toolchain_git_status_probe",
                "toolchain_cuda_library_probe",
                "toolchain_materialization",
            }
        ),
        Decimal("0"),
    )
    post_authorization_actual = max(
        reconstructed_post_authorization_actual,
        POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD + TOOLCHAIN_MAXIMUM_COST_USD,
    )
    remaining_credit_scope_maximum = post_authorization_actual + quantization_job
    if use_remaining_credit_authorization:
        if remaining_credit_scope_maximum >= REMAINING_HF_CREDITS_AUTHORIZATION_USD:
            raise HfQuantizationError(
                "post-authorization toolchain spend plus the quantization reservation "
                "must remain strictly below the user-reported remaining HF credits"
            )
    elif maximum >= TOTAL_HF_BUDGET_USD:
        raise HfQuantizationError(
            f"maximum aggregate HF cost ${maximum:.6f} must remain strictly below ${TOTAL_HF_BUDGET_USD:.3f}"
        )
    failed_count = sum(attempt["terminal_status"] == "ERROR" for attempt in normalized_attempts)
    billed_minutes = sum(
        int(attempt["billed_minutes"]) for attempt in normalized_attempts if attempt["billed_minutes"] is not None
    )
    auxiliary_billed_minutes = sum(int(job["billed_minutes"]) for job in normalized_auxiliary)
    chain_jobs = anchor.get("chain_jobs")
    if not isinstance(chain_jobs, list):
        raise HfQuantizationError("pre-evaluation cost anchor has no complete chain-job inventory")
    accounted_campaign_job_ids = sorted(
        {
            *(str(job.get("job_id", "")) for job in chain_jobs if isinstance(job, Mapping)),
            *(str(attempt["job_id"]) for attempt in normalized_attempts),
            *(str(job["job_id"]) for job in normalized_auxiliary),
        }
    )
    if (
        len(accounted_campaign_job_ids)
        != len(chain_jobs) + len(normalized_attempts) + len(normalized_auxiliary)
        or any(_JOB_ID.fullmatch(job_id) is None for job_id in accounted_campaign_job_ids)
    ):
        raise HfQuantizationError("accounted campaign job IDs are incomplete or duplicated")
    accounted_campaign_job_ids_sha256 = _sha256(
        _canonical_bytes(accounted_campaign_job_ids)
    )
    budget = {
        "total_budget_usd": format(TOTAL_HF_BUDGET_USD, ".3f"),
        "total_budget_authorization_sha256": TOTAL_HF_BUDGET_AUTHORIZATION_SHA256,
        "actual_cost_evidence_sha256": bound_hash,
        "actual_cost_as_of_utc": actual["retrieved_at_utc"],
        "accounted_campaign_job_ids": accounted_campaign_job_ids,
        "accounted_campaign_job_ids_sha256": accounted_campaign_job_ids_sha256,
        "cost_anchor_sha256": expected_anchor_hash,
        "actual_before_first_final_evaluation_usd": format(
            FINAL_EVALUATION_COST_ANCHOR_USD,
            ".6f",
        ),
        "final_evaluation_attempt_count": len(normalized_attempts),
        "final_evaluation_error_count": failed_count,
        "final_evaluation_canceled_count": sum(
            attempt["terminal_status"] == "CANCELED" for attempt in normalized_attempts
        ),
        "final_evaluation_completed_count": len(completed),
        "final_evaluation_observed_billed_minutes": billed_minutes,
        "final_evaluation_observed_actual_cost_usd": format(
            attempt_actual_cost,
            ".6f",
        ),
        "final_evaluation_unknown_cost_reservation_usd": format(
            attempt_reserved_cost,
            ".6f",
        ),
        "final_evaluation_accounted_cost_usd": format(
            attempt_accounted_cost,
            ".6f",
        ),
        "auxiliary_job_count": len(normalized_auxiliary),
        "auxiliary_billed_minutes": auxiliary_billed_minutes,
        "auxiliary_actual_cost_usd": format(auxiliary_cost, ".6f"),
        "toolchain_materialization_attempt_count": len(toolchain_attempts),
        "toolchain_materialization_error_count": sum(job["terminal_status"] == "ERROR" for job in toolchain_attempts),
        "toolchain_materialization_completed_job_id": completed_toolchains[0]["job_id"],
        "toolchain_materialization_packet_tree_sha256": materialization["packet_tree_sha256"],
        "toolchain_materialization_bundle_tree_sha256": materialization["bundle_payload_tree_sha256"],
        "accounted_before_quantization_usd": format(accounted_before, ".6f"),
        "quantization_flavor": QUANTIZATION_FLAVOR,
        "quantization_hourly_rate_usd": format(
            QUANTIZATION_HOURLY_RATE_USD,
            ".3f",
        ),
        "quantization_timeout_minutes": QUANTIZATION_TIMEOUT_MINUTES,
        "quantization_maximum_cost_usd": format(quantization_job, ".3f"),
        "maximum_total_cost_usd": format(maximum, ".6f"),
        "remaining_after_reservations_usd": format(
            TOTAL_HF_BUDGET_USD - maximum,
            ".6f",
        ),
        "strictly_below_total_budget": maximum < TOTAL_HF_BUDGET_USD,
    }
    if use_remaining_credit_authorization:
        budget.update(
            {
                "budget_scope": "user_reported_remaining_hf_credits",
                "remaining_credit_authorization_usd": format(
                    REMAINING_HF_CREDITS_AUTHORIZATION_USD,
                    ".3f",
                ),
                "remaining_credit_authorization_sha256": (
                    TOTAL_HF_BUDGET_AUTHORIZATION_SHA256
                ),
                "post_authorization_actual_spend_usd": format(
                    post_authorization_actual,
                    ".6f",
                ),
                "maximum_remaining_credit_scope_cost_usd": format(
                    remaining_credit_scope_maximum,
                    ".6f",
                ),
                "remaining_credit_after_quantization_reservation_usd": format(
                    REMAINING_HF_CREDITS_AUTHORIZATION_USD
                    - remaining_credit_scope_maximum,
                    ".6f",
                ),
                "strictly_below_remaining_credit_authorization": True,
            }
        )
    return budget


def verify_hf_actual_cost_freshness(
    plan: Mapping[str, object],
    *,
    now_utc: datetime | None = None,
) -> dict[str, str]:
    """Require a cost snapshot captured immediately before packet launch."""

    validated = validate_hf_quantization_plan(plan)
    evidence = _mapping(
        validated["actual_cost_evidence"],
        "HF actual-cost evidence",
    )
    retrieved = datetime.strptime(
        str(evidence["retrieved_at_utc"]),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC)
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        raise HfQuantizationError("HF cost freshness clock must be timezone-aware")
    now = now.astimezone(UTC)
    age = (now - retrieved).total_seconds()
    if age < -ACTUAL_COST_FUTURE_SKEW_SECONDS or age > ACTUAL_COST_MAX_AGE_SECONDS:
        raise HfQuantizationError("HF actual-cost evidence is stale or from the future")
    expires = retrieved + timedelta(seconds=ACTUAL_COST_MAX_AGE_SECONDS)
    return {
        "retrieved_at_utc": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _validate_source_model(
    *,
    result: Mapping[str, Any],
    final_manifest: Mapping[str, Any],
    training_report: Mapping[str, Any],
    training_report_sha256: str,
    remote_model_uri: str,
    model_weight_bytes: int | None = None,
) -> dict[str, object]:
    if result.get("kind") == "scriber_full_model_final_evaluation_result":
        if (
            result.get("schema_version") != 1
            or result.get("training_steps_executed") != 0
            or final_manifest.get("schema_version") != 1
            or final_manifest.get("kind") != "scriber_full_model_final_evaluation_job"
            or _mapping(final_manifest.get("training"), "full-model evaluation training")
            != {"new_data_generated": False, "steps_executed": 0}
            or final_manifest.get("visibility") != "private"
        ):
            raise HfQuantizationError(
                "quantization requires the verified inference-only full-model evaluation"
            )
        model_uri = _private_bucket_uri(remote_model_uri, "final model")
        evaluated_model = _mapping(
            final_manifest.get("final_model"),
            "full-model final-evaluation binding",
        )
        final_tree = _prefixed_hash(
            evaluated_model.get("tree_sha256"),
            "full-model tree",
        )
        model_weight = _prefixed_hash(
            evaluated_model.get("weight_sha256"),
            "full-model weight",
        )
        training_fingerprint = _prefixed_hash(
            evaluated_model.get("run_fingerprint"),
            "full-model training fingerprint",
        )
        if (
            result.get("model_tree_sha256") != final_tree
            or evaluated_model.get("remote_uri") != model_uri
            or model_uri != FULL_MODEL_SOURCE_REMOTE_URI
            or final_tree != FULL_MODEL_SOURCE_TREE_SHA256
        ):
            raise HfQuantizationError(
                "full-model evaluation differs from the requested immutable model"
            )
        reports = result.get("reports")
        expected_report_paths = {
            f"{candidate}/{suite}/evaluation.json"
            for candidate in ("base_model", "final")
            for suite in REFERENCE_SUITES
        }
        if not isinstance(reports, list) or len(reports) != len(expected_report_paths):
            raise HfQuantizationError("full-model evaluation report inventory is incomplete")
        observed_paths: set[str] = set()
        for row in reports:
            if not isinstance(row, Mapping) or set(row) != {"bytes", "path", "sha256"}:
                raise HfQuantizationError("full-model evaluation report inventory is not closed")
            path = row.get("path")
            size = row.get("bytes")
            if (
                not isinstance(path, str)
                or path not in expected_report_paths
                or path in observed_paths
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 1
            ):
                raise HfQuantizationError("full-model evaluation report inventory is invalid")
            _prefixed_hash(row.get("sha256"), f"full-model report {path}")
            observed_paths.add(path)
        if observed_paths != expected_report_paths:
            raise HfQuantizationError("full-model evaluation report inventory is incomplete")

        report_hash = _prefixed_hash(
            training_report_sha256,
            "full-training completion hash",
        )
        final_artifact = _mapping(
            training_report.get("final_artifact"),
            "full-training final artifact",
        )
        resume = _mapping(training_report.get("resume"), "full-training resume")
        early_stopping = _mapping(
            training_report.get("early_stopping"),
            "full-training early stopping",
        )
        job = _mapping(training_report.get("job"), "full-training job")
        completed_updates = early_stopping.get("global_step")
        if (
            training_report.get("schema_version") != 1
            or training_report.get("kind") != "scriber_hf_full_training_completion"
            or training_report.get("training_completed") is not True
            or final_artifact.get("remote_uri") != model_uri
            or final_artifact.get("tree_sha256") != final_tree
            or final_artifact.get("model_safetensors_sha256") != model_weight
            or final_artifact.get("reload_status") != "passed"
            or training_report.get("run_fingerprint") != training_fingerprint
            or resume.get("checkpoint") != "checkpoint-1000"
            or resume.get("same_run") is not True
            or resume.get("repeated_steps_0_through_1000") is not False
            or job.get("status") != "COMPLETED"
            or isinstance(completed_updates, bool)
            or not isinstance(completed_updates, int)
            or completed_updates <= 1000
            or isinstance(model_weight_bytes, bool)
            or not isinstance(model_weight_bytes, int)
            or model_weight_bytes < 1
        ):
            raise HfQuantizationError(
                "full-training completion does not prove the exact non-replayed model"
            )
        frozen = frozen_lexical_policy_binding()
        return {
            "schema_version": 2,
            "kind": "scriber_hf_quantization_source_model",
            "training_regime": "full_120k_sft",
            "completed_updates": completed_updates,
            "replayed_updates": 0,
            "resume_checkpoint": "checkpoint-1000",
            "parent_initialization": "same_run_checkpoint_resume",
            "final_model_tree_sha256": final_tree,
            "model_weight_sha256": model_weight,
            "model_weight_bytes": model_weight_bytes,
            "model_weight_filename": FULL_MODEL_WEIGHT_FILENAME,
            "training_fingerprint": training_fingerprint,
            "remote_uri": model_uri,
            "mount_mode": "read_only",
            "cost_anchor_continuation_result_sha256": (
                _COST_ANCHOR_CONTINUATION_RESULT_SHA256
            ),
            "training_completion_sha256": report_hash,
            "protection_policy": frozen,
        }

    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_final_evaluation_result"
        or result.get("status") != "complete"
        or result.get("training_steps_executed") != 0
    ):
        raise HfQuantizationError("quantization requires one completed inference-only final evaluation")
    if final_manifest.get("schema_version") != 1 or final_manifest.get("kind") != "scriber_hf_final_evaluation":
        raise HfQuantizationError("final-evaluation manifest identity is invalid")
    final_model = _mapping(result.get("final_model"), "final-evaluation model")
    if dict(final_model) != dict(
        _mapping(
            final_manifest.get("final_model"),
            "final-evaluation manifest model",
        )
    ):
        raise HfQuantizationError("final-evaluation result and manifest bind different final models")
    if (
        final_model.get("cumulative_updates") != 1100
        or final_model.get("replayed_updates") != 0
        or final_model.get("parent_initialization") != "warm_start_weights_only"
        or final_model.get("mount_mode") != "read_only"
    ):
        raise HfQuantizationError("final evaluation does not bind the exact non-replayed cumulative-1100 model")
    model_uri = _private_bucket_uri(remote_model_uri, "final model")
    if final_model.get("remote_model_uri") != model_uri:
        raise HfQuantizationError("requested final-model mount differs from final-evaluation evidence")
    final_tree = _prefixed_hash(
        final_model.get("final_model_tree_sha256"),
        "final model tree",
    )
    report_hash = _prefixed_hash(
        training_report_sha256,
        "training report hash",
    )
    if final_model.get("training_report_sha256") != report_hash:
        raise HfQuantizationError("training report bytes differ from final-evaluation model evidence")
    if (
        training_report.get("schema_version") != 2
        or training_report.get("run_kind") != "improvement"
        or training_report.get("training_completed") is not True
        or training_report.get("max_steps") != 254
    ):
        raise HfQuantizationError("training report is not the completed 254-update continuation")
    training_fingerprint = _prefixed_hash(
        training_report.get("run_fingerprint"),
        "training fingerprint",
    )
    frozen = frozen_lexical_policy_binding()
    result_policy = _mapping(
        final_model.get("preprocessing_policy"),
        "final-model preprocessing policy",
    )
    for key in ("artifact_sha256", "policy_id", "policy_sha256"):
        if result_policy.get(key) != frozen[key]:
            raise HfQuantizationError("final model does not bind the frozen lexical protection policy")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_source_model",
        "cumulative_updates": 1100,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "final_model_tree_sha256": final_tree,
        "training_fingerprint": training_fingerprint,
        "remote_uri": model_uri,
        "mount_mode": "read_only",
        "continuation_result_sha256": _prefixed_hash(
            final_model.get("continuation_result_sha256"),
            "continuation result hash",
        ),
        "training_report_sha256": report_hash,
        "protection_policy": frozen,
    }


def _jsonl_record_count(path: Path) -> int:
    count = 0
    final_byte = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            count += chunk.count(b"\n")
            final_byte = chunk[-1:]
    return count if not final_byte or final_byte == b"\n" else count + 1


def _validate_full_model_references(
    final_manifest: Mapping[str, Any],
    reference_root: str | Path | None,
) -> dict[str, dict[str, object]]:
    if reference_root is None:
        raise HfQuantizationError(
            "full-model quantization requires the frozen evaluation reference root"
        )
    summary = _mapping(
        final_manifest.get("references"),
        "full-model final-evaluation references",
    )
    if summary != {
        "ai_gold_records": 1750,
        "critical_regression_records": 500,
        "regular_test_records": 1000,
        "usage": "inference_and_evaluation_only",
    }:
        raise HfQuantizationError("full-model evaluation reference summary is invalid")
    root = Path(reference_root).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise HfQuantizationError("full-model evaluation reference root is invalid")
    references: dict[str, dict[str, object]] = {}
    for suite_id, spec in FULL_MODEL_REFERENCE_FILES.items():
        records_filename, manifest_filename, expected_count, expected_splits = spec
        records_path = root / records_filename
        manifest_path = root / manifest_filename
        if (
            records_path.is_symlink()
            or not records_path.is_file()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
        ):
            raise HfQuantizationError(f"{suite_id} frozen references are missing")
        manifest, manifest_payload = read_json_object(
            manifest_path,
            f"{suite_id} frozen reference manifest",
        )
        output = manifest.get("output")
        manifest_record_count = manifest.get("record_count")
        manifest_records_sha256 = manifest.get("records_sha256")
        manifest_split_counts = manifest.get("split_counts")
        manifest_output_filename = manifest.get("output_filename")
        if isinstance(output, Mapping):
            manifest_record_count = output.get("record_count")
            manifest_records_sha256 = output.get("records_sha256")
            manifest_split_counts = output.get("split_counts")
            manifest_output_filename = output.get("filename")
        records_payload_hash = hashlib.sha256(records_path.read_bytes()).hexdigest()
        if (
            manifest.get("schema_version") != 1
            or manifest_record_count != expected_count
            or manifest_records_sha256 != records_payload_hash
            or manifest_split_counts != expected_splits
            or manifest_output_filename != records_filename
            or _jsonl_record_count(records_path) != expected_count
        ):
            raise HfQuantizationError(
                f"{suite_id} frozen references differ from their manifest"
            )
        references[suite_id] = {
            "records_filename": records_filename,
            "manifest_filename": manifest_filename,
            "record_count": expected_count,
            "split_counts": dict(sorted(expected_splits.items())),
            "records_sha256": records_payload_hash,
            "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "usage": "inference_and_evaluation_only",
        }
    return references


def _validate_references(
    final_manifest: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    reference_root: str | Path | None = None,
) -> dict[str, dict[str, object]]:
    if final_manifest.get("kind") == "scriber_full_model_final_evaluation_job":
        return _validate_full_model_references(final_manifest, reference_root)
    raw_references = _mapping(
        final_manifest.get("references"),
        "final-evaluation references",
    )
    if set(raw_references) != set(REFERENCE_SUITES):
        raise HfQuantizationError("quantization requires AI-Gold, critical-regression, and regular-test references")
    reports = _mapping(result.get("reports"), "final-evaluation reports")
    final_reports = _mapping(
        reports.get("final"),
        "final candidate evaluation reports",
    )
    if set(final_reports) != set(REFERENCE_SUITES):
        raise HfQuantizationError("successful final evaluation must cover every required reference suite")
    references: dict[str, dict[str, object]] = {}
    for suite_id in REFERENCE_SUITES:
        suite = _mapping(raw_references[suite_id], f"{suite_id} references")
        report = _mapping(final_reports[suite_id], f"{suite_id} final report")
        count = suite.get("record_count")
        splits = suite.get("split_counts")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or not isinstance(splits, Mapping)
            or not splits
            or "train" in splits
            or any(split not in {"test", "challenge"} for split in splits)
            or sum(int(value) for value in splits.values()) != count
            or suite.get("usage") != "inference_and_evaluation_only"
        ):
            raise HfQuantizationError(f"{suite_id} references are not a complete evaluation-only suite")
        records_filename = suite.get("records_filename")
        manifest_filename = suite.get("manifest_filename")
        if (
            not isinstance(records_filename, str)
            or _FILENAME.fullmatch(records_filename) is None
            or not isinstance(manifest_filename, str)
            or _FILENAME.fullmatch(manifest_filename) is None
        ):
            raise HfQuantizationError(f"{suite_id} reference filenames are unsafe")
        if (
            report.get("record_count") != count
            or report.get("gate_status") != "passed"
            or report.get("gate_passed") is not True
        ):
            raise HfQuantizationError(f"successful final evaluation is missing for {suite_id}")
        references[suite_id] = {
            "records_filename": records_filename,
            "manifest_filename": manifest_filename,
            "record_count": count,
            "split_counts": dict(sorted(splits.items())),
            "records_sha256": _raw_hash(
                suite.get("records_sha256"),
                f"{suite_id} reference hash",
            ),
            "manifest_sha256": _raw_hash(
                suite.get("manifest_sha256"),
                f"{suite_id} reference manifest hash",
            ),
            "usage": "inference_and_evaluation_only",
        }
    return references


def _immutable_bf16_baseline_binding(
    *,
    result: Mapping[str, object],
    result_sha256: str,
    result_bytes: int,
    manifest_sha256: str,
    manifest_bytes: int,
    model_tree_sha256: str,
    model_weight_sha256: str,
    model_weight_bytes: int,
) -> dict[str, object]:
    """Bind the completed full-model evaluation as evidence, never as work."""

    reports = result.get("reports")
    if not isinstance(reports, list):
        raise HfQuantizationError("full-model BF16 baseline report inventory is missing")
    suites: dict[str, dict[str, object]] = {}
    for suite_id in REFERENCE_SUITES:
        expected_path = f"final/{suite_id}/evaluation.json"
        matches = [
            row
            for row in reports
            if isinstance(row, Mapping) and row.get("path") == expected_path
        ]
        if len(matches) != 1:
            raise HfQuantizationError(
                f"full-model BF16 baseline is not unique for {suite_id}"
            )
        row = matches[0]
        size = row.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise HfQuantizationError(
                f"full-model BF16 baseline byte count is invalid for {suite_id}"
            )
        suites[suite_id] = {
            "source_report_path": expected_path,
            "relative_path": f"{suite_id}/evaluation.json",
            "bytes": size,
            "sha256": _prefixed_hash(
                row.get("sha256"),
                f"full-model BF16 baseline {suite_id}",
            ),
        }
    return {
        "schema_version": 1,
        "kind": "scriber_immutable_bf16_evaluation_baseline",
        "remote_uri": FULL_MODEL_BF16_BASELINE_REMOTE_URI,
        "mount_mode": "read_only",
        "source_model_tree_sha256": _prefixed_hash(
            model_tree_sha256,
            "full-model BF16 baseline model tree",
        ),
        "final_evaluation_result_sha256": _prefixed_hash(
            result_sha256,
            "full-model BF16 baseline result",
        ),
        "final_evaluation_result_bytes": result_bytes,
        "final_evaluation_manifest_sha256": _prefixed_hash(
            manifest_sha256,
            "full-model BF16 baseline manifest",
        ),
        "final_evaluation_manifest_bytes": manifest_bytes,
        "model_weight_sha256": _prefixed_hash(
            model_weight_sha256,
            "full-model BF16 baseline weight",
        ),
        "model_weight_bytes": model_weight_bytes,
        "mount_root_layout": "final_candidate_suite_roots",
        "training_steps_executed": 0,
        "reuse_policy": "evidence_only_no_materialization_inference_or_evaluation",
        "suites": suites,
    }


def _validate_immutable_bf16_baseline_binding(
    value: Mapping[str, object],
    *,
    source_model_tree_sha256: object,
    final_evaluation_result_sha256: object,
    final_evaluation_result_bytes: object,
    final_evaluation_manifest_sha256: object,
    final_evaluation_manifest_bytes: object,
    model_weight_sha256: object,
    model_weight_bytes: object,
) -> dict[str, object]:
    baseline = dict(_mapping(value, "immutable BF16 evaluation baseline"))
    expected = {
        "schema_version",
        "kind",
        "remote_uri",
        "mount_mode",
        "source_model_tree_sha256",
        "final_evaluation_result_sha256",
        "final_evaluation_result_bytes",
        "final_evaluation_manifest_sha256",
        "final_evaluation_manifest_bytes",
        "model_weight_sha256",
        "model_weight_bytes",
        "mount_root_layout",
        "training_steps_executed",
        "reuse_policy",
        "suites",
    }
    if (
        set(baseline) != expected
        or baseline.get("schema_version") != 1
        or baseline.get("kind") != "scriber_immutable_bf16_evaluation_baseline"
        or baseline.get("remote_uri") != FULL_MODEL_BF16_BASELINE_REMOTE_URI
        or baseline.get("mount_mode") != "read_only"
        or baseline.get("source_model_tree_sha256") != source_model_tree_sha256
        or baseline.get("final_evaluation_result_sha256")
        != final_evaluation_result_sha256
        or baseline.get("final_evaluation_result_bytes")
        != final_evaluation_result_bytes
        or baseline.get("final_evaluation_manifest_sha256")
        != final_evaluation_manifest_sha256
        or baseline.get("final_evaluation_manifest_bytes")
        != final_evaluation_manifest_bytes
        or baseline.get("model_weight_sha256") != model_weight_sha256
        or baseline.get("model_weight_bytes") != model_weight_bytes
        or baseline.get("mount_root_layout") != "final_candidate_suite_roots"
        or isinstance(baseline.get("final_evaluation_result_bytes"), bool)
        or not isinstance(baseline.get("final_evaluation_result_bytes"), int)
        or int(baseline["final_evaluation_result_bytes"]) < 1
        or isinstance(baseline.get("final_evaluation_manifest_bytes"), bool)
        or not isinstance(baseline.get("final_evaluation_manifest_bytes"), int)
        or int(baseline["final_evaluation_manifest_bytes"]) < 1
        or isinstance(baseline.get("model_weight_bytes"), bool)
        or not isinstance(baseline.get("model_weight_bytes"), int)
        or int(baseline["model_weight_bytes"]) < 1
        or baseline.get("training_steps_executed") != 0
        or baseline.get("reuse_policy")
        != "evidence_only_no_materialization_inference_or_evaluation"
    ):
        raise HfQuantizationError("immutable BF16 baseline binding is invalid")
    _private_bucket_uri(baseline.get("remote_uri"), "immutable BF16 baseline")
    _prefixed_hash(baseline.get("model_weight_sha256"), "immutable BF16 model weight")
    suites = _mapping(baseline.get("suites"), "immutable BF16 baseline suites")
    if set(suites) != set(REFERENCE_SUITES):
        raise HfQuantizationError("immutable BF16 baseline suite binding is incomplete")
    for suite_id in REFERENCE_SUITES:
        suite = _mapping(suites[suite_id], f"immutable BF16 baseline {suite_id}")
        if (
            set(suite)
            != {"source_report_path", "relative_path", "bytes", "sha256"}
            or suite.get("source_report_path")
            != f"final/{suite_id}/evaluation.json"
            or suite.get("relative_path") != f"{suite_id}/evaluation.json"
            or isinstance(suite.get("bytes"), bool)
            or not isinstance(suite.get("bytes"), int)
            or int(suite["bytes"]) < 1
        ):
            raise HfQuantizationError(
                f"immutable BF16 baseline {suite_id} binding is invalid"
            )
        _prefixed_hash(suite.get("sha256"), f"immutable BF16 baseline {suite_id}")
    return baseline


def _validate_toolchain_materialization_binding(
    value: Mapping[str, object],
) -> dict[str, object]:
    binding = dict(_mapping(value, "llama.cpp toolchain materialization"))
    expected = {
        "schema_version",
        "kind",
        "attempt",
        "source_git_head",
        "status",
        "packet_tree_sha256",
        "bundle_payload_tree_sha256",
        "result_sha256",
        "flavor",
        "timeout_minutes",
        "training_steps_executed",
        "evaluation_reports_written",
        "prediction_records_written",
    }
    provenance = binding.get("external_provenance")
    if provenance is not None:
        expected.add("external_provenance")
    if (
        set(binding) != expected
        or binding.get("schema_version") != 1
        or binding.get("kind") != "scriber_llama_cpp_toolchain_materialization_binding"
        or binding.get("attempt") != TOOLCHAIN_ATTEMPT
        or binding.get("source_git_head") != REVIEWED_SCRIBER_GIT_HEAD
        or binding.get("status") != "verified"
        or binding.get("flavor") != TOOLCHAIN_FLAVOR
        or binding.get("timeout_minutes") != 20
        or binding.get("training_steps_executed") != 0
        or binding.get("evaluation_reports_written") != 0
        or binding.get("prediction_records_written") != 0
    ):
        raise HfQuantizationError("llama.cpp toolchain materialization binding is invalid")
    for field in (
        "packet_tree_sha256",
        "bundle_payload_tree_sha256",
        "result_sha256",
    ):
        binding[field] = _prefixed_hash(
            binding.get(field),
            f"llama.cpp toolchain {field}",
        )
    if provenance is not None:
        external = dict(_mapping(provenance, "llama.cpp external provenance"))
        provenance_fields = {
            "schema_version",
            "kind",
            "result_sha256",
            "producer_job_id",
            "claim_commit",
            "claim_owner",
            "remote_uri",
            "binding_sha256",
        }
        unsigned = {key: value for key, value in external.items() if key != "binding_sha256"}
        if (
            set(external) != provenance_fields
            or external.get("schema_version") != 1
            or external.get("kind")
            != "scriber_hf_toolchain_v19_external_provenance"
            or _prefixed_hash(
                external.get("result_sha256"),
                "external toolchain result hash",
            )
            != binding["result_sha256"]
            or _JOB_ID.fullmatch(str(external.get("producer_job_id", ""))) is None
            or _COMMIT.fullmatch(str(external.get("claim_commit", ""))) is None
            or _OWNER_TOKEN.fullmatch(str(external.get("claim_owner", ""))) is None
            or _private_bucket_uri(
                external.get("remote_uri"),
                "external toolchain URI",
            )
            != TOOLCHAIN_REMOTE_OUTPUT_URI
            or external.get("binding_sha256") != _sha256(_canonical_bytes(unsigned))
        ):
            raise HfQuantizationError(
                "llama.cpp external materialization provenance is invalid"
            )
        binding["external_provenance"] = external
    return binding


def _validate_llama_cpp_binding(
    value: Mapping[str, Any],
    *,
    remote_uri: str,
) -> dict[str, object]:
    required = {
        "schema_version",
        "kind",
        "attempt",
        "source_git_head",
        "status",
        "platform",
        "lock_sha256",
        "commit",
        "source_tree_sha256",
        "build",
        "build_identity_sha256",
        "cuda_enabled",
        "distribution",
        "native_runtime_tree_sha256",
        "ldd_closure_sha256",
        "required_host_driver_sonames",
        "runtime_library_directories",
        "tool_sha256",
        "materialization",
    }
    if set(value) != required:
        raise HfQuantizationError("llama.cpp cloud binding is not a closed manifest")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "scriber_cloud_llama_cpp_binding"
        or value.get("status") != "verified"
        or value.get("platform") != "linux_x86_64_cuda"
        or value.get("cuda_enabled") is not True
        or value.get("required_host_driver_sonames")
        != list(CUDA_HOST_DRIVER_SONAMES)
        or value.get("build") != reviewed_toolchain_build_binding()
        or remote_uri != TOOLCHAIN_REMOTE_OUTPUT_URI
    ):
        raise HfQuantizationError("llama.cpp bundle is not a verified Linux CUDA toolchain")
    tools = _mapping(value.get("tool_sha256"), "llama.cpp tools")
    expected_tools = {
        "convert_hf_to_gguf",
        "llama_quantize",
        "llama_cli",
        "llama_server",
    }
    if set(tools) != expected_tools:
        raise HfQuantizationError("llama.cpp bundle has an incomplete tool set")
    commit = value.get("commit")
    if commit != TOOLCHAIN_COMMIT:
        raise HfQuantizationError("llama.cpp commit must be the exact reviewed b10156 revision")
    distribution = _mapping(
        value.get("distribution"),
        "llama.cpp OCI distribution",
    )
    if set(distribution) != {
        "kind",
        "repository",
        "tag",
        "index_digest",
        "manifest_digest",
        "config_digest",
        "revision",
        "platform",
        "layers",
        "runtime_image",
    }:
        raise HfQuantizationError("llama.cpp OCI distribution is not closed")
    platform = _mapping(
        distribution.get("platform"),
        "llama.cpp OCI platform",
    )
    layers = distribution.get("layers")
    if (
        distribution.get("kind") != "oci_image_layers"
        or distribution.get("repository") != "ghcr.io/ggml-org/llama.cpp"
        or distribution.get("tag") != TAG
        or distribution.get("index_digest") != INDEX_DIGEST
        or distribution.get("manifest_digest") != MANIFEST_DIGEST
        or distribution.get("config_digest") != CONFIG_DIGEST
        or distribution.get("revision") != commit
        or platform != {"os": "linux", "architecture": "amd64"}
        or distribution.get("runtime_image") != QUANTIZATION_IMAGE
        or not isinstance(layers, list)
        or len(layers) != 2
    ):
        raise HfQuantizationError("llama.cpp OCI distribution identity is invalid")
    for label in ("index_digest", "manifest_digest", "config_digest"):
        _prefixed_hash(
            distribution.get(label),
            f"llama.cpp OCI {label}",
        )
    layer_purposes: set[str] = set()
    layer_digests: set[str] = set()
    normalized_layers: list[dict[str, object]] = []
    for layer in layers:
        record = _mapping(layer, "llama.cpp OCI layer")
        if set(record) != {
            "digest",
            "compressed_bytes",
            "media_type",
            "purpose",
        }:
            raise HfQuantizationError("llama.cpp OCI layer is not closed")
        size = record.get("compressed_bytes")
        purpose = record.get("purpose")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or record.get("media_type") != "application/vnd.oci.image.layer.v1.tar+gzip"
            or purpose not in {"native_libraries", "tools_and_conversion"}
        ):
            raise HfQuantizationError("llama.cpp OCI layer identity is invalid")
        digest = _prefixed_hash(
            record.get("digest"),
            "llama.cpp OCI layer digest",
        )
        if digest in layer_digests:
            raise HfQuantizationError("llama.cpp OCI layer digests must be unique")
        layer_digests.add(digest)
        layer_purposes.add(str(purpose))
        normalized_layers.append(dict(record))
    if layer_purposes != {"native_libraries", "tools_and_conversion"}:
        raise HfQuantizationError("llama.cpp OCI layer purposes are incomplete")
    expected_layers = [
        {
            "digest": digest,
            "compressed_bytes": size,
            "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
            "purpose": purpose,
        }
        for purpose, (digest, size) in zip(
            ("native_libraries", "tools_and_conversion"),
            SELECTED_LAYERS.items(),
            strict=True,
        )
    ]
    if normalized_layers != expected_layers:
        raise HfQuantizationError("llama.cpp OCI layers differ from the reviewed b10156 CUDA bytes")
    library_directories = value.get("runtime_library_directories")
    if (
        not isinstance(library_directories, list)
        or not library_directories
        or any(
            not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or ".." in path.split("/")
            for path in library_directories
        )
        or len(set(library_directories)) != len(library_directories)
    ):
        raise HfQuantizationError("llama.cpp runtime library directories are invalid")
    binding = {
        "schema_version": 1,
        "kind": "scriber_cloud_llama_cpp_binding",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "platform": "linux_x86_64_cuda",
        "lock_sha256": _prefixed_hash(
            value.get("lock_sha256"),
            "llama.cpp lock hash",
        ),
        "commit": commit,
        "source_tree_sha256": _prefixed_hash(
            value.get("source_tree_sha256"),
            "llama.cpp source tree",
        ),
        "build": reviewed_toolchain_build_binding(),
        "build_identity_sha256": _prefixed_hash(
            value.get("build_identity_sha256"),
            "llama.cpp build identity",
        ),
        "cuda_enabled": True,
        "distribution": {
            **dict(distribution),
            "platform": dict(platform),
            "layers": normalized_layers,
        },
        "native_runtime_tree_sha256": _prefixed_hash(
            value.get("native_runtime_tree_sha256"),
            "llama.cpp native runtime tree",
        ),
        "ldd_closure_sha256": _prefixed_hash(
            value.get("ldd_closure_sha256"),
            "llama.cpp ldd closure",
        ),
        "required_host_driver_sonames": list(CUDA_HOST_DRIVER_SONAMES),
        "runtime_library_directories": list(library_directories),
        "tool_sha256": {name: _prefixed_hash(tools[name], f"llama.cpp {name} hash") for name in sorted(tools)},
        "materialization": _validate_toolchain_materialization_binding(
            _mapping(
                value.get("materialization"),
                "llama.cpp toolchain materialization",
            )
        ),
        "remote_uri": _private_bucket_uri(
            remote_uri,
            "llama.cpp bundle",
        ),
        "mount_mode": "read_only",
    }
    return binding


def build_hf_quantization_dry_bind(
    *,
    final_evaluation_result: Mapping[str, object],
    final_evaluation_result_sha256: str,
    final_evaluation_result_bytes: int | None = None,
    final_evaluation_manifest: Mapping[str, object],
    final_evaluation_manifest_sha256: str,
    final_evaluation_manifest_bytes: int | None = None,
    training_report: Mapping[str, object],
    training_report_sha256: str,
    remote_model_uri: str,
    model_weight_bytes: int | None = None,
    llama_cpp_bundle: Mapping[str, object],
    remote_llama_cpp_uri: str,
    source_git_head: str,
    actual_cost_evidence: Mapping[str, object],
    actual_cost_evidence_sha256: str,
    reference_root: str | Path | None = None,
    evaluation_config_sha256: str | None = None,
) -> dict[str, object]:
    """Build the immutable plan without copying a packet or starting a job."""

    if _COMMIT.fullmatch(source_git_head) is None:
        raise HfQuantizationError("source Git HEAD must be an exact 40-character commit")
    result = _mapping(
        final_evaluation_result,
        "final-evaluation result",
    )
    final_manifest = _mapping(
        final_evaluation_manifest,
        "final-evaluation manifest",
    )
    report = _mapping(training_report, "training completion report")
    source_model = _validate_source_model(
        result=result,
        final_manifest=final_manifest,
        training_report=report,
        training_report_sha256=training_report_sha256,
        remote_model_uri=remote_model_uri,
        model_weight_bytes=model_weight_bytes,
    )
    references = _validate_references(
        final_manifest,
        result,
        reference_root=reference_root,
    )
    llama_cpp = _validate_llama_cpp_binding(
        _mapping(llama_cpp_bundle, "llama.cpp cloud binding"),
        remote_uri=remote_llama_cpp_uri,
    )
    full_model = source_model.get("schema_version") == 2
    config_filename = "evaluation.yaml"
    if full_model:
        config_sha256 = _raw_hash(
            evaluation_config_sha256,
            "full-model evaluation configuration hash",
        )
    else:
        evaluation = _mapping(
            final_manifest.get("evaluation"),
            "final-evaluation configuration",
        )
        if evaluation.get("config_filename") != config_filename:
            raise HfQuantizationError("quantization must reuse the frozen evaluation.yaml")
        config_sha256 = _raw_hash(
            evaluation.get("config_sha256"),
            "evaluation configuration hash",
        )
    final_result_hash = _prefixed_hash(
        final_evaluation_result_sha256,
        "final-evaluation result hash",
    )
    final_manifest_hash = _prefixed_hash(
        final_evaluation_manifest_sha256,
        "final-evaluation manifest hash",
    )
    if full_model and (
        isinstance(final_evaluation_result_bytes, bool)
        or not isinstance(final_evaluation_result_bytes, int)
        or final_evaluation_result_bytes < 1
        or isinstance(final_evaluation_manifest_bytes, bool)
        or not isinstance(final_evaluation_manifest_bytes, int)
        or final_evaluation_manifest_bytes < 1
    ):
        raise HfQuantizationError(
            "full-model final-evaluation byte-size binding is incomplete"
        )
    normalized_cost_evidence = json.loads(
        _canonical_bytes(
            dict(
                _mapping(
                    actual_cost_evidence,
                    "HF actual-cost evidence",
                )
            )
        )
    )
    budget = build_hf_quantization_budget(
        normalized_cost_evidence,
        evidence_sha256=actual_cost_evidence_sha256,
        expected_continuation_result_sha256=str(
            source_model[
                "cost_anchor_continuation_result_sha256"
                if full_model
                else "continuation_result_sha256"
            ]
        ),
        expected_final_evaluation_result_sha256=final_result_hash,
        expected_toolchain_materialization=_mapping(
            llama_cpp["materialization"],
            "llama.cpp toolchain materialization",
        ),
        use_remaining_credit_authorization=full_model,
    )
    evaluation_binding = (
        {
            "result_sha256": final_result_hash,
            "result_bytes": final_evaluation_result_bytes,
            "manifest_sha256": final_manifest_hash,
            "manifest_bytes": final_evaluation_manifest_bytes,
            "required_suites": list(REFERENCE_SUITES),
            "training_steps_executed": 0,
            "model_tree_sha256": source_model["final_model_tree_sha256"],
            "release_eligibility": "not_granted",
            "quantization_scope": "private_candidate_comparison_only",
        }
        if full_model
        else {
            "result_sha256": final_result_hash,
            "manifest_sha256": final_manifest_hash,
            "required_suites": list(REFERENCE_SUITES),
            "all_final_gates_passed": True,
        }
    )
    evaluation_policy = {
        "config_filename": config_filename,
        "config_sha256": config_sha256,
        "max_new_tokens": 512,
        "transformers_batch_size": 8,
        "gguf_parallel_slots": 1,
        "fresh_quantized_variants": list(QUANTIZED_VARIANTS),
        "quality_delta_baseline": "bf16",
        "quality_delta_budgets": QUALITY_DELTA_BUDGETS,
    }
    if full_model:
        evaluation_policy.update(
            {
                "absolute_gate_policy": "record_only_private_candidate",
                "bf16_baseline_mode": "immutable_existing_evidence",
                "bf16_execution_prohibited": [
                    "materialization",
                    "inference",
                    "evaluation",
                ],
            }
        )
    else:
        evaluation_policy["fresh_for_every_variant"] = True
    output = {
        "bucket_root": QUANTIZATION_OUTPUT_BUCKET,
        "relative_path": QUANTIZATION_OUTPUT_RELATIVE,
        "container_path": QUANTIZATION_OUTPUT_ROOT,
        "unique": True,
        "overwrite_allowed": False,
    }
    if full_model:
        output.update(
            {
                "duplicate_launch_allowed": False,
                "durable_resume_allowed": False,
                "completed_output_never_overwritten": True,
            }
        )
    variants = [
        {
            "id": "bf16",
            "backend": (
                "verified_final_evaluation_evidence"
                if full_model
                else "transformers"
            ),
            "evaluation": (
                "immutable_existing_baseline" if full_model else "fresh_full"
            ),
        },
        {
            "id": "int8",
            "backend": "transformers_bitsandbytes",
            "evaluation": "fresh_full",
        },
        {
            "id": "int4_nf4",
            "backend": "transformers_bitsandbytes",
            "evaluation": "fresh_full",
        },
        {
            "id": "Q8_0",
            "backend": "llama_cpp",
            "evaluation": "fresh_full",
        },
        {
            "id": "Q4_K_M",
            "backend": "llama_cpp",
            "evaluation": "fresh_full",
        },
    ]
    plan = {
        "schema_version": 2 if full_model else 1,
        "kind": "scriber_hf_quantization_plan",
        "source_git_head": source_git_head,
        "image": QUANTIZATION_IMAGE,
        "flavor": QUANTIZATION_FLAVOR,
        "timeout_minutes": QUANTIZATION_TIMEOUT_MINUTES,
        "source_model": source_model,
        (
            "verified_final_evaluation"
            if full_model
            else "successful_final_evaluation"
        ): evaluation_binding,
        "actual_cost_evidence": normalized_cost_evidence,
        "llama_cpp": llama_cpp,
        "references": references,
        "evaluation": evaluation_policy,
        "variants": variants,
        "budget": budget,
        "output": output,
        "publication": {
            "allowed": False,
            "destination_kind": "private_hf_bucket",
        },
    }
    if full_model:
        plan["immutable_bf16_baseline"] = _immutable_bf16_baseline_binding(
            result=result,
            result_sha256=final_result_hash,
            result_bytes=int(final_evaluation_result_bytes),
            manifest_sha256=final_manifest_hash,
            manifest_bytes=int(final_evaluation_manifest_bytes),
            model_tree_sha256=str(source_model["final_model_tree_sha256"]),
            model_weight_sha256=str(source_model["model_weight_sha256"]),
            model_weight_bytes=int(source_model["model_weight_bytes"]),
        )
    return plan


def validate_hf_quantization_result(
    value: Mapping[str, object],
    *,
    plan: Mapping[str, object],
) -> dict[str, object]:
    """Validate the closed cross-variant summary against the immutable plan."""

    plan = validate_hf_quantization_plan(plan)
    result = dict(_mapping(value, "HF quantization result"))
    expected_top = {
        "schema_version",
        "kind",
        "status",
        "source_model_tree_sha256",
        "training_fingerprint",
        "protection_policy",
        "variants",
        "budget",
        "publication",
    }
    full_model = plan.get("schema_version") == 2
    if full_model:
        expected_top.add("immutable_bf16_baseline")
    if set(result) != expected_top:
        raise HfQuantizationError("HF quantization result is not a closed manifest")
    source = _mapping(plan.get("source_model"), "quantization source model")
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_quantization_result"
        or result.get("status") != "complete"
        or result.get("source_model_tree_sha256") != source.get("final_model_tree_sha256")
        or result.get("training_fingerprint") != source.get("training_fingerprint")
        or result.get("protection_policy") != frozen_lexical_policy_binding()
        or result.get("budget") != plan.get("budget")
        or result.get("publication") != plan.get("publication")
    ):
        raise HfQuantizationError("HF quantization result identity differs from its dry bind")
    variants = _mapping(result.get("variants"), "quantization variants")
    absolute_gate_required = plan.get("schema_version") == 1
    expected_result_variants = QUANTIZED_VARIANTS if full_model else VARIANTS
    if set(variants) != set(expected_result_variants):
        raise HfQuantizationError(
            "HF quantization result contains an invalid variant set"
        )
    if full_model:
        baseline = _mapping(
            result.get("immutable_bf16_baseline"),
            "immutable BF16 baseline verification",
        )
        plan_baseline = _mapping(
            plan.get("immutable_bf16_baseline"),
            "immutable BF16 baseline binding",
        )
        expected_baseline_fields = {
            "schema_version",
            "kind",
            "status",
            "source_model_tree_sha256",
            "model_weight_sha256",
            "model_weight_bytes",
            "final_evaluation_result_sha256",
            "final_evaluation_result_bytes",
            "final_evaluation_manifest_sha256",
            "final_evaluation_manifest_bytes",
            "remote_uri",
            "suites",
            "materialization_runs",
            "inference_runs",
            "evaluation_runs",
        }
        if (
            set(baseline) != expected_baseline_fields
            or baseline.get("schema_version") != 1
            or baseline.get("kind")
            != "scriber_immutable_bf16_baseline_verification"
            or baseline.get("status") != "verified"
            or baseline.get("source_model_tree_sha256")
            != plan_baseline.get("source_model_tree_sha256")
            or baseline.get("model_weight_sha256")
            != plan_baseline.get("model_weight_sha256")
            or baseline.get("model_weight_bytes")
            != plan_baseline.get("model_weight_bytes")
            or baseline.get("final_evaluation_result_sha256")
            != plan_baseline.get("final_evaluation_result_sha256")
            or baseline.get("final_evaluation_result_bytes")
            != plan_baseline.get("final_evaluation_result_bytes")
            or baseline.get("final_evaluation_manifest_sha256")
            != plan_baseline.get("final_evaluation_manifest_sha256")
            or baseline.get("final_evaluation_manifest_bytes")
            != plan_baseline.get("final_evaluation_manifest_bytes")
            or baseline.get("remote_uri") != plan_baseline.get("remote_uri")
            or baseline.get("materialization_runs") != 0
            or baseline.get("inference_runs") != 0
            or baseline.get("evaluation_runs") != 0
        ):
            raise HfQuantizationError(
                "immutable BF16 baseline verification is invalid"
            )
        baseline_suites = _mapping(
            baseline.get("suites"), "immutable BF16 baseline verification suites"
        )
        bound_suites = _mapping(
            plan_baseline.get("suites"), "immutable BF16 baseline bound suites"
        )
        if set(baseline_suites) != set(REFERENCE_SUITES):
            raise HfQuantizationError(
                "immutable BF16 baseline verification is incomplete"
            )
        for suite_id in REFERENCE_SUITES:
            observed = _mapping(
                baseline_suites[suite_id],
                f"immutable BF16 baseline verification {suite_id}",
            )
            bound = _mapping(
                bound_suites[suite_id],
                f"immutable BF16 baseline binding {suite_id}",
            )
            if observed != {"bytes": bound["bytes"], "sha256": bound["sha256"]}:
                raise HfQuantizationError(
                    f"immutable BF16 baseline verification differs for {suite_id}"
                )
    references = _mapping(plan.get("references"), "quantization references")
    runtime_instances: set[str] = set()
    llama_tools = _mapping(
        _mapping(plan["llama_cpp"], "llama.cpp plan")["tool_sha256"],
        "llama.cpp tools",
    )
    for variant in expected_result_variants:
        record = _mapping(variants[variant], f"{variant} result")
        expected_fields = {
            "artifact_tree_sha256",
            "source_tree_sha256",
            "training_fingerprint",
            "protection_policy",
            "fresh_reload_passed",
            "evaluation",
        }
        if set(record) != expected_fields:
            raise HfQuantizationError(f"{variant} result is not a closed artifact record")
        _prefixed_hash(
            record.get("artifact_tree_sha256"),
            f"{variant} artifact tree",
        )
        if record.get("source_tree_sha256") != source.get("final_model_tree_sha256"):
            raise HfQuantizationError(f"{variant} artifact differs from the bound source model")
        if (
            record.get("training_fingerprint") != source.get("training_fingerprint")
            or record.get("protection_policy") != frozen_lexical_policy_binding()
            or record.get("fresh_reload_passed") is not True
        ):
            raise HfQuantizationError(f"{variant} artifact identity or fresh reload is incomplete")
        evaluations = _mapping(
            record.get("evaluation"),
            f"{variant} evaluation",
        )
        if set(evaluations) != set(references):
            raise HfQuantizationError(f"{variant} evaluation does not cover every required suite")
        for suite_id, suite in references.items():
            lane = _mapping(
                evaluations[suite_id],
                f"{variant}/{suite_id} evaluation",
            )
            expected_lane_fields = {
                "record_count",
                "exact_case_coverage",
                "gate_passed",
                "prediction_sha256",
                "evaluation_sha256",
                "quality_delta",
            }
            if variant in {"Q8_0", "Q4_K_M"}:
                expected_lane_fields.add("runtime_evidence")
            if set(lane) != expected_lane_fields:
                raise HfQuantizationError(f"{variant}/{suite_id} evaluation is not closed")
            if (
                lane.get("record_count") != _mapping(suite, f"{suite_id} references").get("record_count")
                or lane.get("exact_case_coverage") is not True
                or not isinstance(lane.get("gate_passed"), bool)
                or (absolute_gate_required and lane.get("gate_passed") is not True)
            ):
                raise HfQuantizationError(f"{variant}/{suite_id} evaluation is incomplete")
            _prefixed_hash(
                lane.get("prediction_sha256"),
                f"{variant}/{suite_id} prediction hash",
            )
            _prefixed_hash(
                lane.get("evaluation_sha256"),
                f"{variant}/{suite_id} evaluation hash",
            )
            delta = _mapping(
                lane.get("quality_delta"),
                f"{variant}/{suite_id} quality delta",
            )
            expected_delta_fields = {
                "baseline_evaluation_sha256",
                "candidate_evaluation_sha256",
                "report_sha256",
                "maximum_absolute_metric_drop",
                "new_critical_errors",
                "budget",
                "within_budget",
            }
            budget = QUALITY_DELTA_BUDGETS[variant]
            if (
                set(delta) != expected_delta_fields
                or delta.get("candidate_evaluation_sha256") != lane.get("evaluation_sha256")
                or delta.get("budget") != budget
                or delta.get("within_budget") is not True
                or not isinstance(delta.get("maximum_absolute_metric_drop"), int | float)
                or float(delta["maximum_absolute_metric_drop"]) > float(budget["maximum_absolute_metric_drop"])
                or not isinstance(delta.get("new_critical_errors"), int)
                or int(delta["new_critical_errors"]) > int(budget["maximum_new_critical_errors"])
            ):
                raise HfQuantizationError(
                    f"{variant}/{suite_id} BF16-relative quality delta is incomplete or over budget"
                )
            _prefixed_hash(
                delta.get("baseline_evaluation_sha256"),
                f"{variant}/{suite_id} baseline evaluation hash",
            )
            _prefixed_hash(
                delta.get("report_sha256"),
                f"{variant}/{suite_id} quality-delta report hash",
            )
            if variant in {"Q8_0", "Q4_K_M"}:
                runtime_evidence = _validate_llama_cpp_cuda_lane_evidence(
                    _mapping(
                        lane.get("runtime_evidence"),
                        f"{variant}/{suite_id} runtime evidence",
                    ),
                    variant=variant,
                    suite_id=suite_id,
                    llama_server_sha256=str(llama_tools["llama_server"]),
                    reference_sha256=(
                        "sha256:"
                        + str(
                            _mapping(
                                suite,
                                f"{suite_id} references",
                            )["records_sha256"]
                        )
                    ),
                    prediction_sha256=str(lane["prediction_sha256"]),
                )
                instance_id = str(
                    _mapping(
                        runtime_evidence["offload"],
                        "llama.cpp offload proof",
                    )["runtime_instance_id"]
                )
                if instance_id in runtime_instances:
                    raise HfQuantizationError("llama.cpp runtime instance was reused across lanes")
                runtime_instances.add(instance_id)
    return result


def hf_quantization_bootstrap_payload(
    packet_root: str | Path = "/input/repo",
    runner_executable: str = "/bin/bash",
) -> str:
    """Return trusted stdlib-only packet verification embedded in HF argv."""

    raw_root = str(packet_root)
    if not Path(raw_root).is_absolute() and not PurePosixPath(raw_root).is_absolute():
        raise HfQuantizationError("trusted quantization packet root must be absolute")
    if not runner_executable or "\x00" in runner_executable:
        raise HfQuantizationError("trusted quantization runner executable is invalid")
    script = r'''import hashlib,json,os,re,subprocess,sys
from pathlib import Path

root=Path(__PACKET_ROOT__).resolve()
expected_tree,expected_runner,expected_manifest,owner=sys.argv[1:5]
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_tree) is None:
    raise SystemExit("trusted quantization bootstrap: invalid packet tree")
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_runner) is None:
    raise SystemExit("trusted quantization bootstrap: invalid runner hash")
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_manifest) is None:
    raise SystemExit("trusted quantization bootstrap: invalid manifest hash")
if re.fullmatch(r"[0-9a-f]{64}",owner) is None:
    raise SystemExit("trusted quantization bootstrap: invalid claim owner")

def canonical(value):
    return (json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True,allow_nan=False)+"\n").encode("utf-8")

def file_hash(path):
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block=stream.read(1024*1024)
            if not block:
                break
            digest.update(block)
    return "sha256:"+digest.hexdigest()

manifest_path=root/"job-manifest.json"
payload=manifest_path.read_bytes()
if file_hash(manifest_path) != expected_manifest:
    raise SystemExit("trusted quantization bootstrap: manifest hash differs")
manifest=json.loads(payload)
if payload != canonical(manifest):
    raise SystemExit("trusted quantization bootstrap: manifest is not canonical")
if (
    manifest.get("schema_version") != 2
    or manifest.get("kind") != "scriber_hf_quantization_plan"
    or re.fullmatch(r"[0-9a-f]{40}",str(manifest.get("source_git_head", ""))) is None
    or manifest.get("image") != __IMAGE__
    or manifest.get("flavor") != "l4x1"
    or manifest.get("timeout_minutes") != 285
):
    raise SystemExit("trusted quantization bootstrap: reviewed launch identity differs")
records=[]
for path in sorted(root.rglob("*"),key=lambda item:item.relative_to(root).as_posix()):
    relative=path.relative_to(root).as_posix()
    if relative == "job-manifest.json":
        continue
    if path.is_symlink():
        raise SystemExit("trusted quantization bootstrap: non-regular entry: "+relative)
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit("trusted quantization bootstrap: non-regular entry: "+relative)
    records.append({"path":relative,"kind":"file","bytes":path.stat().st_size,"sha256":file_hash(path)})
digest=hashlib.sha256()
for record in records:
    digest.update(canonical(record))
tree="sha256:"+digest.hexdigest()
if tree != expected_tree or manifest.get("packet_tree_sha256") != expected_tree:
    raise SystemExit("trusted quantization bootstrap: packet tree differs")
if manifest.get("file_count") != len(records) or manifest.get("byte_count") != sum(int(row["bytes"]) for row in records):
    raise SystemExit("trusted quantization bootstrap: packet inventory differs")
runner=root/"run-hf-quantization.sh"
if file_hash(runner) != expected_runner or manifest.get("runner_sha256") != expected_runner:
    raise SystemExit("trusted quantization bootstrap: runner differs")
os.environ["SCRIBER_TRUSTED_QUANTIZATION_PACKET_TREE_SHA256"]=expected_tree
os.environ["SCRIBER_TRUSTED_QUANTIZATION_MANIFEST_SHA256"]=expected_manifest
os.environ["SCRIBER_QUANTIZATION_V2_LAUNCH_CLAIM_OWNER"]=owner
runner_argv=[__RUNNER_EXEC__,str(runner),expected_tree,expected_manifest,owner]
if os.name == "nt":
    raise SystemExit(subprocess.run(runner_argv,env=os.environ,check=False).returncode)
os.execvpe(__RUNNER_EXEC__,runner_argv,os.environ)
'''
    return (
        script.replace("__PACKET_ROOT__", json.dumps(raw_root))
        .replace("__RUNNER_EXEC__", json.dumps(runner_executable))
        .replace("__IMAGE__", json.dumps(QUANTIZATION_IMAGE))
    )


def hf_quantization_runner_payload() -> bytes:
    """Return the no-secret, mount-only, bounded cloud runner."""

    script = r"""#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8

PACKET_ROOT=/input/repo
SOURCE_REPO="$PACKET_ROOT/repo"
REMOTE_MODEL=/final-model
REMOTE_LLAMA_CPP=/llama-cpp
REMOTE_BF16_BASELINE=__BF16_BASELINE_ROOT__
WORKSPACE_ROOT=/workspace/scriber-full-120k-v2-quantization
WORK_ROOT="$WORKSPACE_ROOT/repo"
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
LOCAL_MODEL="$WORKSPACE_ROOT/source-model"
STAGE_ROOT="$WORKSPACE_ROOT/result"
VENV_ROOT=/tmp/scriber-quantization-runtime
OUTPUT_ROOT=__OUTPUT_ROOT__
EXPECTED_PACKET_TREE_SHA256="${1:-}"
EXPECTED_MANIFEST_SHA256="${2:-}"
LAUNCH_CLAIM_OWNER="${3:-}"
PUBLISH_STAGE="$(dirname "$OUTPUT_ROOT")/.staging/${LAUNCH_CLAIM_OWNER}-${EXPECTED_PACKET_TREE_SHA256#sha256:}"

if [ -z "$EXPECTED_PACKET_TREE_SHA256" ] || \
   [ "$SCRIBER_TRUSTED_QUANTIZATION_PACKET_TREE_SHA256" != "$EXPECTED_PACKET_TREE_SHA256" ] || \
   [ -z "$EXPECTED_MANIFEST_SHA256" ] || \
   [ "$SCRIBER_TRUSTED_QUANTIZATION_MANIFEST_SHA256" != "$EXPECTED_MANIFEST_SHA256" ] || \
   [ -z "$LAUNCH_CLAIM_OWNER" ] || \
   [ "$SCRIBER_QUANTIZATION_V2_LAUNCH_CLAIM_OWNER" != "$LAUNCH_CLAIM_OWNER" ]; then
  echo "trusted quantization bootstrap evidence is required" >&2
  exit 1
fi
for required in "$SOURCE_REPO" "$REMOTE_MODEL" "$REMOTE_LLAMA_CPP" "$REMOTE_BF16_BASELINE"; do
  if [ ! -d "$required" ]; then
    echo "required read-only mount is missing: $required" >&2
    exit 1
  fi
done
if [ -e "$WORKSPACE_ROOT" ] || [ -e "$VENV_ROOT" ] || [ -e "$OUTPUT_ROOT" ] || [ -e "$PUBLISH_STAGE" ]; then
  echo "refusing to overwrite quantization workspace, isolated runtime, owner stage, or completed output" >&2
  exit 1
fi

# Admission is intentionally the first executable gate.  The packet is bound
# to exactly one L4 (Ada, compute capability 8.9); do not spend time installing
# packages or copying the model on any other worker shape.
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
python -I - <<'PY'
import ctypes

driver = ctypes.CDLL("libcuda.so.1", mode=ctypes.RTLD_LOCAL)
driver.cuInit.argtypes = [ctypes.c_uint]
driver.cuInit.restype = ctypes.c_int
initialization = driver.cuInit(0)
if initialization != 0:
    raise SystemExit(f"CUDA driver cuInit(0) failed: {initialization}")

driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
driver.cuDeviceGetCount.restype = ctypes.c_int
device_count = ctypes.c_int()
enumeration = driver.cuDeviceGetCount(ctypes.byref(device_count))
if enumeration != 0 or device_count.value != 1:
    raise SystemExit(
        "exact L4 admission failed: "
        f"device enumeration status={enumeration}, count={device_count.value}"
    )

device = ctypes.c_int()
driver.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
driver.cuDeviceGet.restype = ctypes.c_int
selection = driver.cuDeviceGet(ctypes.byref(device), 0)
if selection != 0:
    raise SystemExit(f"exact L4 admission failed: cuDeviceGet(0)={selection}")

name_buffer = ctypes.create_string_buffer(96)
driver.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
driver.cuDeviceGetName.restype = ctypes.c_int
name_status = driver.cuDeviceGetName(name_buffer, len(name_buffer), device.value)
gpu_name = name_buffer.value.decode("utf-8", errors="strict")

driver.cuDeviceGetAttribute.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
    ctypes.c_int,
]
driver.cuDeviceGetAttribute.restype = ctypes.c_int
major = ctypes.c_int()
minor = ctypes.c_int()
# CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR/MINOR from cuda.h.
major_status = driver.cuDeviceGetAttribute(ctypes.byref(major), 75, device.value)
minor_status = driver.cuDeviceGetAttribute(ctypes.byref(minor), 76, device.value)
if (
    name_status != 0
    or major_status != 0
    or minor_status != 0
    or gpu_name != "NVIDIA L4"
    or (major.value, minor.value) != (8, 9)
):
    raise SystemExit(
        "exact L4 admission failed: "
        f"name_status={name_status}, name={gpu_name!r}, "
        f"cc_status=({major_status},{minor_status}), "
        f"cc={major.value}.{minor.value}"
    )
print("cuda_admission=verified devices=1 name=NVIDIA_L4 compute_capability=8.9")
PY

python -I -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "isolated quantization Python was not created" >&2
  exit 1
fi
"$PYTHON" -I - <<'PY'
import pathlib
import sys

prefix = pathlib.Path(sys.prefix).resolve()
base_prefix = pathlib.Path(sys.base_prefix).resolve()
if sys.version_info[:2] != (3, 12) or prefix == base_prefix:
    raise SystemExit(
        f"isolated CPython 3.12 admission failed: prefix={prefix}, base={base_prefix}, "
        f"version={sys.version_info[:2]}"
    )
PY

PIP_NO_INDEX=1 "$PYTHON" -m pip install \
  --isolated \
  --no-cache-dir \
  --no-index \
  --require-hashes \
  --force-reinstall \
  --ignore-installed \
  --no-deps \
  --find-links "$PACKET_ROOT/input/wheels" \
  --requirement "$PACKET_ROOT/input/quantization-requirements.txt"

"$PYTHON" -I - <<'PY'
import importlib.metadata
import pathlib
import sys

expected = {
    "accelerate": "1.14.0",
    "bitsandbytes": "0.49.2",
    "huggingface-hub": "0.36.0",
    "jsonschema": "4.26.0",
    "numpy": "2.5.1",
    "pyyaml": "6.0.3",
    "safetensors": "0.8.0",
    "sentencepiece": "0.2.2",
    "transformers": "4.57.6",
}
prefix = pathlib.Path(sys.prefix).resolve()
for name, version in expected.items():
    distribution = importlib.metadata.distribution(name)
    location = pathlib.Path(distribution.locate_file("")).resolve()
    if distribution.version != version or not location.is_relative_to(prefix):
        raise SystemExit(
            f"isolated dependency admission failed: {name}=={distribution.version} at {location}"
        )
PY

export PYTHONPATH="$SOURCE_REPO/ml/scriber_polishing/src"
"$PYTHON" "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_quantization.py" \
  --mode packet \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256"

"$PYTHON" "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_quantization.py" \
  --mode bf16-baseline \
  --manifest "$PACKET_ROOT/job-input/hf-quantization-plan.json" \
  --bf16-baseline-root "$REMOTE_BF16_BASELINE"

mkdir -p "$WORKSPACE_ROOT"
cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export NO_PROXY='*'

LLAMA_CPP_LIBRARY_PATH="$(
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode llama-library-path \
    --manifest job-input/hf-quantization-plan.json \
    --llama-cpp-bundle "$REMOTE_LLAMA_CPP" \
    --llama-cpp-bundle-lock "$REMOTE_LLAMA_CPP/llama-cpp-bundle-lock.json"
)"
if [ -z "$LLAMA_CPP_LIBRARY_PATH" ]; then
  echo "verified llama.cpp library path is empty" >&2
  exit 1
fi
CUDA_RUNTIME_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib"
CUDA_RUNTIME_LIBRARY="$CUDA_RUNTIME_LIBRARY_PATH/libcudart.so.12"
test -f "$CUDA_RUNTIME_LIBRARY"
test "$(dirname "$(readlink -f "$CUDA_RUNTIME_LIBRARY")")" = "$CUDA_RUNTIME_LIBRARY_PATH"
CUBLAS_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib"
CUBLAS_LIBRARY="$CUBLAS_LIBRARY_PATH/libcublas.so.12"
test -f "$CUBLAS_LIBRARY"
test "$(dirname "$(readlink -f "$CUBLAS_LIBRARY")")" = "$CUBLAS_LIBRARY_PATH"
NCCL_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib"
NCCL_LIBRARY="$NCCL_LIBRARY_PATH/libnccl.so.2"
test -f "$NCCL_LIBRARY"
test "$(dirname "$(readlink -f "$NCCL_LIBRARY")")" = "$NCCL_LIBRARY_PATH"
export LD_LIBRARY_PATH="$LLAMA_CPP_LIBRARY_PATH:$CUDA_RUNTIME_LIBRARY_PATH:$CUBLAS_LIBRARY_PATH:$NCCL_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$PYTHON" scripts/verify_hf_quantization.py \
  --mode llama-cpp \
  --manifest job-input/hf-quantization-plan.json \
  --llama-cpp-bundle "$REMOTE_LLAMA_CPP" \
  --llama-cpp-bundle-lock "$REMOTE_LLAMA_CPP/llama-cpp-bundle-lock.json" \
  --output "$WORKSPACE_ROOT/llama-cpp-preflight.json"

cp -a --no-preserve=ownership "$REMOTE_MODEL" "$LOCAL_MODEL"
"$PYTHON" scripts/verify_hf_quantization.py \
  --mode source-model \
  --manifest job-input/hf-quantization-plan.json \
  --model "$LOCAL_MODEL" \
  --output "$WORKSPACE_ROOT/source-model-preflight.json"

verify_variant() {
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode variant \
    --manifest job-input/hf-quantization-plan.json \
    --output-root "$STAGE_ROOT" \
    --variant "$1"
}

mkdir -p "$STAGE_ROOT/variants"
SOURCE_ID="scriber-full-120k-v2:$(
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode source-id \
    --manifest job-input/hf-quantization-plan.json
)"
TRAINING_FINGERPRINT="$(
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode training-fingerprint \
    --manifest job-input/hf-quantization-plan.json
)"

for VARIANT in int8 int4_nf4; do
  if [ -e "$STAGE_ROOT/variants/$VARIANT" ]; then
    verify_variant "$VARIANT"
  else
    "$PYTHON" scripts/materialize_quantized_model.py \
      --checkpoint "$LOCAL_MODEL" \
      --source-id "$SOURCE_ID" \
      --training-fingerprint "$TRAINING_FINGERPRINT" \
      --variant "$VARIANT" \
      --output "$STAGE_ROOT/variants/$VARIANT"
    verify_variant "$VARIANT"
  fi
done
if [ -e "$STAGE_ROOT/variants/gguf/Q8_0" ] || \
   [ -e "$STAGE_ROOT/variants/gguf/Q4_K_M" ]; then
  if [ ! -d "$STAGE_ROOT/variants/gguf/Q8_0" ] || \
     [ ! -d "$STAGE_ROOT/variants/gguf/Q4_K_M" ]; then
    echo "durable GGUF resume is incomplete" >&2
    exit 1
  fi
  verify_variant Q8_0
  verify_variant Q4_K_M
else
  "$PYTHON" scripts/build_gguf.py \
    --checkpoint "$LOCAL_MODEL" \
    --source-id "$SOURCE_ID" \
    --training-fingerprint "$TRAINING_FINGERPRINT" \
    --llama-cpp-bundle "$REMOTE_LLAMA_CPP" \
    --llama-cpp-bundle-lock "$REMOTE_LLAMA_CPP/llama-cpp-bundle-lock.json" \
    --python-executable "$PYTHON" \
    --output "$STAGE_ROOT/variants/gguf"
  verify_variant Q8_0
  verify_variant Q4_K_M
fi

evaluate_lane() {
  local variant="$1"
  local suite_id="$2"
  local references="$3"
  local lane_root="$STAGE_ROOT/evaluation/$variant/$suite_id"
  mkdir -p "$lane_root"
  set +e
  "$PYTHON" scripts/evaluate_predictions.py \
    --references "$references" \
    --predictions "$lane_root/predictions.jsonl" \
    --candidate "$variant" \
    --config configs/evaluation.yaml \
    --output "$lane_root/evaluation.json"
  local evaluation_exit=$?
  set -e
  if [ "$evaluation_exit" -ne 0 ] && [ "$evaluation_exit" -ne 2 ]; then
    exit "$evaluation_exit"
  fi
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode record-exit \
    --variant "$variant" \
    --suite "$suite_id" \
    --evaluation-exit "$evaluation_exit" \
    --output "$lane_root/evaluation-exit.json"
}

run_transformers_lane() {
  local variant="$1"
  local suite_id="$2"
  local references="$3"
  local model="$STAGE_ROOT/variants/$variant"
  local quantization="$variant"
  local lane_root="$STAGE_ROOT/evaluation/$variant/$suite_id"
  mkdir -p "$lane_root"
  "$PYTHON" -m scriber_polishing.inference \
    --model "$model" \
    --references "$references" \
    --predictions-output "$lane_root/predictions.jsonl" \
    --manifest-output "$lane_root/predictions.manifest.json" \
    --candidate "$variant" \
    --resume-journal "$lane_root/predictions.resume.json" \
    --batch-size 8 \
    --max-new-tokens 512 \
    --device cuda \
    --quantization "$quantization" \
    --local-files-only \
    --protection-policy-artifact "$model/scriber-protection-policy.json"
  evaluate_lane "$variant" "$suite_id" "$references"
}

run_gguf_lane() {
  local variant="$1"
  local suite_id="$2"
  local references="$3"
  local model="$STAGE_ROOT/variants/gguf/$variant"
  local lane_root="$STAGE_ROOT/evaluation/$variant/$suite_id"
  mkdir -p "$lane_root"
  "$PYTHON" scripts/run_hf_quantization_gguf_inference.py \
    --variant "$variant" \
    --suite "$suite_id" \
    --model-root "$model" \
    --references "$references" \
    --llama-cpp-bundle "$REMOTE_LLAMA_CPP" \
    --llama-cpp-bundle-lock "$REMOTE_LLAMA_CPP/llama-cpp-bundle-lock.json" \
    --predictions-output "$lane_root/predictions.jsonl" \
    --manifest-output "$lane_root/predictions.manifest.json"
  evaluate_lane "$variant" "$suite_id" "$references"
}

while IFS=$'\t' read -r SUITE_ID REFERENCE_FILENAME; do
  REFERENCES="evaluation-input/$REFERENCE_FILENAME"
  for VARIANT in int8 int4_nf4 Q8_0 Q4_K_M; do
    LANE_ROOT="$STAGE_ROOT/evaluation/$VARIANT/$SUITE_ID"
    if [ -e "$LANE_ROOT" ]; then
      "$PYTHON" scripts/verify_hf_quantization.py \
        --mode lane \
        --manifest job-input/hf-quantization-plan.json \
        --output-root "$STAGE_ROOT" \
        --variant "$VARIANT" \
        --suite "$SUITE_ID" \
        --bf16-baseline-root "$REMOTE_BF16_BASELINE"
      continue
    fi
    if [ "$VARIANT" = Q8_0 ] || [ "$VARIANT" = Q4_K_M ]; then
      run_gguf_lane "$VARIANT" "$SUITE_ID" "$REFERENCES"
    else
      run_transformers_lane "$VARIANT" "$SUITE_ID" "$REFERENCES"
    fi
    "$PYTHON" scripts/build_quality_delta_report.py \
      --baseline "$REMOTE_BF16_BASELINE/$SUITE_ID/evaluation.json" \
      --candidate "$STAGE_ROOT/evaluation/$VARIANT/$SUITE_ID/evaluation.json" \
      --candidate-variant "$VARIANT" \
      --output "$STAGE_ROOT/evaluation/$VARIANT/$SUITE_ID/quality-delta.json"
    "$PYTHON" scripts/verify_hf_quantization.py \
      --mode lane \
      --manifest job-input/hf-quantization-plan.json \
      --output-root "$STAGE_ROOT" \
      --variant "$VARIANT" \
      --suite "$SUITE_ID" \
      --bf16-baseline-root "$REMOTE_BF16_BASELINE"
  done
done < <(
  "$PYTHON" scripts/verify_hf_quantization.py \
    --mode list-suites \
    --manifest job-input/hf-quantization-plan.json
)

cp job-input/hf-quantization-plan.json "$STAGE_ROOT/"
cp "$WORKSPACE_ROOT/source-model-preflight.json" "$STAGE_ROOT/"
cp "$WORKSPACE_ROOT/llama-cpp-preflight.json" "$STAGE_ROOT/"
"$PYTHON" scripts/verify_hf_quantization.py \
  --mode finalize \
  --manifest job-input/hf-quantization-plan.json \
  --output-root "$STAGE_ROOT" \
  --bf16-baseline-root "$REMOTE_BF16_BASELINE" \
  --output "$STAGE_ROOT/hf-quantization-result.json"

mkdir -p "$(dirname "$PUBLISH_STAGE")"
cp -a --no-preserve=ownership "$STAGE_ROOT" "$PUBLISH_STAGE"
"$PYTHON" scripts/verify_hf_quantization.py \
  --mode publish \
  --manifest "$PUBLISH_STAGE/hf-quantization-plan.json" \
  --output-root "$PUBLISH_STAGE" \
  --destination-root "$OUTPUT_ROOT" \
  --bf16-baseline-root "$REMOTE_BF16_BASELINE" \
  --result "$PUBLISH_STAGE/hf-quantization-result.json" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE_SHA256" \
  --owner-token "$LAUNCH_CLAIM_OWNER"
"$PYTHON" scripts/verify_hf_quantization.py \
  --mode result \
  --manifest "$OUTPUT_ROOT/hf-quantization-plan.json" \
  --output-root "$OUTPUT_ROOT" \
  --bf16-baseline-root "$REMOTE_BF16_BASELINE" \
  --result "$OUTPUT_ROOT/hf-quantization-result.json"

echo "cloud_quantization=complete quantized_variants=4 bf16_baseline=immutable_existing_evidence training_steps_executed=0" >&2
"""
    return (
        script.replace("__OUTPUT_ROOT__", QUANTIZATION_OUTPUT_ROOT)
        .replace("__BF16_BASELINE_ROOT__", FULL_MODEL_BF16_BASELINE_ROOT)
        .replace("\r\n", "\n")
        .encode("utf-8")
    )


def read_json_object(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    """Read stable regular-file JSON for packet preparation."""

    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise HfQuantizationError(f"{label} must be a regular file: {candidate}")
    before = candidate.stat()
    try:
        payload = candidate.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HfQuantizationError(f"{label} is invalid JSON: {error}") from error
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
        raise HfQuantizationError(f"{label} changed while it was read")
    if not isinstance(value, dict):
        raise HfQuantizationError(f"{label} must contain an object")
    return value, payload


def canonical_sha256(value: object) -> str:
    """Hash canonical JSON with the repository's sha256: convention."""

    return _sha256(_canonical_bytes(value))


def _write_immutable_json(path: str | Path, value: object) -> Path:
    destination = Path(path).expanduser().resolve()
    payload = _canonical_bytes(value)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise HfQuantizationError(f"refusing to overwrite different output: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        raise HfQuantizationError(f"temporary quantization output already exists: {temporary}")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)
    return destination


def validate_hf_quantization_plan(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Validate the closed dry-bind surface before any mount is consumed."""

    plan = dict(_mapping(value, "HF quantization plan"))
    schema_version = plan.get("schema_version")
    if schema_version not in {1, 2}:
        raise HfQuantizationError("HF quantization plan schema version is unsupported")
    evaluation_binding_key = (
        "verified_final_evaluation"
        if schema_version == 2
        else "successful_final_evaluation"
    )
    expected = {
        "schema_version",
        "kind",
        "source_git_head",
        "image",
        "flavor",
        "timeout_minutes",
        "source_model",
        evaluation_binding_key,
        "actual_cost_evidence",
        "llama_cpp",
        "references",
        "evaluation",
        "variants",
        "budget",
        "output",
        "publication",
    }
    if schema_version == 2:
        expected.add("immutable_bf16_baseline")
    if set(plan) != expected:
        raise HfQuantizationError("HF quantization plan is not closed")
    if (
        plan.get("kind") != "scriber_hf_quantization_plan"
        or not isinstance(plan.get("source_git_head"), str)
        or _COMMIT.fullmatch(str(plan["source_git_head"])) is None
        or plan.get("image") != QUANTIZATION_IMAGE
        or plan.get("flavor") != QUANTIZATION_FLAVOR
        or plan.get("timeout_minutes") != QUANTIZATION_TIMEOUT_MINUTES
        or plan.get("publication")
        != {
            "allowed": False,
            "destination_kind": "private_hf_bucket",
        }
    ):
        raise HfQuantizationError("HF quantization plan identity is invalid")
    source = _mapping(plan.get("source_model"), "quantization source model")
    if schema_version == 2:
        expected_source = {
            "schema_version",
            "kind",
            "training_regime",
            "completed_updates",
            "replayed_updates",
            "resume_checkpoint",
            "parent_initialization",
            "final_model_tree_sha256",
            "model_weight_sha256",
            "model_weight_bytes",
            "model_weight_filename",
            "training_fingerprint",
            "remote_uri",
            "mount_mode",
            "cost_anchor_continuation_result_sha256",
            "training_completion_sha256",
            "protection_policy",
        }
        if (
            set(source) != expected_source
            or source.get("schema_version") != 2
            or source.get("kind") != "scriber_hf_quantization_source_model"
            or source.get("training_regime") != "full_120k_sft"
            or isinstance(source.get("completed_updates"), bool)
            or not isinstance(source.get("completed_updates"), int)
            or int(source["completed_updates"]) <= 1000
            or source.get("replayed_updates") != 0
            or source.get("resume_checkpoint") != "checkpoint-1000"
            or source.get("parent_initialization") != "same_run_checkpoint_resume"
            or source.get("mount_mode") != "read_only"
            or source.get("protection_policy") != frozen_lexical_policy_binding()
            or isinstance(source.get("model_weight_bytes"), bool)
            or not isinstance(source.get("model_weight_bytes"), int)
            or int(source["model_weight_bytes"]) < 1
            or source.get("model_weight_filename") != FULL_MODEL_WEIGHT_FILENAME
            or source.get("remote_uri") != FULL_MODEL_SOURCE_REMOTE_URI
            or source.get("final_model_tree_sha256")
            != FULL_MODEL_SOURCE_TREE_SHA256
        ):
            raise HfQuantizationError("full quantization source-model binding is invalid")
        _prefixed_hash(source.get("model_weight_sha256"), "quantization model weight")
        continuation_result_hash = _prefixed_hash(
            source.get("cost_anchor_continuation_result_sha256"),
            "quantization historical cost anchor",
        )
        if continuation_result_hash != _COST_ANCHOR_CONTINUATION_RESULT_SHA256:
            raise HfQuantizationError("quantization historical cost anchor is invalid")
        _prefixed_hash(
            source.get("training_completion_sha256"),
            "quantization full-training completion",
        )
    else:
        expected_source = {
            "schema_version",
            "kind",
            "cumulative_updates",
            "replayed_updates",
            "parent_initialization",
            "final_model_tree_sha256",
            "training_fingerprint",
            "remote_uri",
            "mount_mode",
            "continuation_result_sha256",
            "training_report_sha256",
            "protection_policy",
        }
        if (
            set(source) != expected_source
            or source.get("schema_version") != 1
            or source.get("kind") != "scriber_hf_quantization_source_model"
            or source.get("cumulative_updates") != 1100
            or source.get("replayed_updates") != 0
            or source.get("parent_initialization") != "warm_start_weights_only"
            or source.get("mount_mode") != "read_only"
            or source.get("protection_policy") != frozen_lexical_policy_binding()
        ):
            raise HfQuantizationError("quantization source-model binding is invalid")
        continuation_result_hash = _prefixed_hash(
            source.get("continuation_result_sha256"),
            "quantization continuation result",
        )
        _prefixed_hash(
            source.get("training_report_sha256"),
            "quantization training report",
        )
    _prefixed_hash(
        source.get("final_model_tree_sha256"),
        "quantization source tree",
    )
    _prefixed_hash(
        source.get("training_fingerprint"),
        "quantization training fingerprint",
    )
    _private_bucket_uri(source.get("remote_uri"), "quantization source model")
    successful = _mapping(
        plan.get(evaluation_binding_key),
        evaluation_binding_key.replace("_", " "),
    )
    expected_evaluation_binding = (
        {
            "result_sha256",
            "result_bytes",
            "manifest_sha256",
            "manifest_bytes",
            "required_suites",
            "training_steps_executed",
            "model_tree_sha256",
            "release_eligibility",
            "quantization_scope",
        }
        if schema_version == 2
        else {
            "result_sha256",
            "manifest_sha256",
            "required_suites",
            "all_final_gates_passed",
        }
    )
    if set(successful) != expected_evaluation_binding:
        raise HfQuantizationError("final-evaluation binding is not closed")
    final_evaluation_result_hash = _prefixed_hash(
        successful.get("result_sha256"),
        "successful final-evaluation result",
    )
    _prefixed_hash(
        successful.get("manifest_sha256"),
        "successful final-evaluation manifest",
    )
    if schema_version == 2 and (
        isinstance(successful.get("result_bytes"), bool)
        or not isinstance(successful.get("result_bytes"), int)
        or int(successful["result_bytes"]) < 1
        or isinstance(successful.get("manifest_bytes"), bool)
        or not isinstance(successful.get("manifest_bytes"), int)
        or int(successful["manifest_bytes"]) < 1
    ):
        raise HfQuantizationError("full-model final-evaluation byte binding is invalid")
    if successful.get("required_suites") != list(REFERENCE_SUITES):
        raise HfQuantizationError("final-evaluation suite binding is invalid")
    if schema_version == 2:
        if (
            successful.get("training_steps_executed") != 0
            or successful.get("model_tree_sha256")
            != source.get("final_model_tree_sha256")
            or successful.get("release_eligibility") != "not_granted"
            or successful.get("quantization_scope")
            != "private_candidate_comparison_only"
        ):
            raise HfQuantizationError("full-model final-evaluation binding is invalid")
        _validate_immutable_bf16_baseline_binding(
            _mapping(
                plan.get("immutable_bf16_baseline"),
                "immutable BF16 evaluation baseline",
            ),
            source_model_tree_sha256=source.get("final_model_tree_sha256"),
            final_evaluation_result_sha256=successful.get("result_sha256"),
            final_evaluation_manifest_sha256=successful.get("manifest_sha256"),
            final_evaluation_result_bytes=successful.get("result_bytes"),
            final_evaluation_manifest_bytes=successful.get("manifest_bytes"),
            model_weight_sha256=source.get("model_weight_sha256"),
            model_weight_bytes=source.get("model_weight_bytes"),
        )
    elif successful.get("all_final_gates_passed") is not True:
        raise HfQuantizationError("successful final-evaluation binding is invalid")
    llama = _mapping(plan.get("llama_cpp"), "quantization llama.cpp")
    raw_llama = {key: value for key, value in llama.items() if key not in {"remote_uri", "mount_mode"}}
    checked_llama = _validate_llama_cpp_binding(
        raw_llama,
        remote_uri=str(llama.get("remote_uri", "")),
    )
    if dict(llama) != checked_llama:
        raise HfQuantizationError("llama.cpp binding is not canonical")
    cost_evidence = _mapping(
        plan.get("actual_cost_evidence"),
        "HF actual-cost evidence",
    )
    budget = _mapping(plan.get("budget"), "HF quantization budget")
    expected_budget = build_hf_quantization_budget(
        cost_evidence,
        evidence_sha256=str(budget.get("actual_cost_evidence_sha256", "")),
        expected_continuation_result_sha256=continuation_result_hash,
        expected_final_evaluation_result_sha256=final_evaluation_result_hash,
        expected_toolchain_materialization=_mapping(
            checked_llama["materialization"],
            "llama.cpp toolchain materialization",
        ),
        use_remaining_credit_authorization=schema_version == 2,
    )
    if dict(budget) != expected_budget:
        raise HfQuantizationError("HF quantization budget differs from actual-cost evidence")
    references = _mapping(plan.get("references"), "quantization references")
    if set(references) != set(REFERENCE_SUITES):
        raise HfQuantizationError("quantization references are incomplete")
    evaluation = _mapping(plan.get("evaluation"), "quantization evaluation")
    expected_evaluation = {
        "config_filename": "evaluation.yaml",
        "config_sha256": str(evaluation.get("config_sha256")),
        "max_new_tokens": 512,
        "transformers_batch_size": 8,
        "gguf_parallel_slots": 1,
        "fresh_quantized_variants": list(QUANTIZED_VARIANTS),
        "quality_delta_baseline": "bf16",
        "quality_delta_budgets": QUALITY_DELTA_BUDGETS,
    }
    if schema_version == 2:
        expected_evaluation.update(
            {
                "absolute_gate_policy": "record_only_private_candidate",
                "bf16_baseline_mode": "immutable_existing_evidence",
                "bf16_execution_prohibited": [
                    "materialization",
                    "inference",
                    "evaluation",
                ],
            }
        )
    else:
        expected_evaluation["fresh_for_every_variant"] = True
    if evaluation != expected_evaluation:
        raise HfQuantizationError("quantization evaluation policy is not closed")
    _raw_hash(
        evaluation.get("config_sha256"),
        "quantization evaluation configuration",
    )
    variant_rows = plan.get("variants")
    expected_variants = (
        [
            {
                "id": "bf16",
                "backend": "verified_final_evaluation_evidence",
                "evaluation": "immutable_existing_baseline",
            },
            {
                "id": "int8",
                "backend": "transformers_bitsandbytes",
                "evaluation": "fresh_full",
            },
            {
                "id": "int4_nf4",
                "backend": "transformers_bitsandbytes",
                "evaluation": "fresh_full",
            },
            {"id": "Q8_0", "backend": "llama_cpp", "evaluation": "fresh_full"},
            {"id": "Q4_K_M", "backend": "llama_cpp", "evaluation": "fresh_full"},
        ]
        if schema_version == 2
        else [
            {"id": "bf16", "backend": "transformers", "evaluation": "fresh_full"},
            {
                "id": "int8",
                "backend": "transformers_bitsandbytes",
                "evaluation": "fresh_full",
            },
            {
                "id": "int4_nf4",
                "backend": "transformers_bitsandbytes",
                "evaluation": "fresh_full",
            },
            {"id": "Q8_0", "backend": "llama_cpp", "evaluation": "fresh_full"},
            {"id": "Q4_K_M", "backend": "llama_cpp", "evaluation": "fresh_full"},
        ]
    )
    if variant_rows != expected_variants:
        raise HfQuantizationError("quantization variant plan is incomplete")
    output = _mapping(plan.get("output"), "quantization output")
    expected_output = {
        "bucket_root": QUANTIZATION_OUTPUT_BUCKET,
        "relative_path": QUANTIZATION_OUTPUT_RELATIVE,
        "container_path": QUANTIZATION_OUTPUT_ROOT,
        "unique": True,
        "overwrite_allowed": False,
    }
    if schema_version == 2:
        expected_output.update(
            {
                "duplicate_launch_allowed": False,
                "durable_resume_allowed": False,
                "completed_output_never_overwritten": True,
            }
        )
    if output != expected_output:
        raise HfQuantizationError("quantization output is not the unique private path")
    return plan


def build_cloud_llama_cpp_binding(
    bundle_root: str | Path,
    bundle_lock: str | Path,
) -> dict[str, object]:
    """Bind one executable Linux CUDA llama.cpp bundle for the L4 job."""

    try:
        bundle = load_verified_llama_cpp_bundle(bundle_root, bundle_lock)
    except LlamaCppBundleError as error:
        raise HfQuantizationError(f"llama.cpp cloud bundle verification failed: {error}") from error
    cuda = _mapping(bundle.build.get("cuda"), "llama.cpp CUDA build")
    if cuda.get("enabled") is not True or not isinstance(
        cuda.get("version"),
        str,
    ):
        raise HfQuantizationError("llama.cpp cloud bundle must be CUDA-enabled")
    if (
        bundle.commit != TOOLCHAIN_COMMIT
        or bundle.distribution.get("tag") != TAG
        or bundle.distribution.get("index_digest") != INDEX_DIGEST
        or bundle.distribution.get("manifest_digest") != MANIFEST_DIGEST
        or bundle.distribution.get("config_digest") != CONFIG_DIGEST
        or bundle.distribution.get("runtime_image") != QUANTIZATION_IMAGE
        or bundle.distribution.get("platform") != {"os": "linux", "architecture": "amd64"}
        or bundle.distribution.get("revision") != bundle.commit
    ):
        raise HfQuantizationError("llama.cpp OCI runtime identity is not the exact reviewed b10156 CUDA bundle")
    runtime_tree = _prefixed_hash(
        bundle.native_runtime.get("tree_sha256"),
        "llama.cpp native runtime tree",
    )
    closure = _mapping(
        bundle.native_runtime.get("ldd_closure"),
        "llama.cpp locked ldd closure",
    )
    required_host_drivers = closure.get("required_host_driver_sonames")
    if required_host_drivers != list(CUDA_HOST_DRIVER_SONAMES):
        raise HfQuantizationError(
            "llama.cpp closure does not bind the exact CUDA host driver"
        )
    closure_hash = _sha256(
        json.dumps(
            closure,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )
    library_directories = bundle.native_runtime.get("library_directories")
    if (
        not isinstance(library_directories, list)
        or not library_directories
        or any(not isinstance(relative, str) or not relative for relative in library_directories)
    ):
        raise HfQuantizationError("llama.cpp native library directories are incomplete")
    tools = {
        "convert_hf_to_gguf": bundle.convert_hf_to_gguf,
        "llama_quantize": bundle.llama_quantize,
        "llama_cli": bundle.llama_cli,
        "llama_server": bundle.llama_server,
    }
    hashes: dict[str, str] = {}
    for name, path in tools.items():
        header = path.read_bytes()[:4]
        if name != "convert_hf_to_gguf" and header != b"\x7fELF":
            raise HfQuantizationError(f"llama.cpp {name} is not a Linux ELF executable")
        if name != "convert_hf_to_gguf" and not os.access(path, os.X_OK):
            raise HfQuantizationError(f"llama.cpp {name} is not executable")
        hashes[name] = _sha256(path.read_bytes())
    try:
        materialization = toolchain_materialization_binding(bundle.root)
    except HfLlamaCppToolchainError as error:
        raise HfQuantizationError(f"llama.cpp cloud bundle lacks verified GPU materialization: {error}") from error
    return {
        "schema_version": 1,
        "kind": "scriber_cloud_llama_cpp_binding",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "platform": "linux_x86_64_cuda",
        "lock_sha256": bundle.lock_sha256,
        "commit": bundle.commit,
        "source_tree_sha256": bundle.source_tree_sha256,
        "build": reviewed_toolchain_build_binding(),
        "build_identity_sha256": bundle.build_identity_sha256,
        "cuda_enabled": True,
        "distribution": bundle.distribution,
        "native_runtime_tree_sha256": runtime_tree,
        "ldd_closure_sha256": closure_hash,
        "required_host_driver_sonames": list(required_host_drivers),
        "runtime_library_directories": list(library_directories),
        "tool_sha256": dict(sorted(hashes.items())),
        "materialization": materialization,
    }


def build_cloud_llama_cpp_binding_from_materialization_result(
    result_path: str | Path,
    *,
    remote_uri: str,
    expected_result_sha256: str,
    expected_job_id: str,
    expected_claim_commit: str,
    expected_claim_owner: str,
) -> dict[str, object]:
    """Bind a unique cloud-produced toolchain result without downloading its payload.

    The quantization runner still re-verifies the complete read-only bundle before
    conversion or inference. This local path binds the small canonical result and
    avoids a wasteful round trip of the just-produced toolchain directory.
    """

    result, payload = read_json_object(
        result_path,
        "HF llama.cpp toolchain materialization result",
    )
    result_sha256 = _sha256(payload)
    if (
        _prefixed_hash(
            expected_result_sha256,
            "expected toolchain materialization result",
        )
        != result_sha256
        or _JOB_ID.fullmatch(expected_job_id) is None
        or _COMMIT.fullmatch(expected_claim_commit) is None
        or _OWNER_TOKEN.fullmatch(expected_claim_owner) is None
    ):
        raise HfQuantizationError(
            "external toolchain result provenance is incomplete or differs"
        )
    required = {
        "schema_version",
        "kind",
        "attempt",
        "source_git_head",
        "status",
        "platform",
        "image",
        "flavor",
        "timeout_minutes",
        "packet_tree_sha256",
        "bundle_payload_inventory",
        "lock_filename",
        "lock_sha256",
        "commit",
        "source_tree_sha256",
        "build_identity_sha256",
        "build",
        "distribution",
        "native_runtime_tree_sha256",
        "ldd_closure_sha256",
        "required_host_driver_sonames",
        "runtime_library_directories",
        "tool_sha256",
        "runtime_closure_verified",
        "training_steps_executed",
        "evaluation_reports_written",
        "prediction_records_written",
        "publication_allowed",
    }
    inventory = _mapping(
        result.get("bundle_payload_inventory"),
        "toolchain bundle payload inventory",
    )
    build = _mapping(result.get("build"), "toolchain build")
    cuda = _mapping(build.get("cuda"), "toolchain CUDA build")
    if (
        payload != _canonical_bytes(result)
        or set(result) != required
        or result.get("schema_version") != 1
        or result.get("kind") != "scriber_hf_llama_cpp_toolchain_result"
        or result.get("attempt") != TOOLCHAIN_ATTEMPT
        or result.get("source_git_head") != REVIEWED_SCRIBER_GIT_HEAD
        or result.get("status") != "complete"
        or result.get("platform") != "linux_x86_64_cuda"
        or result.get("image") != TOOLCHAIN_IMAGE
        or result.get("flavor") != TOOLCHAIN_FLAVOR
        or result.get("timeout_minutes") != TOOLCHAIN_TIMEOUT_MINUTES
        or result.get("lock_filename") != "llama-cpp-bundle-lock.json"
        or result.get("commit") != TOOLCHAIN_COMMIT
        or dict(build) != reviewed_toolchain_build_binding()
        or remote_uri != TOOLCHAIN_REMOTE_OUTPUT_URI
        or result.get("runtime_closure_verified") is not True
        or result.get("training_steps_executed") != 0
        or result.get("evaluation_reports_written") != 0
        or result.get("prediction_records_written") != 0
        or result.get("publication_allowed") is not False
        or cuda.get("enabled") is not True
        or not isinstance(cuda.get("version"), str)
        or set(inventory)
        != {
            "algorithm",
            "tree_sha256",
            "entry_count",
            "total_file_bytes",
        }
        or inventory.get("algorithm")
        != "sha256-canonical-path-kind-size-content-or-target-v1"
        or isinstance(inventory.get("entry_count"), bool)
        or not isinstance(inventory.get("entry_count"), int)
        or int(inventory["entry_count"]) < 1
        or isinstance(inventory.get("total_file_bytes"), bool)
        or not isinstance(inventory.get("total_file_bytes"), int)
        or int(inventory["total_file_bytes"]) < 1
    ):
        raise HfQuantizationError(
            "HF llama.cpp toolchain materialization result is incomplete or not canonical"
        )
    packet_tree = _prefixed_hash(
        result.get("packet_tree_sha256"),
        "toolchain packet tree",
    )
    bundle_tree = _prefixed_hash(
        inventory.get("tree_sha256"),
        "toolchain bundle payload tree",
    )
    external_provenance_unsigned = {
        "schema_version": 1,
        "kind": "scriber_hf_toolchain_v19_external_provenance",
        "result_sha256": result_sha256,
        "producer_job_id": expected_job_id,
        "claim_commit": expected_claim_commit,
        "claim_owner": expected_claim_owner,
        "remote_uri": remote_uri,
    }
    external_provenance = {
        **external_provenance_unsigned,
        "binding_sha256": _sha256(
            _canonical_bytes(external_provenance_unsigned)
        ),
    }
    candidate = {
        "schema_version": 1,
        "kind": "scriber_cloud_llama_cpp_binding",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "platform": "linux_x86_64_cuda",
        "lock_sha256": result["lock_sha256"],
        "commit": result["commit"],
        "source_tree_sha256": result["source_tree_sha256"],
        "build": reviewed_toolchain_build_binding(),
        "build_identity_sha256": result["build_identity_sha256"],
        "cuda_enabled": True,
        "distribution": result["distribution"],
        "native_runtime_tree_sha256": result["native_runtime_tree_sha256"],
        "ldd_closure_sha256": result["ldd_closure_sha256"],
        "required_host_driver_sonames": result[
            "required_host_driver_sonames"
        ],
        "runtime_library_directories": result["runtime_library_directories"],
        "tool_sha256": result["tool_sha256"],
        "materialization": {
            "schema_version": 1,
            "kind": "scriber_llama_cpp_toolchain_materialization_binding",
            "attempt": TOOLCHAIN_ATTEMPT,
            "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
            "status": "verified",
            "packet_tree_sha256": packet_tree,
            "bundle_payload_tree_sha256": bundle_tree,
            "result_sha256": result_sha256,
            "flavor": result["flavor"],
            "timeout_minutes": TOOLCHAIN_TIMEOUT_MINUTES,
            "training_steps_executed": 0,
            "evaluation_reports_written": 0,
            "prediction_records_written": 0,
            "external_provenance": external_provenance,
        },
    }
    checked = _validate_llama_cpp_binding(
        candidate,
        remote_uri=remote_uri,
    )
    checked.pop("remote_uri")
    checked.pop("mount_mode")
    return checked


def resolve_quantization_llama_cpp_library_path(
    *,
    manifest_path: str | Path,
    bundle_root: str | Path,
    bundle_lock: str | Path,
) -> str:
    """Return only the plan-bound native library path for the mounted bundle."""

    plan, _ = read_json_object(
        manifest_path,
        "HF quantization plan",
    )
    plan = validate_hf_quantization_plan(plan)
    actual = build_cloud_llama_cpp_binding(bundle_root, bundle_lock)
    expected = dict(_mapping(plan["llama_cpp"], "llama.cpp plan"))
    expected.pop("remote_uri")
    expected.pop("mount_mode")
    if actual != expected:
        raise HfQuantizationError("mounted llama.cpp bundle differs from the exact plan binding")
    root = Path(bundle_root).expanduser().resolve()
    paths: list[str] = []
    for relative in actual["runtime_library_directories"]:
        path = (root / str(relative)).resolve()
        try:
            common = Path(os.path.commonpath([root, path]))
        except ValueError as error:
            raise HfQuantizationError("llama.cpp native library directory escapes the bundle") from error
        if common != root or not path.is_dir() or ":" in str(path) or "\n" in str(path):
            raise HfQuantizationError("llama.cpp native library directory is invalid")
        paths.append(path.as_posix())
    return ":".join(paths)


def verify_quantization_source_model(
    *,
    manifest_path: str | Path,
    model_root: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Verify mounted model bytes and the lexical policy before quantization."""

    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    source = _mapping(plan["source_model"], "quantization source model")
    root = Path(model_root).expanduser().resolve()
    try:
        from .inference import _inventory_local_model_tree

        inventory = _inventory_local_model_tree(root)
        policy = verify_frozen_lexical_policy_artifact(root)
    except (OSError, ValueError, PolicyArtifactError) as error:
        raise HfQuantizationError(f"quantization source-model preflight failed: {error}") from error
    if (
        inventory.get("tree_sha256") != source.get("final_model_tree_sha256")
        or policy != frozen_lexical_policy_binding()
    ):
        raise HfQuantizationError("mounted source model differs from its exact tree or policy binding")
    weight = root / str(source.get("model_weight_filename", ""))
    if (
        weight.is_symlink()
        or not weight.is_file()
        or weight.stat().st_size != source.get("model_weight_bytes")
        or _sha256_path(weight, "mounted source model weight")
        != source.get("model_weight_sha256")
    ):
        raise HfQuantizationError(
            "mounted source model weight differs from its exact hash or byte binding"
        )
    reload_report, _ = read_json_object(
        root / "reload-verification.json",
        "source reload verification",
    )
    if reload_report.get("status") != "passed" or reload_report.get("run_fingerprint") != source.get(
        "training_fingerprint"
    ):
        raise HfQuantizationError("mounted source model differs from the training fingerprint")
    report = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_source_preflight",
        "status": "passed",
        "model_tree_sha256": inventory["tree_sha256"],
        "model_weight_sha256": source["model_weight_sha256"],
        "model_weight_bytes": source["model_weight_bytes"],
        "training_fingerprint": source["training_fingerprint"],
        "protection_policy": policy,
    }
    _write_immutable_json(output_path, report)
    return report


def verify_quantization_llama_cpp(
    *,
    manifest_path: str | Path,
    bundle_root: str | Path,
    bundle_lock: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Verify mounted llama.cpp bytes against the dry-bound Linux CUDA lock."""

    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    actual = build_cloud_llama_cpp_binding(bundle_root, bundle_lock)
    expected = dict(_mapping(plan["llama_cpp"], "llama.cpp plan"))
    expected.pop("remote_uri")
    expected.pop("mount_mode")
    if actual != expected:
        raise HfQuantizationError("mounted llama.cpp bundle differs from the exact plan binding")
    try:
        verified_bundle = load_verified_llama_cpp_bundle(
            bundle_root,
            bundle_lock,
        )
        closure = verify_llama_cpp_runtime_closure(verified_bundle)
    except LlamaCppBundleError as error:
        raise HfQuantizationError(f"llama.cpp runtime closure preflight failed: {error}") from error
    if closure.get("ldd_closure_sha256") != actual["ldd_closure_sha256"] or closure.get("library_directories") != [
        str(path) for path in verified_bundle.runtime_library_paths
    ]:
        raise HfQuantizationError("llama.cpp runtime closure differs from the locked plan")
    report = {
        **actual,
        "kind": "scriber_hf_quantization_llama_cpp_preflight",
        "runtime_closure": closure,
    }
    _write_immutable_json(output_path, report)
    return report


def list_hf_quantization_suites(
    manifest_path: str | Path,
) -> list[tuple[str, str]]:
    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    references = _mapping(plan["references"], "quantization references")
    return [
        (
            suite_id,
            str(_mapping(references[suite_id], suite_id)["records_filename"]),
        )
        for suite_id in REFERENCE_SUITES
    ]


def record_hf_quantization_evaluation_exit(
    *,
    variant: str,
    suite_id: str,
    evaluation_exit: int,
    output_path: str | Path,
) -> dict[str, object]:
    if variant not in VARIANTS or suite_id not in REFERENCE_SUITES:
        raise HfQuantizationError("unknown quantization variant or suite")
    if evaluation_exit not in {0, 2}:
        raise HfQuantizationError("evaluation exit must be 0 or 2")
    value = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_evaluation_exit",
        "variant": variant,
        "suite": suite_id,
        "evaluation_exit": evaluation_exit,
    }
    _write_immutable_json(output_path, value)
    return value


def verify_quantization_variant_artifact(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    variant: str,
) -> dict[str, object]:
    """Verify one durable variant before a resumed job skips materialization."""

    if variant not in VARIANTS:
        raise HfQuantizationError("unknown quantization variant")
    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    root = Path(output_root).expanduser().resolve()
    artifact_root = (
        root / "variants" / "gguf" / variant
        if variant in {"Q8_0", "Q4_K_M"}
        else root / "variants" / variant
    )
    try:
        artifact = verify_variant_artifact_manifest(artifact_root)
    except (OSError, VariantArtifactError) as error:
        raise HfQuantizationError(
            f"{variant} durable artifact verification failed: {error}"
        ) from error
    source = _mapping(plan["source_model"], "quantization source model")
    reload_evidence = _mapping(artifact.get("reload"), f"{variant} reload")
    if (
        artifact.get("variant") != variant
        or _mapping(artifact.get("source"), f"{variant} source").get(
            "tree_sha256"
        )
        != source["final_model_tree_sha256"]
        or artifact.get("training_fingerprint") != source["training_fingerprint"]
        or artifact.get("protection_policy") != frozen_lexical_policy_binding()
        or reload_evidence.get("deterministic") is not True
        or reload_evidence.get("valid_sst") is not True
        or reload_evidence.get("process_boundary") != "fresh_subprocess"
    ):
        raise HfQuantizationError(
            f"{variant} durable artifact differs from the bound source model"
        )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_variant_resume_verification",
        "status": "verified",
        "variant": variant,
        "artifact_tree_sha256": _mapping(
            artifact.get("artifact"),
            f"{variant} artifact inventory",
        )["tree_sha256"],
        "source_model_tree_sha256": source["final_model_tree_sha256"],
        "training_fingerprint": source["training_fingerprint"],
    }


def verify_immutable_bf16_baseline(
    *,
    manifest_path: str | Path,
    baseline_root: str | Path,
) -> dict[str, object]:
    """Verify only the three hash-bound reports from the completed BF16 run."""

    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    if plan.get("schema_version") != 2:
        raise HfQuantizationError(
            "immutable BF16 baseline is available only for the full-model plan"
        )
    source = _mapping(plan["source_model"], "quantization source model")
    final_evaluation = _mapping(
        plan["verified_final_evaluation"], "verified final evaluation"
    )
    baseline = _validate_immutable_bf16_baseline_binding(
        _mapping(plan.get("immutable_bf16_baseline"), "immutable BF16 baseline"),
        source_model_tree_sha256=source["final_model_tree_sha256"],
        final_evaluation_result_sha256=final_evaluation["result_sha256"],
        final_evaluation_result_bytes=final_evaluation["result_bytes"],
        final_evaluation_manifest_sha256=final_evaluation["manifest_sha256"],
        final_evaluation_manifest_bytes=final_evaluation["manifest_bytes"],
        model_weight_sha256=source["model_weight_sha256"],
        model_weight_bytes=source["model_weight_bytes"],
    )
    root = Path(baseline_root).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise HfQuantizationError("immutable BF16 baseline mount is missing")
    verified: dict[str, dict[str, object]] = {}
    suites = _mapping(baseline["suites"], "immutable BF16 baseline suites")
    for suite_id in REFERENCE_SUITES:
        binding = _mapping(suites[suite_id], f"immutable BF16 baseline {suite_id}")
        path = root / str(binding["relative_path"])
        if path.is_symlink() or not path.is_file():
            raise HfQuantizationError(
                f"immutable BF16 baseline report is missing for {suite_id}"
            )
        payload = path.read_bytes()
        if (
            len(payload) != binding["bytes"]
            or _sha256(payload) != binding["sha256"]
        ):
            raise HfQuantizationError(
                f"immutable BF16 baseline report differs for {suite_id}"
            )
        value, stable_payload = read_json_object(
            path,
            f"immutable BF16 baseline {suite_id}",
        )
        if stable_payload != payload or not value:
            raise HfQuantizationError(
                f"immutable BF16 baseline report is invalid for {suite_id}"
            )
        verified[suite_id] = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    return {
        "schema_version": 1,
        "kind": "scriber_immutable_bf16_baseline_verification",
        "status": "verified",
        "source_model_tree_sha256": baseline["source_model_tree_sha256"],
        "model_weight_sha256": baseline["model_weight_sha256"],
        "model_weight_bytes": baseline["model_weight_bytes"],
        "final_evaluation_result_sha256": baseline[
            "final_evaluation_result_sha256"
        ],
        "final_evaluation_result_bytes": baseline[
            "final_evaluation_result_bytes"
        ],
        "final_evaluation_manifest_sha256": baseline[
            "final_evaluation_manifest_sha256"
        ],
        "final_evaluation_manifest_bytes": baseline[
            "final_evaluation_manifest_bytes"
        ],
        "remote_uri": baseline["remote_uri"],
        "suites": verified,
        "materialization_runs": 0,
        "inference_runs": 0,
        "evaluation_runs": 0,
    }


def _immutable_bf16_evaluation(
    *,
    plan: Mapping[str, object],
    baseline_root: Path,
    suite_id: str,
) -> tuple[dict[str, Any], bytes]:
    baseline = _mapping(plan["immutable_bf16_baseline"], "immutable BF16 baseline")
    suite = _mapping(
        _mapping(baseline["suites"], "immutable BF16 baseline suites")[suite_id],
        f"immutable BF16 baseline {suite_id}",
    )
    path = baseline_root / str(suite["relative_path"])
    evaluation, payload = read_json_object(path, f"immutable BF16 baseline {suite_id}")
    if len(payload) != suite["bytes"] or _sha256(payload) != suite["sha256"]:
        raise HfQuantizationError(
            f"immutable BF16 baseline report differs for {suite_id}"
        )
    return evaluation, payload


def _verify_evaluation_lane(
    *,
    plan: Mapping[str, object],
    variant: str,
    suite_id: str,
    lane_root: Path,
    baseline_root: Path | None = None,
) -> dict[str, object]:
    suite = _mapping(
        _mapping(plan["references"], "quantization references")[suite_id],
        suite_id,
    )
    predictions_path = lane_root / "predictions.jsonl"
    prediction_manifest, _ = read_json_object(
        lane_root / "predictions.manifest.json",
        f"{variant}/{suite_id} prediction manifest",
    )
    evaluation, evaluation_payload = read_json_object(
        lane_root / "evaluation.json",
        f"{variant}/{suite_id} evaluation",
    )
    exit_record, _ = read_json_object(
        lane_root / "evaluation-exit.json",
        f"{variant}/{suite_id} evaluation exit",
    )
    if predictions_path.is_symlink() or not predictions_path.is_file():
        raise HfQuantizationError(f"{variant}/{suite_id} predictions are missing")
    prediction_payload = predictions_path.read_bytes()
    prediction_hash = hashlib.sha256(prediction_payload).hexdigest()
    expected_quantization = "none" if variant == "bf16" else variant
    is_gguf = variant in {"Q8_0", "Q4_K_M"}
    if (
        prediction_manifest.get("candidate") != variant
        or prediction_manifest.get("record_count") != suite["record_count"]
        or prediction_manifest.get("prediction_sha256") != prediction_hash
        or prediction_manifest.get("reference_sha256") != suite["records_sha256"]
        or prediction_manifest.get("quantization") != expected_quantization
        or prediction_manifest.get("device") != "cuda"
        or (is_gguf and prediction_manifest.get("backend") != "llama_cpp_server")
    ):
        raise HfQuantizationError(f"{variant}/{suite_id} prediction evidence is inconsistent")
    runtime_evidence: dict[str, object] | None = None
    if is_gguf:
        model_path = lane_root.parents[2] / "variants" / "gguf" / variant / f"model-{variant}.gguf"
        llama_tools = _mapping(
            _mapping(plan["llama_cpp"], "llama.cpp plan")["tool_sha256"],
            "llama.cpp tools",
        )
        runtime_evidence = _validate_llama_cpp_cuda_lane_evidence(
            _mapping(
                prediction_manifest.get("runtime_evidence"),
                f"{variant}/{suite_id} llama.cpp runtime evidence",
            ),
            variant=variant,
            suite_id=suite_id,
            model_sha256=_sha256_path(
                model_path,
                f"{variant} GGUF model",
            ),
            llama_server_sha256=str(llama_tools["llama_server"]),
            reference_sha256=("sha256:" + str(suite["records_sha256"])),
            prediction_sha256="sha256:" + prediction_hash,
        )
    policy = _mapping(
        prediction_manifest.get("protection_policy"),
        f"{variant}/{suite_id} prediction policy",
    )
    frozen = frozen_lexical_policy_binding()
    if (
        policy.get("policy_id") != frozen["policy_id"]
        or policy.get("policy_sha256") != frozen["policy_sha256"]
        or "sha256:" + str(policy.get("artifact_sha256")) != frozen["artifact_sha256"]
    ):
        raise HfQuantizationError(f"{variant}/{suite_id} did not use the frozen lexical policy")
    gate = _mapping(
        evaluation.get("gate"),
        f"{variant}/{suite_id} evaluation gate",
    )
    gate_passed = gate.get("passed")
    absolute_gate_required = plan.get("schema_version") == 1
    expected_evaluation_exit = 0 if gate_passed is True else 2
    if (
        evaluation.get("candidate") != variant
        or evaluation.get("reference_sha256") != suite["records_sha256"]
        or evaluation.get("prediction_sha256") != prediction_hash
        or evaluation.get("evaluation_config_sha256")
        != _mapping(plan["evaluation"], "evaluation config")["config_sha256"]
        or evaluation.get("exact_case_coverage") is not True
        or not isinstance(gate_passed, bool)
        or (absolute_gate_required and gate_passed is not True)
        or exit_record
        != {
            "schema_version": 1,
            "kind": "scriber_hf_quantization_evaluation_exit",
            "variant": variant,
            "suite": suite_id,
            "evaluation_exit": expected_evaluation_exit,
        }
    ):
        raise HfQuantizationError(
            f"{variant}/{suite_id} fresh evaluation evidence is inconsistent"
        )
    if plan.get("schema_version") == 2:
        if baseline_root is None:
            raise HfQuantizationError("immutable BF16 baseline mount is required")
        baseline_evaluation, baseline_payload = _immutable_bf16_evaluation(
            plan=plan,
            baseline_root=baseline_root,
            suite_id=suite_id,
        )
    else:
        baseline_evaluation, baseline_payload = read_json_object(
            lane_root.parents[1] / "bf16" / suite_id / "evaluation.json",
            f"bf16/{suite_id} baseline evaluation",
        )
    delta_report, delta_payload = read_json_object(
        lane_root / "quality-delta.json",
        f"{variant}/{suite_id} quality delta",
    )
    baseline_hash = _sha256(baseline_payload)
    candidate_hash = _sha256(evaluation_payload)
    try:
        verified_delta = verify_quality_delta_report(
            delta_report,
            baseline_evaluation,
            evaluation,
            baseline_sha256=baseline_hash,
            candidate_sha256=candidate_hash,
            candidate_variant=variant,
        )
    except QualityDeltaError as error:
        raise HfQuantizationError(f"{variant}/{suite_id} quality delta verification failed: {error}") from error
    budget = _mapping(
        _mapping(plan["evaluation"], "quantization evaluation")["quality_delta_budgets"],
        "quality delta budgets",
    )[variant]
    budget = _mapping(budget, f"{variant} quality delta budget")
    if float(verified_delta["maximum_absolute_metric_drop"]) > float(budget["maximum_absolute_metric_drop"]) or int(
        verified_delta["new_critical_errors"]
    ) > int(budget["maximum_new_critical_errors"]):
        raise HfQuantizationError(f"{variant}/{suite_id} exceeds its BF16-relative quality delta budget")
    lane_result: dict[str, object] = {
        "record_count": suite["record_count"],
        "exact_case_coverage": True,
        "gate_passed": gate_passed,
        "prediction_sha256": "sha256:" + prediction_hash,
        "evaluation_sha256": candidate_hash,
        "quality_delta": {
            "baseline_evaluation_sha256": baseline_hash,
            "candidate_evaluation_sha256": candidate_hash,
            "report_sha256": _sha256(delta_payload),
            "maximum_absolute_metric_drop": verified_delta["maximum_absolute_metric_drop"],
            "new_critical_errors": verified_delta["new_critical_errors"],
            "budget": dict(budget),
            "within_budget": True,
        },
    }
    if runtime_evidence is not None:
        lane_result["runtime_evidence"] = runtime_evidence
    return lane_result


def verify_quantization_completed_lane(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    variant: str,
    suite_id: str,
    baseline_root: str | Path | None = None,
) -> dict[str, object]:
    """Verify one complete durable lane before a resumed job skips inference."""

    if variant not in VARIANTS or suite_id not in REFERENCE_SUITES:
        raise HfQuantizationError("unknown quantization variant or suite")
    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    if plan.get("schema_version") == 2 and variant == "bf16":
        raise HfQuantizationError(
            "BF16 inference and evaluation are prohibited for the full-model plan"
        )
    root = Path(output_root).expanduser().resolve()
    lane = _verify_evaluation_lane(
        plan=plan,
        variant=variant,
        suite_id=suite_id,
        lane_root=root / "evaluation" / variant / suite_id,
        baseline_root=(
            Path(baseline_root).expanduser().resolve()
            if baseline_root is not None
            else None
        ),
    )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_lane_resume_verification",
        "status": "verified",
        "variant": variant,
        "suite": suite_id,
        "lane": lane,
    }


def finalize_hf_quantization(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    output_path: str | Path,
    baseline_root: str | Path | None = None,
) -> dict[str, object]:
    """Verify fresh quantized lanes and the immutable BF16 evidence binding."""

    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    plan = validate_hf_quantization_plan(plan)
    root = Path(output_root).expanduser().resolve()
    source = _mapping(plan["source_model"], "quantization source model")
    full_model = plan.get("schema_version") == 2
    resolved_baseline_root = (
        Path(baseline_root).expanduser().resolve()
        if baseline_root is not None
        else None
    )
    baseline_verification: dict[str, object] | None = None
    if full_model:
        if resolved_baseline_root is None:
            raise HfQuantizationError("immutable BF16 baseline mount is required")
        baseline_verification = verify_immutable_bf16_baseline(
            manifest_path=manifest_path,
            baseline_root=resolved_baseline_root,
        )
    variants: dict[str, dict[str, object]] = {}
    for variant in QUANTIZED_VARIANTS if full_model else VARIANTS:
        artifact_root = (
            root / "variants" / "gguf" / variant if variant in {"Q8_0", "Q4_K_M"} else root / "variants" / variant
        )
        try:
            artifact = verify_variant_artifact_manifest(artifact_root)
        except (OSError, VariantArtifactError) as error:
            raise HfQuantizationError(f"{variant} artifact verification failed: {error}") from error
        if (
            artifact.get("variant") != variant
            or _mapping(artifact.get("source"), f"{variant} source").get("tree_sha256")
            != source["final_model_tree_sha256"]
            or artifact.get("training_fingerprint") != source["training_fingerprint"]
            or artifact.get("protection_policy") != frozen_lexical_policy_binding()
        ):
            raise HfQuantizationError(f"{variant} artifact differs from the bound source model")
        reload_evidence = _mapping(
            artifact.get("reload"),
            f"{variant} reload",
        )
        if (
            reload_evidence.get("deterministic") is not True
            or reload_evidence.get("valid_sst") is not True
            or reload_evidence.get("process_boundary") != "fresh_subprocess"
        ):
            raise HfQuantizationError(f"{variant} has no successful fresh reload")
        variants[variant] = {
            "artifact_tree_sha256": _mapping(
                artifact.get("artifact"),
                f"{variant} artifact inventory",
            )["tree_sha256"],
            "source_tree_sha256": source["final_model_tree_sha256"],
            "training_fingerprint": source["training_fingerprint"],
            "protection_policy": frozen_lexical_policy_binding(),
            "fresh_reload_passed": True,
            "evaluation": {
                suite_id: _verify_evaluation_lane(
                    plan=plan,
                    variant=variant,
                    suite_id=suite_id,
                    lane_root=root / "evaluation" / variant / suite_id,
                    baseline_root=resolved_baseline_root,
                )
                for suite_id in REFERENCE_SUITES
            },
        }
    result_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "scriber_hf_quantization_result",
            "status": "complete",
            "source_model_tree_sha256": source["final_model_tree_sha256"],
            "training_fingerprint": source["training_fingerprint"],
            "protection_policy": frozen_lexical_policy_binding(),
            "variants": variants,
            "budget": plan["budget"],
            "publication": plan["publication"],
        }
    if baseline_verification is not None:
        result_payload["immutable_bf16_baseline"] = baseline_verification
    result = validate_hf_quantization_result(
        result_payload,
        plan=plan,
    )
    _write_immutable_json(output_path, result)
    return result


def verify_hf_quantization_result_file(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    result_path: str | Path,
    baseline_root: str | Path | None = None,
) -> dict[str, object]:
    plan, _ = read_json_object(manifest_path, "HF quantization plan")
    result, result_payload = read_json_object(
        result_path,
        "HF quantization result",
    )
    validated = validate_hf_quantization_result(result, plan=plan)
    if result_payload != _canonical_bytes(validated):
        raise HfQuantizationError("HF quantization result is not canonical immutable JSON")
    root = Path(output_root).expanduser().resolve()
    if Path(result_path).expanduser().resolve().parent != root:
        raise HfQuantizationError("HF quantization result is outside the bound output root")
    recomputed = finalize_hf_quantization(
        manifest_path=manifest_path,
        output_root=root,
        output_path=result_path,
        baseline_root=baseline_root,
    )
    if recomputed != validated:
        raise HfQuantizationError("HF quantization result differs from recomputed output evidence")
    return validated


def _rename_quantization_directory_no_replace(
    source: Path,
    destination: Path,
) -> None:
    """Atomically commit a staged result without replacing any winner."""

    if source.parent.name != ".staging" or source.parent.parent != destination.parent:
        raise HfQuantizationError(
            "quantization stage and final output must share the mounted parent"
        )
    if os.name != "posix":
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise HfQuantizationError(
                "completed quantization output already exists"
            ) from error
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise HfQuantizationError(
            "renameat2(RENAME_NOREPLACE) is unavailable; refusing a non-atomic commit"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise HfQuantizationError(
            "completed quantization output already exists"
        )
    raise HfQuantizationError(
        "atomic quantization output commit failed: "
        f"{os.strerror(error_number)} ({error_number})"
    )


def publish_hf_quantization_result_no_clobber(
    *,
    manifest_path: str | Path,
    staged_root: str | Path,
    result_path: str | Path,
    destination_root: str | Path,
    baseline_root: str | Path,
    expected_packet_tree_sha256: str,
    owner_token: str,
) -> dict[str, object]:
    """Verify and atomically publish one owner-bound result exactly once."""

    packet_tree = _prefixed_hash(
        expected_packet_tree_sha256,
        "quantization publication packet tree",
    )
    if _OWNER_TOKEN.fullmatch(owner_token) is None:
        raise HfQuantizationError(
            "quantization publication owner token must be 64 lowercase hex"
        )
    staged = Path(staged_root).expanduser().resolve()
    destination = Path(destination_root).expanduser().resolve()
    expected_stage_name = f"{owner_token}-{packet_tree.removeprefix('sha256:')}"
    if (
        staged.is_symlink()
        or not staged.is_dir()
        or staged.name != expected_stage_name
        or staged.parent.name != ".staging"
        or staged.parent.parent != destination.parent
    ):
        raise HfQuantizationError(
            "quantization publication stage is not owner- and packet-bound"
        )
    verified_stage = verify_hf_quantization_result_file(
        manifest_path=manifest_path,
        output_root=staged,
        result_path=result_path,
        baseline_root=baseline_root,
    )
    if destination.exists() or destination.is_symlink():
        verify_hf_quantization_result_file(
            manifest_path=destination / "hf-quantization-plan.json",
            output_root=destination,
            result_path=destination / "hf-quantization-result.json",
            baseline_root=baseline_root,
        )
        raise HfQuantizationError(
            "completed quantization output already exists and was preserved"
        )
    try:
        _rename_quantization_directory_no_replace(staged, destination)
    except HfQuantizationError as error:
        if destination.exists() and not destination.is_symlink():
            winner = verify_hf_quantization_result_file(
                manifest_path=destination / "hf-quantization-plan.json",
                output_root=destination,
                result_path=destination / "hf-quantization-result.json",
                baseline_root=baseline_root,
            )
            if winner == verified_stage:
                raise HfQuantizationError(
                    "quantization publication race preserved the verified winner"
                ) from error
        raise
    committed = verify_hf_quantization_result_file(
        manifest_path=destination / "hf-quantization-plan.json",
        output_root=destination,
        result_path=destination / "hf-quantization-result.json",
        baseline_root=baseline_root,
    )
    if committed != verified_stage:
        raise HfQuantizationError(
            "committed quantization result differs from the staged result"
        )
    return committed


def _packet_inventory(
    root: Path,
) -> tuple[list[dict[str, object]], str]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.name == "job-manifest.json":
            continue
        if path.is_symlink():
            raise HfQuantizationError(f"packet contains a non-regular entry: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise HfQuantizationError(f"packet contains a non-regular entry: {path}")
        payload = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "kind": "file",
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        )
    if not records:
        raise HfQuantizationError("HF quantization packet is empty")
    digest = hashlib.sha256()
    for record in records:
        digest.update(_canonical_bytes(record))
    return records, "sha256:" + digest.hexdigest()


def _normalized_distribution_name(value: object) -> str:
    name = re.sub(r"[-_.]+", "-", str(value or "").strip().lower())
    if _DISTRIBUTION.fullmatch(name) is None:
        raise HfQuantizationError("offline wheel distribution name is invalid")
    return name


def _verify_wheel_archive(
    path: Path,
    *,
    distribution: str,
    version: str,
) -> dict[str, object]:
    """Verify that locked bytes are one usable CPython 3.12 Linux wheel."""

    filename_parts = path.name.removesuffix(".whl").split("-")
    if (
        len(filename_parts) != 5
        or _normalized_distribution_name(filename_parts[0]) != distribution
        or filename_parts[1] != version
    ):
        raise HfQuantizationError(
            f"quantization wheel filename identity is invalid: {path.name}"
        )
    python_tags = filename_parts[2].split(".")
    abi_tags = filename_parts[3].split(".")
    platform_tags = filename_parts[4].split(".")
    if (
        not set(python_tags).intersection({"py3", "py312", "cp312"})
        or not set(abi_tags).intersection({"none", "abi3", "cp312"})
        or not any(
            tag == "any"
            or (
                ("linux" in tag or "manylinux" in tag)
                and (tag.endswith("x86_64") or tag.endswith("amd64"))
            )
            for tag in platform_tags
        )
        or not zipfile.is_zipfile(path)
    ):
        raise HfQuantizationError(
            f"quantization wheel is not usable on CPython 3.12 Linux x86_64: {path.name}"
        )
    filename_tags = {
        f"{python_tag}-{abi_tag}-{platform_tag}"
        for python_tag in python_tags
        for abi_tag in abi_tags
        for platform_tag in platform_tags
    }
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if not infos or len(names) != len(set(names)):
                raise HfQuantizationError(
                    f"quantization wheel archive entries are empty or duplicated: {path.name}"
                )
            for info in infos:
                pure = PurePosixPath(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    not info.filename
                    or "\\" in info.filename
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or stat.S_IFMT(mode) == stat.S_IFLNK
                    or bool(info.flag_bits & 0x1)
                ):
                    raise HfQuantizationError(
                        f"quantization wheel contains an unsafe archive entry: {path.name}"
                    )
            metadata_names = [
                name
                for name in names
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise HfQuantizationError(
                    f"quantization wheel has no unique METADATA: {path.name}"
                )
            dist_info = metadata_names[0].removesuffix("/METADATA")
            dist_identity = dist_info.removesuffix(".dist-info").rsplit("-", 1)
            if (
                len(dist_identity) != 2
                or _normalized_distribution_name(dist_identity[0]) != distribution
                or dist_identity[1] != version
            ):
                raise HfQuantizationError(
                    f"quantization wheel dist-info identity differs: {path.name}"
                )
            wheel_name = f"{dist_info}/WHEEL"
            record_name = f"{dist_info}/RECORD"
            if wheel_name not in names or record_name not in names:
                raise HfQuantizationError(
                    f"quantization wheel metadata set is incomplete: {path.name}"
                )
            metadata_bytes = archive.read(metadata_names[0])
            wheel_bytes = archive.read(wheel_name)
            record_bytes = archive.read(record_name)
            if max(len(metadata_bytes), len(wheel_bytes), len(record_bytes)) > 1024 * 1024:
                raise HfQuantizationError(
                    f"quantization wheel metadata is unreasonably large: {path.name}"
                )
            metadata = BytesParser(policy=policy.compat32).parsebytes(metadata_bytes)
            wheel_metadata = BytesParser(policy=policy.compat32).parsebytes(wheel_bytes)
            wheel_tags = set(wheel_metadata.get_all("Tag", []))
            if (
                _normalized_distribution_name(metadata.get("Name")) != distribution
                or metadata.get("Version") != version
                or wheel_metadata.get("Wheel-Version") not in {"1.0", "1.1"}
                or not wheel_tags.intersection(filename_tags)
            ):
                raise HfQuantizationError(
                    f"quantization wheel internal identity or tags differ: {path.name}"
                )
            try:
                record_rows = list(csv.reader(io.StringIO(record_bytes.decode("utf-8"))))
            except (UnicodeDecodeError, csv.Error) as error:
                raise HfQuantizationError(
                    f"quantization wheel RECORD is invalid: {path.name}"
                ) from error
            records: dict[str, tuple[str, str]] = {}
            for row in record_rows:
                if len(row) != 3 or row[0] in records:
                    raise HfQuantizationError(
                        f"quantization wheel RECORD is malformed or duplicated: {path.name}"
                    )
                records[row[0]] = (row[1], row[2])
            archive_files = {
                info.filename: info for info in infos if not info.is_dir()
            }
            if set(records) != set(archive_files):
                raise HfQuantizationError(
                    f"quantization wheel RECORD does not cover the archive: {path.name}"
                )
            for archived_name, info in archive_files.items():
                digest, recorded_size = records[archived_name]
                if archived_name == record_name:
                    if digest or recorded_size:
                        raise HfQuantizationError(
                            f"quantization wheel RECORD self-entry is not empty: {path.name}"
                        )
                    continue
                if not digest.startswith("sha256=") or recorded_size != str(info.file_size):
                    raise HfQuantizationError(
                        f"quantization wheel RECORD entry is incomplete: {path.name}"
                    )
                try:
                    decoded = base64.urlsafe_b64decode(
                        digest.removeprefix("sha256=") + "=="
                    )
                except (ValueError, TypeError) as error:
                    raise HfQuantizationError(
                        f"quantization wheel RECORD hash is invalid: {path.name}"
                    ) from error
                if len(decoded) != hashlib.sha256().digest_size:
                    raise HfQuantizationError(
                        f"quantization wheel RECORD hash is invalid: {path.name}"
                    )
                with archive.open(info, "r") as archived_file:
                    actual_digest = hashlib.file_digest(
                        archived_file,
                        "sha256",
                    ).digest()
                if decoded != actual_digest:
                    raise HfQuantizationError(
                        f"quantization wheel RECORD hash differs: {path.name}"
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise HfQuantizationError(
            f"quantization wheel archive cannot be verified: {path.name}"
        ) from error
    return {
        "distribution": distribution,
        "version": version,
        "filename_tags": sorted(filename_tags),
        "wheel_tags": sorted(wheel_tags),
        "metadata_verified": True,
        "record_verified": True,
    }


def _verify_quantization_wheelhouse(
    wheelhouse_dir: str | Path,
    lock_path: str | Path,
) -> tuple[dict[str, object], bytes]:
    wheelhouse = Path(wheelhouse_dir).expanduser().resolve()
    lock, lock_payload = read_json_object(lock_path, "quantization wheelhouse lock")
    if lock_payload != _canonical_bytes(lock):
        raise HfQuantizationError("quantization wheelhouse lock is not canonical JSON")
    wheels = lock.get("wheels")
    if (
        set(lock) != {"schema_version", "kind", "wheels"}
        or lock.get("schema_version") != 1
        or lock.get("kind") != "scriber_hf_quantization_offline_wheelhouse"
        or not isinstance(wheels, list)
        or not wheels
        or wheelhouse.is_symlink()
        or not wheelhouse.is_dir()
    ):
        raise HfQuantizationError("quantization wheelhouse lock identity is invalid")
    actual_entries = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
    records: list[dict[str, object]] = []
    seen_names: set[str] = set()
    seen_filenames: set[str] = set()
    for raw in wheels:
        row = dict(_mapping(raw, "quantization wheel lock record"))
        if set(row) != {"name", "version", "filename", "bytes", "sha256"}:
            raise HfQuantizationError("quantization wheel lock record is not closed")
        name = _normalized_distribution_name(row.get("name"))
        version = str(row.get("version", ""))
        filename = str(row.get("filename", ""))
        size = row.get("bytes")
        digest = _prefixed_hash(row.get("sha256"), "quantization wheel hash")
        if (
            _VERSION.fullmatch(version) is None
            or _FILENAME.fullmatch(filename) is None
            or not filename.endswith(".whl")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or name in seen_names
            or filename in seen_filenames
        ):
            raise HfQuantizationError("quantization wheel lock record is invalid or duplicated")
        path = wheelhouse / filename
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != size
            or _sha256(path.read_bytes()) != digest
        ):
            raise HfQuantizationError(f"quantization wheel differs from lock: {filename}")
        _verify_wheel_archive(
            path,
            distribution=name,
            version=version,
        )
        seen_names.add(name)
        seen_filenames.add(filename)
        records.append(
            {
                "name": name,
                "version": version,
                "filename": filename,
                "bytes": size,
                "sha256": digest,
            }
        )
    if [path.name for path in actual_entries] != sorted(seen_filenames) or any(
        path.is_symlink() or not path.is_file() for path in actual_entries
    ):
        raise HfQuantizationError("quantization wheelhouse contains unbound entries")
    direct = {name: row["version"] for name in QUANTIZATION_DIRECT_DEPENDENCIES for row in records if row["name"] == name}
    if direct != QUANTIZATION_DIRECT_DEPENDENCIES:
        raise HfQuantizationError("quantization wheelhouse lacks exact direct dependency versions")
    records.sort(key=lambda row: (str(row["name"]), str(row["filename"])))
    if wheels != records:
        raise HfQuantizationError("quantization wheelhouse lock is not canonically ordered")
    requirements = "".join(
        f"{row['name']}=={row['version']} --hash={row['sha256']}\n"
        for row in records
    ).encode("utf-8")
    binding = {
        "mode": "offline_no_index_require_hashes",
        "archive_validation": "zip-wheel-metadata-record-target-tags-v1",
        "wheelhouse_lock_sha256": _sha256(lock_payload),
        "requirements_sha256": _sha256(requirements),
        "wheels": records,
    }
    return binding, requirements


def _verify_packet_quantization_dependencies(
    root: Path,
    expected: object,
) -> dict[str, object]:
    binding = dict(_mapping(expected, "quantization offline dependencies"))
    lock_path = root / "input/quantization-wheels.lock.json"
    requirements_path = root / "input/quantization-requirements.txt"
    verified, requirements = _verify_quantization_wheelhouse(
        root / "input/wheels",
        lock_path,
    )
    if (
        binding != verified
        or requirements_path.is_symlink()
        or not requirements_path.is_file()
        or requirements_path.read_bytes() != requirements
    ):
        raise HfQuantizationError("packet offline dependency binding differs")
    return binding


def verify_hf_quantization_packet(
    packet_root: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str | None = None,
    require_fresh_actual_cost: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    root = Path(packet_root).expanduser().resolve()
    manifest, manifest_payload = read_json_object(
        root / "job-manifest.json",
        "HF quantization job manifest",
    )
    if manifest_payload != _canonical_bytes(manifest):
        raise HfQuantizationError("HF quantization job manifest is not canonical JSON")
    if (
        expected_manifest_sha256 is not None
        and _sha256(manifest_payload)
        != _prefixed_hash(expected_manifest_sha256, "HF quantization manifest")
    ):
        raise HfQuantizationError("HF quantization job manifest hash differs")
    packet_fields = {
        "runner_sha256",
        "packet_tree_sha256",
        "file_count",
        "byte_count",
        "dependencies",
    }
    plan = validate_hf_quantization_plan({key: value for key, value in manifest.items() if key not in packet_fields})
    records, tree = _packet_inventory(root)
    if (
        tree != expected_packet_tree_sha256
        or manifest.get("packet_tree_sha256") != tree
        or manifest.get("file_count") != len(records)
        or manifest.get("byte_count") != sum(int(record["bytes"]) for record in records)
    ):
        raise HfQuantizationError("HF quantization packet differs from its exact launch binding")
    runner = (root / "run-hf-quantization.sh").read_bytes()
    if manifest.get("runner_sha256") != _sha256(runner):
        raise HfQuantizationError("HF quantization runner hash differs")
    _verify_packet_quantization_dependencies(root, manifest.get("dependencies"))
    core, core_payload = read_json_object(
        root / "repo/ml/scriber_polishing/job-input/hf-quantization-plan.json",
        "packet HF quantization plan",
    )
    if core_payload != _canonical_bytes(plan) or core != plan:
        raise HfQuantizationError("packet and job HF quantization plans differ")
    cost_evidence, cost_evidence_payload = read_json_object(
        root / "repo/ml/scriber_polishing/job-input/hf-actual-cost-evidence.json",
        "packet HF actual-cost evidence",
    )
    if (
        cost_evidence_payload != _canonical_bytes(cost_evidence)
        or cost_evidence != plan.get("actual_cost_evidence")
        or _sha256(cost_evidence_payload)
        != _mapping(
            plan.get("budget"),
            "HF quantization budget",
        ).get("actual_cost_evidence_sha256")
    ):
        raise HfQuantizationError("packet actual-cost evidence differs from its plan binding")
    if require_fresh_actual_cost:
        verify_hf_actual_cost_freshness(
            plan,
            now_utc=now_utc,
        )
    return manifest


def _git_head() -> str:
    repository = Path(__file__).parents[4]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    head = completed.stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", REVIEWED_SCRIBER_GIT_HEAD, head],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if (
        completed.returncode != 0
        or _COMMIT.fullmatch(head) is None
        or completed.stderr.strip()
        or ancestry.returncode != 0
        or ancestry.stdout.strip()
        or ancestry.stderr.strip()
        or status.returncode != 0
        or bool(status.stdout)
        or bool(status.stderr)
    ):
        raise HfQuantizationError("cannot bind the source Git HEAD")
    return head


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise HfQuantizationError(f"packet source tree is invalid: {source}")
    shutil.copytree(
        source,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def build_hf_quantization_packet(
    *,
    final_evaluation_result_path: str | Path,
    final_evaluation_manifest_path: str | Path,
    training_report_path: str | Path,
    actual_cost_evidence_path: str | Path,
    remote_model_uri: str,
    model_weight_bytes: int,
    llama_cpp_bundle_root: str | Path | None,
    llama_cpp_bundle_lock: str | Path | None,
    remote_llama_cpp_uri: str,
    reference_root: str | Path,
    offline_wheelhouse_dir: str | Path,
    offline_wheelhouse_lock_path: str | Path,
    output_dir: str | Path,
    final_evaluation_config_path: str | Path | None = None,
    llama_cpp_materialization_result_path: str | Path | None = None,
    llama_cpp_materialization_result_sha256: str | None = None,
    llama_cpp_materialization_job_id: str | None = None,
    llama_cpp_launch_claim_commit: str | None = None,
    llama_cpp_launch_claim_owner: str | None = None,
) -> HfQuantizationPacket:
    """Create, but do not upload or execute, one immutable job packet."""

    result, result_payload = read_json_object(
        final_evaluation_result_path,
        "final-evaluation result",
    )
    final_manifest, final_manifest_payload = read_json_object(
        final_evaluation_manifest_path,
        "final-evaluation manifest",
    )
    training_report, training_report_payload = read_json_object(
        training_report_path,
        "continuation training report",
    )
    actual_cost_evidence, actual_cost_evidence_payload = read_json_object(
        actual_cost_evidence_path,
        "HF actual-cost evidence",
    )
    if llama_cpp_materialization_result_path is not None:
        if llama_cpp_bundle_root is not None or llama_cpp_bundle_lock is not None:
            raise HfQuantizationError(
                "choose the cloud materialization result or a local llama.cpp bundle, not both"
            )
        if not all(
            isinstance(value, str) and value
            for value in (
                llama_cpp_materialization_result_sha256,
                llama_cpp_materialization_job_id,
                llama_cpp_launch_claim_commit,
                llama_cpp_launch_claim_owner,
            )
        ):
            raise HfQuantizationError(
                "cloud materialization requires expected result, job, and claim provenance"
            )
        assert isinstance(llama_cpp_materialization_result_sha256, str)
        assert isinstance(llama_cpp_materialization_job_id, str)
        assert isinstance(llama_cpp_launch_claim_commit, str)
        assert isinstance(llama_cpp_launch_claim_owner, str)
        llama_binding = build_cloud_llama_cpp_binding_from_materialization_result(
            llama_cpp_materialization_result_path,
            remote_uri=remote_llama_cpp_uri,
            expected_result_sha256=llama_cpp_materialization_result_sha256,
            expected_job_id=llama_cpp_materialization_job_id,
            expected_claim_commit=llama_cpp_launch_claim_commit,
            expected_claim_owner=llama_cpp_launch_claim_owner,
        )
    else:
        if any(
            value is not None
            for value in (
                llama_cpp_materialization_result_sha256,
                llama_cpp_materialization_job_id,
                llama_cpp_launch_claim_commit,
                llama_cpp_launch_claim_owner,
            )
        ):
            raise HfQuantizationError(
                "external toolchain provenance is valid only with a cloud materialization result"
            )
        if llama_cpp_bundle_root is None or llama_cpp_bundle_lock is None:
            raise HfQuantizationError(
                "local llama.cpp bundle and lock are both required without a cloud materialization result"
            )
        llama_binding = build_cloud_llama_cpp_binding(
            llama_cpp_bundle_root,
            llama_cpp_bundle_lock,
        )
    references = Path(reference_root).expanduser().resolve()
    dependencies, requirements = _verify_quantization_wheelhouse(
        offline_wheelhouse_dir,
        offline_wheelhouse_lock_path,
    )
    final_evaluation_config_hash: str | None = None
    if result.get("kind") == "scriber_full_model_final_evaluation_result":
        if final_evaluation_config_path is None:
            raise HfQuantizationError(
                "full-model quantization requires the frozen final-evaluation config"
            )
        final_evaluation_config = Path(final_evaluation_config_path).expanduser().resolve()
        if final_evaluation_config.is_symlink() or not final_evaluation_config.is_file():
            raise HfQuantizationError("frozen final-evaluation config is invalid")
        final_evaluation_config_hash = hashlib.sha256(
            final_evaluation_config.read_bytes()
        ).hexdigest()
    plan = build_hf_quantization_dry_bind(
        final_evaluation_result=result,
        final_evaluation_result_sha256=_sha256(result_payload),
        final_evaluation_result_bytes=len(result_payload),
        final_evaluation_manifest=final_manifest,
        final_evaluation_manifest_sha256=_sha256(final_manifest_payload),
        final_evaluation_manifest_bytes=len(final_manifest_payload),
        training_report=training_report,
        training_report_sha256=_sha256(training_report_payload),
        remote_model_uri=remote_model_uri,
        model_weight_bytes=model_weight_bytes,
        llama_cpp_bundle=llama_binding,
        remote_llama_cpp_uri=remote_llama_cpp_uri,
        source_git_head=_git_head(),
        actual_cost_evidence=actual_cost_evidence,
        actual_cost_evidence_sha256=_sha256(actual_cost_evidence_payload),
        reference_root=references,
        evaluation_config_sha256=final_evaluation_config_hash,
    )
    plan = validate_hf_quantization_plan(plan)
    verify_hf_actual_cost_freshness(plan)
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise HfQuantizationError(f"refusing to overwrite HF quantization packet: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tmp-",
            dir=target.parent,
        )
    )
    project_root = Path(__file__).parents[2]
    try:
        packet_input = temporary / "input"
        packet_input.mkdir()
        shutil.copy2(
            Path(offline_wheelhouse_lock_path).expanduser().resolve(),
            packet_input / "quantization-wheels.lock.json",
        )
        (packet_input / "quantization-requirements.txt").write_bytes(requirements)
        packet_wheels = packet_input / "wheels"
        packet_wheels.mkdir()
        source_wheelhouse = Path(offline_wheelhouse_dir).expanduser().resolve()
        for record in dependencies["wheels"]:
            wheel = _mapping(record, "quantization wheel binding")
            filename = str(wheel["filename"])
            shutil.copy2(source_wheelhouse / filename, packet_wheels / filename)
        project = temporary / "repo/ml/scriber_polishing"
        project.mkdir(parents=True)
        for name in ("src", "scripts", "contracts"):
            _copy_tree(project_root / name, project / name)
        (project / "configs").mkdir()
        for name in ("evaluation.yaml", "quantization.yaml"):
            source = project_root / "configs" / name
            shutil.copy2(source, project / "configs" / name)
        for name in ("README.md", "pyproject.toml"):
            shutil.copy2(project_root / name, project / name)
        evaluation_hash = hashlib.sha256((project / "configs/evaluation.yaml").read_bytes()).hexdigest()
        if (
            evaluation_hash
            != _mapping(
                plan["evaluation"],
                "evaluation plan",
            )["config_sha256"]
        ):
            raise HfQuantizationError("local evaluation configuration differs from the final-evaluation binding")
        evaluation_input = project / "evaluation-input"
        evaluation_input.mkdir()
        for suite in _mapping(
            plan["references"],
            "quantization references",
        ).values():
            record = _mapping(suite, "reference suite")
            for filename_key, hash_key in (
                ("records_filename", "records_sha256"),
                ("manifest_filename", "manifest_sha256"),
            ):
                source = references / str(record[filename_key])
                if source.is_symlink() or not source.is_file():
                    raise HfQuantizationError(f"reference input is missing: {source}")
                actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if actual_hash != record[hash_key]:
                    raise HfQuantizationError(f"reference input differs from its binding: {source.name}")
                shutil.copy2(source, evaluation_input / source.name)
        job_input = project / "job-input"
        job_input.mkdir()
        (job_input / "hf-quantization-plan.json").write_bytes(_canonical_bytes(plan))
        (job_input / "hf-actual-cost-evidence.json").write_bytes(_canonical_bytes(actual_cost_evidence))
        shutil.copy2(
            final_evaluation_result_path,
            job_input / "final-evaluation-result.json",
        )
        shutil.copy2(
            final_evaluation_manifest_path,
            job_input / "final-evaluation-manifest.json",
        )
        shutil.copy2(
            training_report_path,
            job_input / "training-report.json",
        )
        runner = hf_quantization_runner_payload()
        (temporary / "run-hf-quantization.sh").write_bytes(runner)
        inventory, tree = _packet_inventory(temporary)
        manifest = {
            **plan,
            "dependencies": dependencies,
            "runner_sha256": _sha256(runner),
            "packet_tree_sha256": tree,
            "file_count": len(inventory),
            "byte_count": sum(int(record["bytes"]) for record in inventory),
        }
        (temporary / "job-manifest.json").write_bytes(_canonical_bytes(manifest))
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return HfQuantizationPacket(
        output_dir=target,
        manifest=manifest,
    )


def build_hf_quantization_launch_contract(
    packet_dir: str | Path,
    *,
    remote_packet_uri: str,
) -> dict[str, object]:
    """Build only the argv for one bounded, private, no-secret HF Job."""

    packet = Path(packet_dir).expanduser().resolve()
    manifest, _ = read_json_object(
        packet / "job-manifest.json",
        "HF quantization job manifest",
    )
    tree = str(manifest.get("packet_tree_sha256"))
    manifest_sha256 = _sha256(_canonical_bytes(manifest))
    verify_hf_quantization_packet(
        packet,
        expected_packet_tree_sha256=tree,
        expected_manifest_sha256=manifest_sha256,
        require_fresh_actual_cost=True,
    )
    packet_uri = _private_bucket_uri(
        remote_packet_uri,
        "HF quantization packet",
    )
    plan = validate_hf_quantization_plan(
        {
            key: value
            for key, value in manifest.items()
            if key
            not in {
                "runner_sha256",
                "packet_tree_sha256",
                "file_count",
                "byte_count",
                "dependencies",
            }
        }
    )
    source = _mapping(plan["source_model"], "quantization source")
    llama = _mapping(plan["llama_cpp"], "quantization llama.cpp")
    budget = _mapping(plan["budget"], "quantization budget")
    cost_freshness = verify_hf_actual_cost_freshness(plan)
    packet_volume = f"{packet_uri}:/input/repo:ro"
    model_volume = f"{source['remote_uri']}:/final-model:ro"
    llama_volume = f"{llama['remote_uri']}:/llama-cpp:ro"
    baseline = _mapping(
        plan["immutable_bf16_baseline"], "immutable BF16 baseline"
    )
    baseline_volume = f"{baseline['remote_uri']}:{FULL_MODEL_BF16_BASELINE_ROOT}:ro"
    output_volume = f"{QUANTIZATION_OUTPUT_MOUNT_URI}:/outputs:rw"
    validate_no_writable_read_only_uri_overlap(
        writable_uri=QUANTIZATION_OUTPUT_MOUNT_URI,
        read_only_uris=(
            packet_uri,
            str(source["remote_uri"]),
            str(llama["remote_uri"]),
            str(baseline["remote_uri"]),
        ),
    )
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        QUANTIZATION_JOB_NAME,
        "--flavor",
        QUANTIZATION_FLAVOR,
        "--timeout",
        f"{QUANTIZATION_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=quantization-full-120k-v2",
        "--label",
        QUANTIZATION_CAMPAIGN_LABEL,
        "--label",
        QUANTIZATION_ROLE_LABEL,
        "--label",
        QUANTIZATION_ATTEMPT_LABEL,
        "--label",
        f"job-timeout={QUANTIZATION_TIMEOUT_MINUTES}m",
        "--label",
        f"output-prefix={QUANTIZATION_OUTPUT_MOUNT_RELATIVE}",
        "--label",
        f"launch-claim={QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        model_volume,
        "-v",
        llama_volume,
        "-v",
        baseline_volume,
        "-v",
        output_volume,
        QUANTIZATION_IMAGE,
        "python",
        "-I",
        "-c",
        hf_quantization_bootstrap_payload(),
        tree,
        str(manifest["runner_sha256"]),
        manifest_sha256,
        QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    contract = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_launch_contract",
        "attempt": QUANTIZATION_ATTEMPT,
        "source_git_head": str(plan["source_git_head"]),
        "image": QUANTIZATION_IMAGE,
        "flavor": QUANTIZATION_FLAVOR,
        "timeout": f"{QUANTIZATION_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": budget["quantization_maximum_cost_usd"],
        "maximum_total_cost_usd": budget["maximum_total_cost_usd"],
        "maximum_authorized_new_spend_usd": budget[
            "maximum_remaining_credit_scope_cost_usd"
        ],
        "post_authorization_actual_spend_usd": budget[
            "post_authorization_actual_spend_usd"
        ],
        "fresh_campaign_inventory_required": True,
        "actual_cost_evidence_sha256": budget["actual_cost_evidence_sha256"],
        "accounted_campaign_job_ids": budget["accounted_campaign_job_ids"],
        "accounted_campaign_job_ids_sha256": budget[
            "accounted_campaign_job_ids_sha256"
        ],
        "actual_cost_evidence_expires_at_utc": cost_freshness["expires_at_utc"],
        "packet_tree_sha256": tree,
        "runner_sha256": manifest["runner_sha256"],
        "manifest_sha256": manifest_sha256,
        "packet_remote_uri": packet_uri,
        "model_remote_uri": source["remote_uri"],
        "llama_cpp_remote_uri": llama["remote_uri"],
        "bf16_baseline_remote_uri": baseline["remote_uri"],
        "packet_volume": packet_volume,
        "model_volume": model_volume,
        "llama_cpp_volume": llama_volume,
        "bf16_baseline_volume": baseline_volume,
        "output_volume": output_volume,
        "output_root": QUANTIZATION_OUTPUT_ROOT,
        "remote_result_uri": (f"{QUANTIZATION_OUTPUT_BUCKET}/{QUANTIZATION_OUTPUT_RELATIVE}"),
        "output_mount_uri": QUANTIZATION_OUTPUT_MOUNT_URI,
        "launch_claim_placeholder": QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER,
        "remote_output_absence_required": True,
        "argv": argv,
        "external_job_started": False,
        "publication_allowed": False,
        "secret_names": [],
    }
    return contract

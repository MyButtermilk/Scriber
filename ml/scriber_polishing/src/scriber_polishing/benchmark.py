"""Fail-closed laptop benchmark harness for local polishing-model variants."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .generation_adapters import DeterministicGenerationAdapter, GenerationMeasurement

_PROJECT_ROOT = Path(__file__).parents[2]
_CASE_SCHEMA = _PROJECT_ROOT / "contracts" / "benchmark_case_schema.json"
_REPORT_SCHEMA = _PROJECT_ROOT / "contracts" / "benchmark_report_schema.json"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_BACKENDS = {
    "bf16": "transformers",
    "int8": "transformers_bitsandbytes",
    "int4_nf4": "transformers_bitsandbytes",
    "Q8_0": "llama_cpp",
    "Q4_K_M": "llama_cpp",
}
_LLAMA_CUDA_DEVICE_COUNT = re.compile(
    r"found\s+([0-9]+)\s+CUDA devices?",
    re.IGNORECASE,
)
_LLAMA_CUDA_DEVICE = re.compile(
    r"Device\s+([0-9]+):\s*([^,\r\n]+),\s*"
    r"compute capability\s+([0-9]+\.[0-9]+)",
    re.IGNORECASE,
)
_LLAMA_CUDA_OFFLOAD = re.compile(
    r"offloaded\s+([0-9]+)\s*/\s*([0-9]+)\s+layers\s+to\s+GPU",
    re.IGNORECASE,
)
_LLAMA_CUDA_BUFFER = re.compile(
    r"CUDA[0-9]+\s+model buffer size\s*=",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_CUDA_DEVICE = re.compile(
    r"-\s+CUDA([0-9]+)\s*:\s*([^\r\n(]+?)\s*"
    r"\([0-9]+\s+MiB,\s*[0-9]+\s+MiB\s+free\)",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_CUDA_ARCHS = re.compile(
    r"CUDA\s*:\s*ARCHS\s*=\s*([0-9,]+)",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_CUDA_MODEL_MEMORY = re.compile(
    r"-\s+CUDA([0-9]+)\s+\([^)]+\)\s*\|\s*"
    r"[0-9]+\s*=\s*[0-9]+\s*\+\s*\(\s*[0-9]+\s*=\s*([0-9]+)\s*\+",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_FIT_UNCHANGED = re.compile(
    r"common_params_fit_impl:.*no changes needed",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_FIT_COMPLETE = re.compile(
    r"common_fit_params:\s*successfully fit params to free device memory",
    re.IGNORECASE,
)
_LLAMA_STRUCTURED_MODEL_LOADED = re.compile(
    r"llama_server:\s*model loaded",
    re.IGNORECASE,
)


class BenchmarkError(RuntimeError):
    """A benchmark input, measurement, or report is incomplete."""


class TelemetrySession(Protocol):
    def start(self) -> None: ...

    def stop(self) -> Mapping[str, object]: ...


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate(path: Path, value: Mapping[str, object], label: str) -> None:
    errors = sorted(
        _validator(path).iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise BenchmarkError(f"{label} schema failed at {location}: {errors[0].message}")


def load_benchmark_cases(
    path: str | Path,
    config: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Load a closed JSONL suite and require every configured category and bucket."""

    source = Path(path)
    rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BenchmarkError(f"cannot read benchmark cases {source}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise BenchmarkError(f"blank benchmark JSONL line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise BenchmarkError(
                f"invalid benchmark JSON at line {line_number}: {error.msg}"
            ) from error
        if not isinstance(value, dict):
            raise BenchmarkError(f"benchmark case at line {line_number} must be an object")
        _validate(_CASE_SCHEMA, value, "benchmark case")
        case_id = str(value["case_id"])
        if case_id in seen_ids:
            raise BenchmarkError(f"duplicate benchmark case_id: {case_id}")
        seen_ids.add(case_id)
        rows.append(value)
    if not rows:
        raise BenchmarkError("benchmark case file is empty")
    benchmark_config = config.get("benchmark")
    if not isinstance(benchmark_config, Mapping):
        raise BenchmarkError("quantization config is missing benchmark policy")
    required_categories = set(benchmark_config["required_case_categories"])
    actual_categories = {str(row["category"]) for row in rows}
    missing_categories = sorted(required_categories.difference(actual_categories))
    if missing_categories:
        raise BenchmarkError(f"missing required benchmark category: {missing_categories[0]}")
    required_buckets = {int(value) for value in benchmark_config["length_buckets"]}
    actual_buckets = {int(row["token_bucket"]) for row in rows}
    missing_buckets = sorted(required_buckets.difference(actual_buckets))
    if missing_buckets:
        raise BenchmarkError(f"missing required token bucket: {missing_buckets[0]}")
    expected_per_cell = int(benchmark_config["cases_per_category_bucket"])
    for category in sorted(required_categories):
        for bucket in sorted(required_buckets):
            observed = sum(
                1
                for row in rows
                if row["category"] == category and int(row["token_bucket"]) == bucket
            )
            if observed != expected_per_cell:
                raise BenchmarkError(
                    "benchmark category/bucket cell must contain exactly "
                    f"{expected_per_cell} case(s): category={category}, "
                    f"bucket={bucket}, observed={observed}"
                )
    return tuple(rows)


def _percentile(values: Sequence[float], percentile: int) -> float:
    if not values:
        raise BenchmarkError("cannot summarize an empty measurement series")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return round(ordered[rank - 1], 9)


def _distribution(values: Sequence[float], percentiles: Sequence[int]) -> dict[str, float]:
    return {f"p{percentile}": _percentile(values, percentile) for percentile in percentiles}


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_runtime_identity(
    *,
    kind: str,
    platform_name: str,
    details: Mapping[str, object],
) -> dict[str, object]:
    """Build one closed identity for the executable/package runtime under test."""

    if not kind or not re.fullmatch(r"[a-z0-9_]+", kind):
        raise BenchmarkError("runtime identity kind is invalid")
    if platform_name not in {"windows_x86_64", "linux_x86_64"}:
        raise BenchmarkError("runtime identity platform is unsupported")
    normalized_details = dict(details)
    if not normalized_details:
        raise BenchmarkError("runtime identity details are empty")
    core: dict[str, object] = {
        "kind": kind,
        "platform": platform_name,
        "details": normalized_details,
    }
    return {
        **core,
        "identity_sha256": _canonical_hash(core),
    }


def _validate_runtime_identity(value: Mapping[str, object]) -> dict[str, object]:
    identity = dict(value)
    if set(identity) != {"kind", "platform", "details", "identity_sha256"}:
        raise BenchmarkError("runtime identity fields are incomplete or unexpected")
    details = identity.get("details")
    if not isinstance(details, Mapping):
        raise BenchmarkError("runtime identity details must be an object")
    expected = build_runtime_identity(
        kind=str(identity.get("kind", "")),
        platform_name=str(identity.get("platform", "")),
        details=details,
    )
    if identity != expected:
        raise BenchmarkError("runtime identity hash differs from its closed details")
    return identity


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise BenchmarkError(f"{label} must use sha256:<64 lowercase hex>")
    return value


def _measurement_sample(
    measurement: GenerationMeasurement,
    case: Mapping[str, object],
    *,
    repetition: int,
    cold_start: bool,
    phase: str,
    adapter_load_seconds: float,
) -> dict[str, object]:
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "token_bucket": case["token_bucket"],
        "repetition": repetition,
        "cold_start": cold_start,
        "phase": phase,
        "adapter_load_seconds": adapter_load_seconds,
        "input_tokens": measurement.input_tokens,
        "output_tokens": measurement.output_tokens,
        "tokenization_seconds": measurement.tokenization_seconds,
        "model_load_seconds": measurement.model_load_seconds,
        "prefill_seconds": measurement.prefill_seconds,
        "time_to_first_token_seconds": measurement.time_to_first_token_seconds,
        "decode_seconds": measurement.decode_seconds,
        "end_to_end_seconds": measurement.end_to_end_seconds,
        "tokens_per_second": measurement.tokens_per_second,
    }


_LATENCY_FIELDS = (
    "tokenization_seconds",
    "model_load_seconds",
    "prefill_seconds",
    "time_to_first_token_seconds",
    "decode_seconds",
    "end_to_end_seconds",
)


def _sample_summary(
    samples: Sequence[Mapping[str, object]],
    percentiles: Sequence[int],
) -> dict[str, object]:
    return {
        "sample_count": len(samples),
        "latency": {
            field: _distribution(
                [float(sample[field]) for sample in samples],
                percentiles,
            )
            for field in _LATENCY_FIELDS
        },
        "throughput_tokens_per_second": _distribution(
            [float(sample["tokens_per_second"]) for sample in samples],
            percentiles,
        ),
    }


def _verify_runtime_execution(
    value: object,
    *,
    variant: str,
    expected_hardware: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise BenchmarkError("model runtime did not expose CUDA execution evidence")
    raw = dict(value)
    expected_name = str(expected_hardware["gpu_name"])
    expected_capability = str(expected_hardware["gpu_compute_capability"])
    expected_index = int(expected_hardware["gpu_device_index"])
    if variant in {"bf16", "int8", "int4_nf4"}:
        expected_fields = {
            "backend",
            "device",
            "loaded_variant",
            "model_devices",
            "full_model_on_gpu",
            "gpu_name",
            "compute_capability",
            "cuda_device_index",
            "cuda_memory_allocated_bytes",
            "cuda_memory_reserved_bytes",
        }
        devices = raw.get("model_devices")
        if (
            set(raw) != expected_fields
            or raw.get("backend") != "transformers_cuda"
            or raw.get("device") != "cuda"
            or raw.get("loaded_variant") != variant
            or not isinstance(devices, list)
            or not devices
            or any(
                not isinstance(device, str) or not device.startswith("cuda")
                for device in devices
            )
            or raw.get("full_model_on_gpu") is not True
            or raw.get("gpu_name") != expected_name
            or raw.get("compute_capability") != expected_capability
            or raw.get("cuda_device_index") != expected_index
            or not isinstance(raw.get("cuda_memory_allocated_bytes"), int)
            or int(raw["cuda_memory_allocated_bytes"]) < 1
            or not isinstance(raw.get("cuda_memory_reserved_bytes"), int)
            or int(raw["cuda_memory_reserved_bytes"]) < 1
        ):
            raise BenchmarkError(
                "Transformers benchmark did not prove full-model RTX 4070 CUDA execution"
            )
        core: dict[str, object] = {
            "schema_version": 1,
            "status": "verified",
            **raw,
        }
    else:
        expected_fields = {
            "backend",
            "device",
            "requested_gpu_layers",
            "gpu_layers_mode",
            "fit_mode",
            "runtime_instance_id",
            "runtime_executable_sha256",
            "stderr_startup",
        }
        stderr = raw.get("stderr_startup")
        if (
            set(raw) != expected_fields
            or raw.get("backend") != "llama_cpp_cuda"
            or raw.get("device") != "cuda"
            or raw.get("requested_gpu_layers") != 999
            or raw.get("gpu_layers_mode") != "all"
            or raw.get("fit_mode") != "on"
            or not isinstance(stderr, str)
        ):
            raise BenchmarkError(
                "llama.cpp benchmark did not expose the required CUDA diagnostics"
            )
        normalized_stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
        count_match = _LLAMA_CUDA_DEVICE_COUNT.search(normalized_stderr)
        device_match = _LLAMA_CUDA_DEVICE.search(normalized_stderr)
        offload_matches = list(_LLAMA_CUDA_OFFLOAD.finditer(normalized_stderr))
        common_core: dict[str, object] = {
            "schema_version": 1,
            "status": "verified",
            "backend": "llama_cpp_cuda",
            "device": "cuda",
            "loaded_variant": variant,
            "requested_gpu_layers": 999,
            "gpu_layers_mode": "all",
            "fit_mode": "on",
            "runtime_instance_id": _require_hash(
                raw.get("runtime_instance_id"),
                "llama.cpp runtime instance",
            ),
            "runtime_executable_sha256": _require_hash(
                raw.get("runtime_executable_sha256"),
                "llama.cpp runtime executable",
            ),
        }
        if (
            count_match is not None
            and device_match is not None
            and offload_matches
            and _LLAMA_CUDA_BUFFER.search(normalized_stderr) is not None
        ):
            device_count = int(count_match.group(1))
            device_index = int(device_match.group(1))
            gpu_name = device_match.group(2).strip()
            capability = device_match.group(3)
            offloaded_layers = int(offload_matches[-1].group(1))
            total_layers = int(offload_matches[-1].group(2))
            if (
                device_count < 1
                or device_index != expected_index
                or gpu_name != expected_name
                or capability != expected_capability
                or offloaded_layers < 1
                or offloaded_layers != total_layers
            ):
                raise BenchmarkError(
                    "llama.cpp benchmark did not fully offload the model to the bound RTX 4070"
                )
            offload_core: dict[str, object] = {
                "cuda_device_count": device_count,
                "cuda_device_index": device_index,
                "gpu_name": gpu_name,
                "compute_capability": capability,
                "offloaded_layers": offloaded_layers,
                "total_layers": total_layers,
                "offload_proof": "legacy_explicit_layer_count_v1",
            }
        else:
            structured_device = _LLAMA_STRUCTURED_CUDA_DEVICE.search(normalized_stderr)
            structured_archs = _LLAMA_STRUCTURED_CUDA_ARCHS.search(normalized_stderr)
            structured_memory = _LLAMA_STRUCTURED_CUDA_MODEL_MEMORY.search(
                normalized_stderr
            )
            capability_code = expected_capability.replace(".", "") + "0"
            if (
                structured_device is None
                or structured_archs is None
                or structured_memory is None
                or _LLAMA_STRUCTURED_FIT_UNCHANGED.search(normalized_stderr) is None
                or _LLAMA_STRUCTURED_FIT_COMPLETE.search(normalized_stderr) is None
                or _LLAMA_STRUCTURED_MODEL_LOADED.search(normalized_stderr) is None
            ):
                raise BenchmarkError(
                    "llama.cpp benchmark CUDA/full-offload diagnostics are incomplete"
                )
            device_index = int(structured_device.group(1))
            memory_device_index = int(structured_memory.group(1))
            gpu_name = structured_device.group(2).strip()
            architecture_codes = structured_archs.group(1).split(",")
            cuda_model_memory_mib = int(structured_memory.group(2))
            if (
                device_index != expected_index
                or memory_device_index != expected_index
                or gpu_name != expected_name
                or capability_code not in architecture_codes
                or cuda_model_memory_mib < 1
            ):
                raise BenchmarkError(
                    "llama.cpp benchmark did not fully offload the model to the bound RTX 4070"
                )
            offload_core = {
                "cuda_device_count": 1,
                "cuda_device_index": device_index,
                "gpu_name": gpu_name,
                "compute_capability": expected_capability,
                "cuda_model_memory_mib": cuda_model_memory_mib,
                "fit_parameters_unchanged": True,
                "offload_proof": "all_layers_requested_fit_unchanged_v1",
            }
        core = {
            **common_core,
            **offload_core,
            "full_model_on_gpu": True,
            "stderr_sha256": (
                "sha256:"
                + hashlib.sha256(normalized_stderr.encode("utf-8")).hexdigest()
            ),
        }
    return {
        **core,
        "evidence_sha256": _canonical_hash(core),
    }


def run_laptop_benchmark(
    *,
    adapter_factory: Callable[[], DeterministicGenerationAdapter],
    variant: str,
    cases: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
    config_sha256: str,
    cases_sha256: str,
    artifact_manifest_sha256: str,
    artifact_tree_sha256: str,
    training_fingerprint: str,
    artifact_identity: Mapping[str, object],
    runtime_identity: Mapping[str, object],
    artifact_size_bytes: int,
    telemetry_factory: Callable[[], TelemetrySession],
) -> dict[str, object]:
    """Measure one local variant and return complete, schema-validated evidence."""

    if variant not in _ARTIFACT_BACKENDS:
        raise BenchmarkError(f"unsupported benchmark variant: {variant}")
    if not cases:
        raise BenchmarkError("benchmark requires at least one case")
    if artifact_size_bytes < 1:
        raise BenchmarkError("manifest-bound artifact size must be positive")
    identity = dict(artifact_identity)
    runtime = _validate_runtime_identity(runtime_identity)
    if set(identity) != {
        "variant",
        "backend",
        "quantization_config",
        "quantization_config_sha256",
        "toolchain_sha256",
    }:
        raise BenchmarkError("artifact identity fields are incomplete or unexpected")
    if identity.get("variant") != variant:
        raise BenchmarkError("artifact identity variant differs from benchmark variant")
    if identity.get("backend") != _ARTIFACT_BACKENDS[variant]:
        raise BenchmarkError("artifact identity backend differs from benchmark variant")
    quantization_config = identity.get("quantization_config")
    if not isinstance(quantization_config, Mapping) or not quantization_config:
        raise BenchmarkError("artifact identity is missing quantization config")
    if identity.get("quantization_config_sha256") != _canonical_hash(
        dict(quantization_config)
    ):
        raise BenchmarkError("artifact quantization config hash differs")
    for label, value in (
        ("config hash", config_sha256),
        ("case-suite hash", cases_sha256),
        ("artifact manifest hash", artifact_manifest_sha256),
        ("artifact tree hash", artifact_tree_sha256),
        ("training fingerprint", training_fingerprint),
        ("toolchain hash", identity.get("toolchain_sha256")),
    ):
        _require_hash(value, label)
    benchmark_config = config.get("benchmark")
    if not isinstance(benchmark_config, Mapping):
        raise BenchmarkError("quantization config is missing benchmark policy")
    warm_repetitions = int(benchmark_config["warm_repetitions"])
    warmup_repetitions = int(benchmark_config["warmup_repetitions"])
    if warmup_repetitions != 1:
        raise BenchmarkError("benchmark requires exactly one excluded warmup repetition")
    cold_repetitions = int(benchmark_config["cold_repetitions"])
    output_budgets = {
        int(bucket): int(value)
        for bucket, value in benchmark_config["max_new_tokens_by_input_bucket"].items()
    }
    tolerance = int(benchmark_config["bucket_tolerance_tokens"])
    percentiles = tuple(int(value) for value in benchmark_config["percentiles"])
    expected_hardware = benchmark_config.get("expected_hardware")
    if not isinstance(expected_hardware, Mapping):
        raise BenchmarkError("quantization config is missing expected laptop hardware")
    telemetry = telemetry_factory()
    telemetry.start()
    adapter: DeterministicGenerationAdapter | None = None
    samples: list[dict[str, object]] = []
    cold_load_seconds: list[float] = []
    cold_adapter_ids: set[int] = set()
    cold_adapter_references: list[DeterministicGenerationAdapter] = []
    cold_model_sizes: set[int] = set()
    cold_backends: set[str] = set()
    cold_protocols: set[str] = set()
    cold_executable_hashes: set[str] = set()
    closed_adapter_ids: set[int] = set()
    warm_load_seconds = 0.0
    warmup_seconds = 0.0
    warm_runtime_mode = ""
    warm_runtime_protocol = ""
    warm_runtime_executable_sha256 = ""
    warm_runtime_instance_id = ""
    warm_health_before = False
    warm_health_after = False
    model_size_bytes = 0
    resources: Mapping[str, object] | None = None
    runtime_execution: dict[str, object] | None = None

    def close_adapter(instance: DeterministicGenerationAdapter) -> None:
        identity = id(instance)
        if identity in closed_adapter_ids:
            raise BenchmarkError("adapter_factory reused an already-closed adapter instance")
        instance.close()
        closed_adapter_ids.add(identity)

    def measure(
        instance: DeterministicGenerationAdapter,
        case: Mapping[str, object],
        *,
        repetition: int,
        cold_start: bool,
        phase: str,
        adapter_load_seconds: float,
    ) -> dict[str, object]:
        actual_tokens = instance.count_input_tokens(str(case["text"]))
        token_bucket = int(case["token_bucket"])
        if abs(actual_tokens - token_bucket) > tolerance:
            raise BenchmarkError(
                f"case {case['case_id']} has {actual_tokens} input tokens; "
                f"outside {token_bucket}±{tolerance}"
            )
        measurement = instance.generate(
            str(case["text"]),
            max_new_tokens=output_budgets[token_bucket],
        )
        if measurement.input_tokens != actual_tokens:
            raise BenchmarkError(
                f"adapter token count changed during generation for {case['case_id']}"
            )
        if instance.runtime_instance_id != warm_runtime_instance_id and not cold_start:
            raise BenchmarkError("persistent warm runtime instance changed during measurement")
        return _measurement_sample(
            measurement,
            case,
            repetition=repetition,
            cold_start=cold_start,
            phase=phase,
            adapter_load_seconds=adapter_load_seconds,
        )

    try:
        cold_case = min(
            cases,
            key=lambda case: (int(case["token_bucket"]), str(case["case_id"])),
        )
        for repetition in range(1, cold_repetitions + 1):
            load_started = time.perf_counter()
            cold_adapter = adapter_factory()
            load_seconds = time.perf_counter() - load_started
            if cold_adapter.runtime_mode != "persistent_model_process":
                raise BenchmarkError("cold benchmark adapter must own a persistent model runtime")
            if not cold_adapter.health():
                raise BenchmarkError("cold benchmark runtime is unhealthy after model load")
            cold_size = int(cold_adapter.model_size_bytes)
            if cold_size < 1 or cold_size > artifact_size_bytes:
                raise BenchmarkError("cold adapter model size is outside artifact bounds")
            cold_model_sizes.add(cold_size)
            cold_backends.add(cold_adapter.backend)
            cold_protocols.add(cold_adapter.runtime_protocol)
            cold_executable_hashes.add(
                _require_hash(
                    cold_adapter.runtime_executable_sha256,
                    "cold runtime executable hash",
                )
            )
            if id(cold_adapter) in cold_adapter_ids:
                raise BenchmarkError("cold-load benchmark reused an adapter instance")
            cold_adapter_ids.add(id(cold_adapter))
            cold_adapter_references.append(cold_adapter)
            cold_load_seconds.append(load_seconds)
            try:
                samples.append(
                    measure(
                        cold_adapter,
                        cold_case,
                        repetition=repetition,
                        cold_start=True,
                        phase="cold_load",
                        adapter_load_seconds=load_seconds,
                    )
                )
            finally:
                close_adapter(cold_adapter)
        load_started = time.perf_counter()
        adapter = adapter_factory()
        warm_load_seconds = time.perf_counter() - load_started
        if id(adapter) in cold_adapter_ids:
            raise BenchmarkError("warm benchmark reused a closed cold-load adapter")
        warm_runtime_mode = str(getattr(adapter, "runtime_mode", ""))
        if warm_runtime_mode != "persistent_model_process":
            raise BenchmarkError("warm benchmark requires one persistent model runtime")
        warm_runtime_protocol = str(getattr(adapter, "runtime_protocol", ""))
        if warm_runtime_protocol not in {
            "in_process_transformers_v1",
            "llama_server_http_sse_v1",
        }:
            raise BenchmarkError("adapter must declare an approved persistent runtime protocol")
        warm_runtime_executable_sha256 = str(
            getattr(adapter, "runtime_executable_sha256", "")
        )
        if not (
            warm_runtime_executable_sha256.startswith("sha256:")
            and len(warm_runtime_executable_sha256) == 71
        ):
            raise BenchmarkError("adapter runtime executable hash is missing")
        warm_runtime_instance_id = str(getattr(adapter, "runtime_instance_id", ""))
        if not warm_runtime_instance_id:
            raise BenchmarkError("adapter runtime instance identity is missing")
        warm_health_before = adapter.health()
        if not warm_health_before:
            raise BenchmarkError("warm runtime is unhealthy before warmup")
        model_size_bytes = int(adapter.model_size_bytes)
        if model_size_bytes < 1 or model_size_bytes > artifact_size_bytes:
            raise BenchmarkError("adapter model size is outside artifact bounds")
        expected_adapter_backend = (
            "llama_cpp_server"
            if identity["backend"] == "llama_cpp"
            else "transformers"
        )
        if adapter.backend != expected_adapter_backend:
            raise BenchmarkError("adapter backend differs from artifact backend")
        if (
            cold_model_sizes != {model_size_bytes}
            or cold_backends != {adapter.backend}
            or cold_protocols != {warm_runtime_protocol}
            or cold_executable_hashes != {warm_runtime_executable_sha256}
        ):
            raise BenchmarkError("cold and warm runtime identities differ")
        warmup_case = min(
            cases,
            key=lambda case: (int(case["token_bucket"]), str(case["case_id"])),
        )
        warmup_bucket = int(warmup_case["token_bucket"])
        warmup_started = time.perf_counter()
        warmup_measurement = adapter.warmup(
            str(warmup_case["text"]),
            max_new_tokens=output_budgets[warmup_bucket],
        )
        warmup_seconds = time.perf_counter() - warmup_started
        if warmup_measurement.input_tokens != adapter.count_input_tokens(
            str(warmup_case["text"])
        ):
            raise BenchmarkError("warmup token count differs from the adapter tokenizer")
        if adapter.runtime_instance_id != warm_runtime_instance_id:
            raise BenchmarkError("warmup replaced the persistent runtime instance")
        for repetition in range(1, warm_repetitions + 1):
            for case in cases:
                samples.append(
                    measure(
                        adapter,
                        case,
                        repetition=repetition,
                        cold_start=False,
                        phase="warm_runtime",
                        adapter_load_seconds=0.0,
                    )
                )
        warm_health_after = adapter.health()
        if not warm_health_after:
            raise BenchmarkError("warm runtime is unhealthy after measurement")
        if adapter.runtime_instance_id != warm_runtime_instance_id:
            raise BenchmarkError("persistent runtime instance changed after measurement")
        runtime_execution = _verify_runtime_execution(
            getattr(adapter, "runtime_execution_evidence", None),
            variant=variant,
            expected_hardware=expected_hardware,
        )
    except BenchmarkError:
        raise
    except Exception as error:
        raise BenchmarkError(f"benchmark runtime failed: {error}") from error
    finally:
        close_error: Exception | None = None
        if adapter is not None:
            try:
                close_adapter(adapter)
            except Exception as error:
                close_error = error
        try:
            resources = telemetry.stop()
        except Exception as error:
            if close_error is not None:
                raise BenchmarkError(
                    f"adapter cleanup failed ({close_error}); telemetry cleanup failed ({error})"
                ) from error
            raise
        if close_error is not None:
            raise BenchmarkError(f"adapter cleanup failed: {close_error}") from close_error
    if adapter is None or resources is None:
        raise BenchmarkError("benchmark did not initialize its runtime")
    cold_samples = [sample for sample in samples if sample["cold_start"]]
    warm_samples = [sample for sample in samples if not sample["cold_start"]]
    if not warm_samples:
        raise BenchmarkError("benchmark produced no warm-start samples")
    if len(cold_samples) != cold_repetitions:
        raise BenchmarkError("benchmark produced incomplete repeated cold-load samples")
    summary = _sample_summary(warm_samples, percentiles)
    by_bucket = {
        str(bucket): _sample_summary(
            [
                sample
                for sample in warm_samples
                if int(sample["token_bucket"]) == bucket
            ],
            percentiles,
        )
        for bucket in benchmark_config["length_buckets"]
    }
    by_category = {
        str(category): _sample_summary(
            [
                sample
                for sample in warm_samples
                if str(sample["category"]) == category
            ],
            percentiles,
        )
        for category in benchmark_config["required_case_categories"]
    }
    cpu_samples = [float(value) for value in resources["cpu_percent_samples"]]
    gpu_samples = [float(value) for value in resources["gpu_percent_samples"]]
    hardware = resources.get("hardware")
    if not isinstance(hardware, Mapping):
        raise BenchmarkError("telemetry did not return a hardware identity")
    expected_total_bytes = int(expected_hardware["gpu_total_memory_mib"]) * 1024 * 1024
    hardware_checks = {
        "os_family": str(hardware.get("os", "")).startswith(
            str(expected_hardware["os_family"])
        ),
        "gpu_name": hardware.get("gpu_name") == expected_hardware["gpu_name"],
        "gpu_total_memory": hardware.get("gpu_total_memory_bytes")
        == expected_total_bytes,
        "gpu_driver_version": hardware.get("gpu_driver_version")
        == expected_hardware["gpu_driver_version"],
        "gpu_compute_capability": hardware.get("gpu_compute_capability")
        == expected_hardware["gpu_compute_capability"],
        "bf16_support": (
            hardware.get("torch_bf16_supported") is True
            if expected_hardware["require_bf16_support"]
            else True
        ),
        "gpu_activity": max(gpu_samples, default=0.0) > 0,
        "vram_growth": int(resources.get("peak_vram_delta_bytes", 0)) > 0,
    }
    failed_hardware_checks = sorted(
        name for name, passed in hardware_checks.items() if not passed
    )
    if failed_hardware_checks:
        raise BenchmarkError(
            "benchmark did not run on the bound RTX 4070 laptop hardware: "
            + ", ".join(failed_hardware_checks)
        )
    report: dict[str, object] = {
        "schema_version": 4,
        "status": "complete",
        "variant": variant,
        "backend": adapter.backend,
        "config_sha256": config_sha256,
        "cases_sha256": cases_sha256,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "artifact_tree_sha256": artifact_tree_sha256,
        "training_fingerprint": training_fingerprint,
        "artifact_identity": identity,
        "runtime_identity": runtime,
        "runtime_execution": runtime_execution,
        "artifact_size_bytes": artifact_size_bytes,
        "model_size_bytes": model_size_bytes,
        "measurement_definitions": {
            "model_load": "adapter_factory wall time through successful runtime health",
            "cold_start": (
                "fresh model load plus first end-to-end generation on that runtime"
            ),
            "warm_start": (
                "end-to-end generation after one excluded warmup on the same persistent runtime"
            ),
            "prefill": "model-reported prompt evaluation time excluding tokenization",
            "time_to_first_token": (
                "tokenization plus request wall time through first non-empty completion token"
            ),
            "tokens_per_second": (
                "model-reported decoded completion tokens per decode second"
            ),
            "end_to_end": "tokenization plus complete local generation wall time",
            "percentiles": (
                "nearest-rank p50/p95/p99 over warm measured samples unless named cold"
            ),
            "resources": (
                "peak process-tree RAM and physical-device VRAM plus sampled CPU/GPU utilization"
            ),
            "size": (
                "model_size_bytes is loaded weight file/tree; artifact_size_bytes is "
                "manifest-bound package bytes"
            ),
        },
        "adapter_load_seconds": _distribution(cold_load_seconds, percentiles),
        "warm_adapter_load_seconds": warm_load_seconds,
        "warmup_seconds": warmup_seconds,
        "cold_start_seconds": _distribution(
            [
                float(sample["adapter_load_seconds"])
                + float(sample["end_to_end_seconds"])
                for sample in cold_samples
            ],
            percentiles,
        ),
        "warm_start_seconds": _distribution(
            [float(sample["end_to_end_seconds"]) for sample in warm_samples],
            percentiles,
        ),
        "sample_count": len(samples),
        "cold_sample_count": len(cold_samples),
        "warm_sample_count": len(warm_samples),
        "runtime_protocol": {
            "cold_loads": {
                "adapter_instances": cold_repetitions,
                "repetitions": cold_repetitions,
                "case_id": cold_samples[0]["case_id"],
            },
            "warm_runtime": {
                "adapter_instances": 1,
                "persistent_model_process": True,
                "repetitions_per_case": warm_repetitions,
                "warmup_repetitions": warmup_repetitions,
                "health_before_warmup": warm_health_before,
                "health_after_measurement": warm_health_after,
                "instance_identity_stable": True,
                "runtime_protocol": warm_runtime_protocol,
                "runtime_executable_sha256": warm_runtime_executable_sha256,
            },
        },
        "latency": summary["latency"],
        "throughput_tokens_per_second": summary["throughput_tokens_per_second"],
        "by_token_bucket": by_bucket,
        "by_case_category": by_category,
        "resources": {
            "telemetry_sample_count": int(resources["sample_count"]),
            "peak_ram_bytes": int(resources["peak_ram_bytes"]),
            "peak_vram_bytes": int(resources["peak_vram_bytes"]),
            "baseline_vram_bytes": int(resources["baseline_vram_bytes"]),
            "peak_vram_delta_bytes": int(resources["peak_vram_delta_bytes"]),
            "peak_process_count": int(resources["peak_process_count"]),
            "cpu_percent": _distribution(cpu_samples, percentiles),
            "gpu_percent": _distribution(gpu_samples, percentiles),
            "telemetry_scope": dict(resources["telemetry_scope"]),
            "hardware": dict(hardware),
            "hardware_matches_expected": True,
            "hardware_checks": hardware_checks,
        },
        "samples": samples,
    }
    _validate(_REPORT_SCHEMA, report, "benchmark report")
    return report


def write_benchmark_report(path: str | Path, report: Mapping[str, object]) -> None:
    """Write an immutable validated JSON report."""

    _validate(_REPORT_SCHEMA, report, "benchmark report")
    destination = Path(path)
    payload = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == payload:
            return
        raise BenchmarkError(f"refusing to overwrite benchmark report: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)


class SystemTelemetrySampler:
    """Sample process RAM/CPU and NVIDIA device VRAM/GPU utilization."""

    def __init__(
        self,
        *,
        interval_seconds: float = 0.05,
        gpu_index: int = 0,
        require_gpu: bool = True,
    ) -> None:
        if interval_seconds < 0.02:
            raise ValueError("telemetry interval must be at least 20 ms")
        self._interval = interval_seconds
        self._gpu_index = gpu_index
        self._require_gpu = require_gpu
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[tuple[int, float, int, float, int]] = []
        self._error: Exception | None = None
        self._gpu_name: str | None = None
        self._nvml: Any = None
        self._gpu_handle: Any = None
        self._hardware: dict[str, object] = {}

    def start(self) -> None:
        if self._thread is not None:
            raise BenchmarkError("telemetry sampler was already started")
        try:
            import psutil

            self._psutil = psutil
            self._process = psutil.Process()
            self._process.cpu_percent(None)
            self._processes_by_pid = {self._process.pid: self._process}
            self._hardware = {
                "os": platform.platform(),
                "python": platform.python_version(),
                "cpu_name": platform.processor()
                or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
                "logical_cpu_count": int(psutil.cpu_count(logical=True) or 0),
                "physical_cpu_count": int(psutil.cpu_count(logical=False) or 0),
                "total_ram_bytes": int(psutil.virtual_memory().total),
                "gpu_name": None,
                "gpu_uuid": None,
                "gpu_driver_version": None,
                "gpu_total_memory_bytes": 0,
                "gpu_compute_capability": None,
                "torch_bf16_supported": False,
            }
            try:
                import pynvml

                pynvml.nvmlInit()
                self._nvml = pynvml
                self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(self._gpu_index)
                name = pynvml.nvmlDeviceGetName(self._gpu_handle)
                self._gpu_name = (
                    name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
                )
                uuid = pynvml.nvmlDeviceGetUUID(self._gpu_handle)
                driver = pynvml.nvmlSystemGetDriverVersion()
                memory = pynvml.nvmlDeviceGetMemoryInfo(self._gpu_handle)
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(
                    self._gpu_handle
                )
                try:
                    import torch

                    torch_bf16_supported = bool(
                        torch.cuda.is_available()
                        and torch.cuda.is_bf16_supported()
                    )
                except Exception:
                    torch_bf16_supported = False
                self._hardware.update(
                    {
                        "gpu_name": self._gpu_name,
                        "gpu_uuid": (
                            uuid.decode("utf-8", errors="replace")
                            if isinstance(uuid, bytes)
                            else str(uuid)
                        ),
                        "gpu_driver_version": (
                            driver.decode("utf-8", errors="replace")
                            if isinstance(driver, bytes)
                            else str(driver)
                        ),
                        "gpu_total_memory_bytes": int(memory.total),
                        "gpu_compute_capability": f"{major}.{minor}",
                        "torch_bf16_supported": torch_bf16_supported,
                    }
                )
            except Exception as error:
                if self._require_gpu:
                    raise BenchmarkError(f"NVIDIA telemetry is required but unavailable: {error}") from error
            self._sample_once()
            self._thread = threading.Thread(
                target=self._sample_loop,
                name="scriber-benchmark-telemetry",
                daemon=True,
            )
            self._thread.start()
        except BenchmarkError:
            raise
        except Exception as error:
            raise BenchmarkError(f"system telemetry could not start: {error}") from error

    def _sample_once(self) -> None:
        current_processes = [self._process]
        with suppress(self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            current_processes.extend(self._process.children(recursive=True))
        processes = []
        for observed in current_processes:
            process = self._processes_by_pid.get(observed.pid)
            if process is None or process != observed:
                process = observed
                try:
                    process.cpu_percent(None)
                except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                    continue
                self._processes_by_pid[observed.pid] = process
            processes.append(process)
        rss = 0
        cpu = 0.0
        observed_processes = 0
        for process in processes:
            try:
                rss += int(process.memory_info().rss)
                cpu += float(process.cpu_percent(None))
                observed_processes += 1
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        vram = 0
        gpu = 0.0
        if self._nvml is not None:
            vram = int(self._nvml.nvmlDeviceGetMemoryInfo(self._gpu_handle).used)
            gpu = float(self._nvml.nvmlDeviceGetUtilizationRates(self._gpu_handle).gpu)
        self._samples.append((rss, cpu, vram, gpu, observed_processes))

    def _sample_loop(self) -> None:
        try:
            while not self._stop_event.wait(self._interval):
                self._sample_once()
        except Exception as error:
            self._error = error

    def stop(self) -> Mapping[str, object]:
        if self._thread is None:
            raise BenchmarkError("telemetry sampler was not started")
        self._stop_event.set()
        self._thread.join(timeout=max(2.0, self._interval * 4))
        if self._thread.is_alive():
            raise BenchmarkError("telemetry sampler did not stop")
        if not self._samples and self._error is None:
            try:
                self._sample_once()
            except Exception as error:
                self._error = error
        if self._nvml is not None:
            self._nvml.nvmlShutdown()
        if self._error is not None:
            raise BenchmarkError(f"telemetry sampling failed: {self._error}") from self._error
        if not self._samples:
            raise BenchmarkError("telemetry produced no samples")
        baseline_vram = self._samples[0][2]
        peak_vram = max(sample[2] for sample in self._samples)
        return {
            "sample_count": len(self._samples),
            "peak_ram_bytes": max(sample[0] for sample in self._samples),
            "peak_vram_bytes": peak_vram,
            "baseline_vram_bytes": baseline_vram,
            "peak_vram_delta_bytes": max(0, peak_vram - baseline_vram),
            "cpu_percent_samples": [sample[1] for sample in self._samples],
            "gpu_percent_samples": [sample[3] for sample in self._samples],
            "peak_process_count": max(sample[4] for sample in self._samples),
            "telemetry_scope": {
                "cpu_memory": "root_process_tree",
                "gpu": "physical_device",
            },
            "hardware": dict(self._hardware),
        }

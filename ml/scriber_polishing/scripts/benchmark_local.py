"""Benchmark one local Transformers polishing-model variant on this laptop."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.benchmark import (  # noqa: E402
    BenchmarkError,
    SystemTelemetrySampler,
    build_runtime_identity,
    load_benchmark_cases,
    run_laptop_benchmark,
    write_benchmark_report,
)
from scriber_polishing.generation_adapters import (  # noqa: E402
    LlamaCppServerGenerationAdapter,
    TransformersGenerationAdapter,
)
from scriber_polishing.llama_cpp_bundle import (  # noqa: E402
    LlamaCppBundleError,
    load_verified_llama_cpp_bundle,
)
from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    load_quantization_config,
    sha256_file,
)
from scriber_polishing.variant_artifacts import (  # noqa: E402
    VARIANT_MANIFEST_FILENAME,
    VariantArtifactError,
    verify_variant_artifact_manifest,
)
from scriber_polishing.windows_llama_cpp_runtime import (  # noqa: E402
    WindowsLlamaCppRuntimeError,
    load_verified_windows_llama_cpp_runtime,
)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _transformers_runtime_identity(*, variant: str, gpu_index: int) -> dict[str, object]:
    try:
        import torch
    except ImportError as error:
        raise BenchmarkError("PyTorch is required for the Transformers benchmark") from error
    if not torch.cuda.is_available():
        raise BenchmarkError("the bound laptop benchmark requires CUDA")
    if gpu_index < 0 or gpu_index >= torch.cuda.device_count():
        raise BenchmarkError("requested CUDA device index is unavailable")
    properties = torch.cuda.get_device_properties(gpu_index)
    details = {
        "variant": variant,
        "python_executable_sha256": _sha256(Path(sys.executable)),
        "packages": {
            "accelerate": _package_version("accelerate"),
            "bitsandbytes": _package_version("bitsandbytes"),
            "torch": _package_version("torch"),
            "transformers": _package_version("transformers"),
        },
        "torch_cuda_version": str(torch.version.cuda),
        "requested_device": f"cuda:{gpu_index}",
        "cuda_device_count": int(torch.cuda.device_count()),
        "cuda_device_name": str(torch.cuda.get_device_name(gpu_index)),
        "cuda_compute_capability": (
            f"{int(properties.major)}.{int(properties.minor)}"
        ),
        "cuda_total_memory_bytes": int(properties.total_memory),
        "torch_bf16_supported": bool(torch.cuda.is_bf16_supported()),
    }
    return build_runtime_identity(
        kind="transformers_local_cuda",
        platform_name="windows_x86_64",
        details=details,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "quantization.yaml")
    parser.add_argument("--backend", choices=("transformers", "llama-cpp"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--variant",
        choices=("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"),
        required=True,
    )
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--llama-cpp-bundle", type=Path)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime-manifest", type=Path)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--llama-gpu-layers", type=int)
    parser.add_argument("--allow-missing-gpu-telemetry", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_quantization_config(args.config)
        cases = load_benchmark_cases(args.cases, config)
        expected_gpu_index = int(
            config["benchmark"]["expected_hardware"]["gpu_device_index"]
        )
        if args.gpu_index != expected_gpu_index:
            raise BenchmarkError(
                "GPU index differs from the bound laptop hardware policy"
            )
        interval = config["benchmark"]["telemetry_sample_interval_ms"] / 1000
        artifact_root = (
            args.model.resolve()
            if args.model.is_dir()
            else args.model.resolve().parent
        )
        artifact_manifest = verify_variant_artifact_manifest(artifact_root)
        if artifact_manifest["variant"] != args.variant:
            raise BenchmarkError(
                "artifact manifest variant differs from requested benchmark variant"
            )
        if args.backend == "transformers":
            if args.variant not in {"bf16", "int8", "int4_nf4"}:
                raise BenchmarkError(
                    f"Transformers backend does not support variant {args.variant}"
                )
            if args.device != "cuda":
                raise BenchmarkError("the bound laptop benchmark requires --device cuda")
            runtime_identity = _transformers_runtime_identity(
                variant=args.variant,
                gpu_index=args.gpu_index,
            )

            def adapter_factory():
                return TransformersGenerationAdapter.load(
                    args.model,
                    variant=args.variant,
                    device="cuda",
                )
        else:
            if args.variant not in {"Q8_0", "Q4_K_M"}:
                raise BenchmarkError(f"llama.cpp backend does not support variant {args.variant}")
            toolchain = artifact_manifest["toolchain"]
            if args.device != "cuda":
                raise BenchmarkError(
                    "the bound laptop GGUF benchmark requires --device cuda"
                )
            runtime_path_entries: tuple[Path, ...] = ()
            if platform.system() == "Windows":
                if (
                    args.windows_llama_cpp_runtime is None
                    or args.windows_llama_cpp_runtime_manifest is None
                ):
                    raise BenchmarkError(
                        "native Windows GGUF benchmark requires "
                        "--windows-llama-cpp-runtime and "
                        "--windows-llama-cpp-runtime-manifest"
                    )
                windows_runtime = load_verified_windows_llama_cpp_runtime(
                    args.windows_llama_cpp_runtime,
                    args.windows_llama_cpp_runtime_manifest,
                )
                if toolchain["llama_cpp_commit"] != windows_runtime.conversion_commit:
                    raise BenchmarkError(
                        "GGUF conversion commit differs from the Windows runtime "
                        "compatibility binding"
                    )
                llama_server = windows_runtime.llama_server
                llama_server_sha256 = windows_runtime.llama_server_sha256
                runtime_path_entries = windows_runtime.runtime_path_entries
                runtime_identity = build_runtime_identity(
                    kind="llama_cpp_windows_cuda",
                    platform_name="windows_x86_64",
                    details={
                        "runtime_manifest_sha256": windows_runtime.manifest_sha256,
                        "runtime_tree_sha256": windows_runtime.runtime_tree_sha256,
                        "distribution_lock_sha256": (
                            windows_runtime.distribution_lock_sha256
                        ),
                        "release_tag": windows_runtime.release_tag,
                        "llama_cpp_commit": windows_runtime.commit,
                        "gguf_conversion_tag": windows_runtime.conversion_tag,
                        "gguf_conversion_commit": windows_runtime.conversion_commit,
                        "llama_server_sha256": windows_runtime.llama_server_sha256,
                        "requested_gpu_layers": (
                            args.llama_gpu_layers
                            if args.llama_gpu_layers is not None
                            else 999
                        ),
                    },
                )
            else:
                if args.llama_cpp_bundle is None or args.llama_cpp_bundle_lock is None:
                    raise BenchmarkError(
                        "Linux llama.cpp backend requires --llama-cpp-bundle and "
                        "--llama-cpp-bundle-lock"
                    )
                bundle = load_verified_llama_cpp_bundle(
                    args.llama_cpp_bundle,
                    args.llama_cpp_bundle_lock,
                )
                if (
                    toolchain["bundle_lock_sha256"] != bundle.lock_sha256
                    or toolchain["llama_cpp_commit"] != bundle.commit
                    or toolchain["source_tree_sha256"] != bundle.source_tree_sha256
                    or toolchain["build_identity_sha256"]
                    != bundle.build_identity_sha256
                ):
                    raise BenchmarkError(
                        "GGUF artifact toolchain identity differs from the "
                        "verified Linux bundle"
                    )
                llama_server = bundle.llama_server
                llama_server_sha256 = sha256_file(bundle.llama_server)
                runtime_identity = build_runtime_identity(
                    kind="llama_cpp_linux_cuda",
                    platform_name="linux_x86_64",
                    details={
                        "bundle_lock_sha256": bundle.lock_sha256,
                        "runtime_tree_sha256": bundle.native_runtime[
                            "tree_sha256"
                        ],
                        "llama_cpp_commit": bundle.commit,
                        "llama_server_sha256": llama_server_sha256,
                        "requested_gpu_layers": (
                            args.llama_gpu_layers
                            if args.llama_gpu_layers is not None
                            else 999
                        ),
                    },
                )
            tokenizer_root = (
                args.tokenizer.resolve()
                if args.tokenizer is not None
                else artifact_root
            )
            if tokenizer_root != artifact_root:
                raise BenchmarkError(
                    "GGUF tokenizer must come from the same verified variant artifact"
                )

            def adapter_factory():
                runtime = config["llama_cpp_runtime"]
                gpu_layers = (
                    args.llama_gpu_layers
                    if args.llama_gpu_layers is not None
                    else (999 if args.device == "cuda" else 0)
                )
                original_path = os.environ.get("PATH")
                if runtime_path_entries:
                    os.environ["PATH"] = os.pathsep.join(
                        (
                            *(str(path) for path in runtime_path_entries),
                            original_path or "",
                        )
                    )
                try:
                    return LlamaCppServerGenerationAdapter.load(
                        args.model,
                        llama_server_path=llama_server,
                        llama_server_sha256=llama_server_sha256,
                        tokenizer_path=tokenizer_root,
                        context_size=int(runtime["context_size"]),
                        gpu_layers=gpu_layers,
                        startup_timeout_seconds=float(
                            runtime["startup_timeout_seconds"]
                        ),
                        request_timeout_seconds=float(
                            runtime["request_timeout_seconds"]
                        ),
                        shutdown_timeout_seconds=float(
                            runtime["shutdown_timeout_seconds"]
                        ),
                        stderr_max_bytes=int(runtime["stderr_max_bytes"]),
                    )
                finally:
                    if original_path is None:
                        os.environ.pop("PATH", None)
                    else:
                        os.environ["PATH"] = original_path
        quantization = artifact_manifest["quantization"]
        artifact_identity = {
            "variant": artifact_manifest["variant"],
            "backend": artifact_manifest["backend"],
            "quantization_config": quantization["config"],
            "quantization_config_sha256": quantization["config_sha256"],
            "toolchain_sha256": _canonical_sha256(artifact_manifest["toolchain"]),
        }
        report = run_laptop_benchmark(
            adapter_factory=adapter_factory,
            variant=args.variant,
            cases=cases,
            config=config,
            config_sha256=_sha256(args.config),
            cases_sha256=_sha256(args.cases),
            artifact_manifest_sha256=_sha256(
                artifact_root / VARIANT_MANIFEST_FILENAME
            ),
            artifact_tree_sha256=artifact_manifest["artifact"]["tree_sha256"],
            training_fingerprint=artifact_manifest["training_fingerprint"],
            artifact_identity=artifact_identity,
            runtime_identity=runtime_identity,
            artifact_size_bytes=int(artifact_manifest["artifact"]["total_bytes"]),
            telemetry_factory=lambda: SystemTelemetrySampler(
                interval_seconds=interval,
                gpu_index=args.gpu_index,
                require_gpu=not args.allow_missing_gpu_telemetry,
            ),
        )
        write_benchmark_report(args.output, report)
    except (
        OSError,
        ValueError,
        RuntimeError,
        BenchmarkError,
        QuantizationError,
        VariantArtifactError,
        LlamaCppBundleError,
        WindowsLlamaCppRuntimeError,
    ) as error:
        print(f"benchmark=failed variant={args.variant} error={error}", file=sys.stderr)
        return 2
    print(
        f"benchmark=complete variant={args.variant} samples={report['sample_count']} "
        f"p50={report['latency']['end_to_end_seconds']['p50']} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

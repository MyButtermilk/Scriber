"""Run one frozen-policy GGUF evaluation lane through pinned llama-server."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_io import (  # noqa: E402
    load_evaluation_references,
)
from scriber_polishing.generation_adapters import (  # noqa: E402
    LlamaCppServerGenerationAdapter,
)
from scriber_polishing.hf_quantization_pipeline import (  # noqa: E402
    HfQuantizationError,
    bind_llama_cpp_cuda_offload_evidence,
    verify_llama_cpp_cuda_offload,
)
from scriber_polishing.inference import (  # noqa: E402
    PredictionResult,
    validate_batch_prediction_manifest,
    write_batch_predictions,
)
from scriber_polishing.llama_cpp_bundle import (  # noqa: E402
    load_verified_llama_cpp_bundle,
)
from scriber_polishing.llama_cpp_runtime import (  # noqa: E402
    LlamaCppHttpRuntime,
)
from scriber_polishing.policy_artifact import (  # noqa: E402
    verify_frozen_lexical_policy_artifact,
)
from scriber_polishing.protected_spans import (  # noqa: E402
    protect_with_policy,
    restore_spans,
)
from scriber_polishing.quantization import (  # noqa: E402
    load_quantization_config,
    sha256_file,
)
from scriber_polishing.sst_parser import SSTParseError, parse_sst  # noqa: E402
from scriber_polishing.variant_artifacts import (  # noqa: E402
    verify_variant_artifact_manifest,
)
from scriber_polishing.windows_llama_cpp_runtime import (  # noqa: E402
    load_verified_windows_llama_cpp_runtime,
)


def _write_new_json(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite GGUF manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=("Q8_0", "Q4_K_M"),
        required=True,
    )
    parser.add_argument(
        "--suite",
        choices=("ai_gold", "critical_regression", "regular_test"),
        required=True,
    )
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--llama-cpp-bundle", type=Path)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime-manifest", type=Path)
    parser.add_argument("--predictions-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args(argv)

    root = args.model_root.resolve()
    artifact = verify_variant_artifact_manifest(root)
    if artifact["variant"] != args.variant:
        raise ValueError("GGUF artifact variant differs from the request")
    policy_binding = verify_frozen_lexical_policy_artifact(root)
    policy_path = root / policy_binding["filename"]
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("frozen lexical policy artifact must contain an object")
    toolchain = artifact["toolchain"]
    runtime_path_entries: tuple[Path, ...] = ()
    if platform.system() == "Windows":
        if (
            args.windows_llama_cpp_runtime is None
            or args.windows_llama_cpp_runtime_manifest is None
        ):
            raise ValueError(
                "Windows evaluation requires its verified llama.cpp CUDA runtime"
            )
        windows_runtime = load_verified_windows_llama_cpp_runtime(
            args.windows_llama_cpp_runtime,
            args.windows_llama_cpp_runtime_manifest,
        )
        if (
            toolchain["bundle_lock_sha256"]
            != windows_runtime.distribution_lock_sha256
            or toolchain["llama_cpp_commit"]
            != windows_runtime.conversion_commit
        ):
            raise ValueError(
                "GGUF artifact and Windows llama.cpp runtime identities differ"
            )
        llama_server = windows_runtime.llama_server
        llama_server_sha256 = windows_runtime.llama_server_sha256
        runtime_path_entries = windows_runtime.runtime_path_entries
    else:
        if args.llama_cpp_bundle is None or args.llama_cpp_bundle_lock is None:
            raise ValueError("Linux evaluation requires its verified llama.cpp bundle")
        bundle = load_verified_llama_cpp_bundle(
            args.llama_cpp_bundle,
            args.llama_cpp_bundle_lock,
        )
        if (
            toolchain["bundle_lock_sha256"] != bundle.lock_sha256
            or toolchain["llama_cpp_commit"] != bundle.commit
            or toolchain["source_tree_sha256"] != bundle.source_tree_sha256
            or toolchain["build_identity_sha256"] != bundle.build_identity_sha256
        ):
            raise ValueError("GGUF artifact and llama.cpp bundle identities differ")
        llama_server = bundle.llama_server
        llama_server_sha256 = sha256_file(bundle.llama_server)
    config = load_quantization_config(PROJECT_ROOT / "configs/quantization.yaml")
    runtime = config["llama_cpp_runtime"]
    model_path = root / f"model-{args.variant}.gguf"
    model_artifact_sha256 = sha256_file(model_path)
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        root,
        local_files_only=True,
        trust_remote_code=False,
        fix_mistral_regex=False,
    )
    original_path = os.environ.get("PATH")
    if runtime_path_entries:
        os.environ["PATH"] = os.pathsep.join(
            (*(str(path) for path in runtime_path_entries), original_path or "")
        )
    try:
        server_runtime = LlamaCppHttpRuntime(
            model_path=model_path,
            llama_server_path=llama_server,
            llama_server_sha256=llama_server_sha256,
            context_size=int(runtime["context_size"]),
            gpu_layers=999,
            startup_timeout_seconds=float(runtime["startup_timeout_seconds"]),
            request_timeout_seconds=float(runtime["request_timeout_seconds"]),
            shutdown_timeout_seconds=float(runtime["shutdown_timeout_seconds"]),
            stderr_max_bytes=int(runtime["stderr_max_bytes"]),
        )
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path
    try:
        offload_evidence = None
        deadline = time.monotonic() + min(
            5.0,
            float(runtime["startup_timeout_seconds"]),
        )
        last_error: HfQuantizationError | None = None
        while offload_evidence is None and time.monotonic() < deadline:
            try:
                offload_evidence = verify_llama_cpp_cuda_offload(
                    server_runtime.stderr_tail,
                    requested_gpu_layers=999,
                    runtime_instance_id=server_runtime.instance_id,
                )
            except HfQuantizationError as error:
                last_error = error
                time.sleep(0.05)
        if offload_evidence is None:
            raise RuntimeError(f"llama-server did not prove complete CUDA offload for its live instance: {last_error}")
        adapter = LlamaCppServerGenerationAdapter(
            model_path=model_path,
            tokenizer=tokenizer,
            runtime=server_runtime,
        )
        if adapter.runtime_instance_id != offload_evidence["runtime_instance_id"]:
            raise RuntimeError("CUDA evidence and generation adapter bind different llama-server instances")
    except BaseException:
        server_runtime.close()
        raise

    def predict(source: str) -> PredictionResult:
        protected = protect_with_policy(source, policy)
        measurement = adapter.generate(
            protected.text,
            max_new_tokens=512,
        )
        restoration_error: str | None = None
        try:
            prediction = restore_spans(protected, measurement.text)
        except ValueError as error:
            prediction = measurement.text or " "
            restoration_error = str(error)
        valid_sst = restoration_error is None
        if valid_sst:
            try:
                parse_sst(prediction)
            except (SSTParseError, ValueError):
                valid_sst = False
        return PredictionResult(
            prediction_sst=prediction,
            valid_sst=valid_sst,
            restoration_error=restoration_error,
            generation_seconds=measurement.end_to_end_seconds,
            input_tokens=measurement.input_tokens,
            output_tokens=measurement.output_tokens,
        )

    try:
        references = load_evaluation_references(args.references)
        manifest = write_batch_predictions(
            references,
            args.predictions_output,
            predictor=predict,
            candidate=args.variant,
        )
    finally:
        adapter.close()
    if sha256_file(model_path) != model_artifact_sha256:
        raise RuntimeError("GGUF model changed while llama-server was running")
    runtime_evidence = bind_llama_cpp_cuda_offload_evidence(
        offload_evidence,
        variant=args.variant,
        suite_id=args.suite,
        model_sha256=model_artifact_sha256,
        llama_server_sha256=adapter.runtime_executable_sha256,
        reference_sha256=("sha256:" + hashlib.sha256(args.references.read_bytes()).hexdigest()),
        prediction_sha256=("sha256:" + str(manifest["prediction_sha256"])),
    )
    manifest.update(
        {
            "model": str(root),
            "revision": None,
            "reference_sha256": hashlib.sha256(args.references.read_bytes()).hexdigest(),
            "quantization": args.variant,
            "device": offload_evidence["device"],
            "backend": "llama_cpp_server",
            "runtime_evidence": runtime_evidence,
            "protection_policy": {
                "source": "local_model_artifact",
                "path": str(policy_path),
                "artifact_sha256": policy_binding["artifact_sha256"].removeprefix("sha256:"),
                "policy_id": policy_binding["policy_id"],
                "policy_sha256": policy_binding["policy_sha256"],
            },
        }
    )
    manifest = validate_batch_prediction_manifest(manifest)
    _write_new_json(args.manifest_output, manifest)
    print(f"gguf_predictions=complete variant={args.variant} records={manifest['record_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

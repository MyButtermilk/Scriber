"""Build a content-free quantization decision from existing evidence only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value, "sha256:" + hashlib.sha256(payload).hexdigest()


def _variant_file(manifest: dict[str, Any], suffix: str) -> dict[str, Any]:
    matches = [item for item in manifest["artifact"]["files"] if item["path"].endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"variant manifest must contain exactly one {suffix} file")
    return matches[0]


def _validate_variant(
    *,
    expected: str,
    manifest: dict[str, Any],
    benchmark: dict[str, Any],
) -> None:
    if manifest.get("variant") != expected or benchmark.get("variant") != expected:
        raise ValueError(f"variant identity differs for {expected}")
    reload_evidence = manifest.get("reload", {})
    if (
        reload_evidence.get("status") != "passed"
        or reload_evidence.get("deterministic") is not True
        or reload_evidence.get("process_boundary") != "fresh_subprocess"
    ):
        raise ValueError(f"{expected} fresh deterministic reload evidence is incomplete")
    if benchmark.get("status") != "complete":
        raise ValueError(f"{expected} benchmark is not complete")
    if benchmark.get("sample_count") != 303:
        raise ValueError(f"{expected} benchmark must contain exactly 303 samples")
    if benchmark.get("warm_sample_count") != 300 or benchmark.get("cold_sample_count") != 3:
        raise ValueError(f"{expected} benchmark warm/cold coverage differs")
    resources = benchmark.get("resources", {})
    runtime = benchmark.get("runtime_execution", {})
    if resources.get("hardware_matches_expected") is not True:
        raise ValueError(f"{expected} benchmark hardware does not match the expected laptop")
    if runtime.get("status") != "verified" or runtime.get("full_model_on_gpu") is not True:
        raise ValueError(f"{expected} benchmark did not verify full GPU execution")
    if manifest.get("training_fingerprint") != benchmark.get("training_fingerprint"):
        raise ValueError(f"{expected} benchmark is bound to another training run")


def _round(value: float) -> float:
    return round(value, 6)


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for variant in ("bf16", "q8", "q4"):
        parser.add_argument(f"--{variant}-manifest", type=Path, required=True)
        parser.add_argument(f"--{variant}-benchmark", type=Path, required=True)
    parser.add_argument("--bf16-evaluation", type=Path, action="append", required=True)
    parser.add_argument("--q8-evaluation-root", type=Path, required=True)
    parser.add_argument("--q4-evaluation-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    if args.output_json.resolve(strict=False) == args.output_markdown.resolve(strict=False):
        raise ValueError("JSON and Markdown outputs must be distinct")

    variants: dict[str, dict[str, Any]] = {}
    input_hashes: dict[str, str] = {}
    bindings = (("bf16", "bf16"), ("q8", "Q8_0"), ("q4", "Q4_K_M"))
    for cli_name, expected in bindings:
        manifest_path: Path = getattr(args, f"{cli_name}_manifest")
        benchmark_path: Path = getattr(args, f"{cli_name}_benchmark")
        manifest, manifest_hash = _load_json(manifest_path, f"{expected} artifact manifest")
        benchmark, benchmark_hash = _load_json(benchmark_path, f"{expected} benchmark")
        _validate_variant(expected=expected, manifest=manifest, benchmark=benchmark)
        variants[expected] = {"manifest": manifest, "benchmark": benchmark}
        input_hashes[f"{expected}.artifact_manifest"] = manifest_hash
        input_hashes[f"{expected}.benchmark"] = benchmark_hash

    fingerprints = {item["manifest"]["training_fingerprint"] for item in variants.values()}
    case_sets = {item["benchmark"]["cases_sha256"] for item in variants.values()}
    if len(fingerprints) != 1 or len(case_sets) != 1:
        raise ValueError("variant evidence does not share one training run and benchmark case set")

    bf16_gates: list[dict[str, Any]] = []
    for path in args.bf16_evaluation:
        evaluation, evaluation_hash = _load_json(path, "BF16 evaluation")
        gate = evaluation.get("gate", {})
        if gate.get("passed") is not False or gate.get("status") != "failed":
            raise ValueError("BF16 evaluation must retain its failed gate state")
        if evaluation.get("exact_case_coverage") is not True:
            raise ValueError("BF16 evaluation lacks exact case coverage")
        bf16_gates.append(
            {
                "path": path.as_posix(),
                "sha256": evaluation_hash,
                "passed": False,
                "failed_check_count": len(gate.get("failed_checks", [])),
            }
        )

    q8_eval_complete = args.q8_evaluation_root.is_dir()
    q4_eval_complete = args.q4_evaluation_root.is_dir()
    if q8_eval_complete or q4_eval_complete:
        raise ValueError("quantized evaluation root unexpectedly exists; use the release matrix instead")

    q8 = variants["Q8_0"]["benchmark"]
    q4 = variants["Q4_K_M"]["benchmark"]
    q8_model = _variant_file(variants["Q8_0"]["manifest"], ".gguf")
    q4_model = _variant_file(variants["Q4_K_M"]["manifest"], ".gguf")
    size_saved = q8_model["bytes"] - q4_model["bytes"]
    throughput_gain = q4["throughput_tokens_per_second"]["p50"] / q8["throughput_tokens_per_second"]["p50"] - 1
    vram_saved = q8["resources"]["peak_vram_delta_bytes"] - q4["resources"]["peak_vram_delta_bytes"]

    report = {
        "schema_version": 1,
        "kind": "scriber_quantization_selection_report",
        "status": "PRIVATE_CANDIDATE",
        "decision": {
            "preferred_internal_desktop_variant": "Q8_0",
            "optional_low_resource_variant": "Q4_K_M",
            "quality_reference_variant": "bf16",
            "release_qualified": False,
            "reason": (
                "Q8_0 keeps the higher-precision quantization because Q4_K_M saves only "
                "a modest amount of storage, throughput, and VRAM while no quantized quality "
                "evaluation exists to prove the lower-precision quality delta."
            ),
        },
        "training_fingerprint": next(iter(fingerprints)),
        "benchmark_case_set_sha256": next(iter(case_sets)),
        "new_measurements_performed": False,
        "benchmark_scope": {
            "hardware": q8["resources"]["hardware"]["gpu_name"],
            "sample_count_per_variant": 303,
            "warm_sample_count_per_variant": 300,
            "cold_sample_count_per_variant": 3,
            "comparability_note": (
                "BF16 uses Transformers/PyTorch CUDA while Q8_0 and Q4_K_M use llama.cpp CUDA; "
                "their speed difference therefore includes runtime effects and is not an isolated "
                "quantization effect."
            ),
        },
        "variants": {},
        "q4_relative_to_q8": {
            "model_bytes_saved": size_saved,
            "model_size_reduction_rate": _round(size_saved / q8_model["bytes"]),
            "p50_throughput_gain_rate": _round(throughput_gain),
            "peak_vram_delta_bytes_saved": vram_saved,
        },
        "quality_evidence": {
            "bf16_automatic_gates": bf16_gates,
            "q8_complete_evaluation_present": q8_eval_complete,
            "q4_complete_evaluation_present": q4_eval_complete,
            "quantized_quality_delta_proven": False,
            "release_blockers": [
                "all three BF16 automatic quality gates failed",
                "Q8_0 has no complete BF16-relative quality evaluation",
                "Q4_K_M has no complete BF16-relative quality evaluation",
            ],
        },
        "input_sha256s": dict(sorted(input_hashes.items())),
    }
    for variant in ("bf16", "Q8_0", "Q4_K_M"):
        manifest = variants[variant]["manifest"]
        benchmark = variants[variant]["benchmark"]
        model_suffix = ".safetensors" if variant == "bf16" else ".gguf"
        model_file = _variant_file(manifest, model_suffix)
        report["variants"][variant] = {
            "artifact_tree_sha256": manifest["artifact"]["tree_sha256"],
            "artifact_total_bytes": manifest["artifact"]["total_bytes"],
            "model_sha256": model_file["sha256"],
            "model_size_bytes": model_file["bytes"],
            "fresh_reload_passed": True,
            "benchmark_backend": benchmark["backend"],
            "cold_start_seconds": benchmark["cold_start_seconds"],
            "warm_latency_seconds": benchmark["warm_start_seconds"],
            "throughput_tokens_per_second": benchmark["throughput_tokens_per_second"],
            "peak_vram_delta_bytes": benchmark["resources"]["peak_vram_delta_bytes"],
        }

    json_payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    lines = [
        "# Quantisierungsentscheidung",
        "",
        "**Status:** `PRIVATE_CANDIDATE` – keine Variante ist release-qualifiziert.",
        "",
        "- Bevorzugte interne Desktop-/Laptop-Variante: `Q8_0`.",
        "- Optionale Sparvariante: `Q4_K_M`.",
        "- Qualitätsreferenz: `bf16`.",
        f"- Q4 spart gegenüber Q8 {size_saved:,} Modellbytes ({size_saved / q8_model['bytes']:.2%}).",
        f"- Q4 erzielt im Median {throughput_gain:.2%} mehr Tokens/s und spart {vram_saved / 1048576:.2f} MiB Peak-VRAM-Delta.",
        "- Beide GGUF-Artefakte bestanden den deterministischen Reload in frischen Prozessen.",
        "- Für Q8 und Q4 liegt keine vollständige BF16-relative Qualitätsmessung vor.",
        "- Alle drei automatischen BF16-Qualitätsgates sind fehlgeschlagen.",
        "",
        "Die Geschwindigkeitswerte von BF16 und GGUF sind nicht als reine Quantisierungseffekte "
        "zu lesen, weil unterschiedliche CUDA-Runtimes verwendet wurden.",
    ]
    _write_new(args.output_json, json_payload)
    _write_new(args.output_markdown, ("\n".join(lines) + "\n").encode("utf-8"))
    print("quantization_selection=complete preferred=Q8_0 optional=Q4_K_M release_qualified=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

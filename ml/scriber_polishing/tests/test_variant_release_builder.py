from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.benchmark import build_runtime_identity, write_benchmark_report
from scriber_polishing.evaluation_bundle import EvaluationPackagePart
from scriber_polishing.policy_artifact import frozen_lexical_policy_binding
from scriber_polishing.quantization import load_quantization_config
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text
from scriber_polishing.variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    build_variant_artifact_manifest,
)
from scriber_polishing.variant_release import (
    collect_variant_release_records,
    verify_variant_release_records,
)
from scriber_polishing.variant_release_builder import (
    VariantReleaseBuildError,
    build_private_candidate_release_matrix,
    build_variant_release_matrix,
    validate_variant_release_matrix_input,
    validate_variant_release_reload_report,
)

_VARIANTS = ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")
_SUITES = ("ai_gold", "critical_regression", "regular_test")
_SUITE_COUNTS = {
    "ai_gold": 1750,
    "critical_regression": 500,
    "regular_test": 1000,
}
_CATEGORIES = (
    "short_email",
    "long_business_letter",
    "tax_law_letter",
    "multipart_letter",
    "numbered_list",
    "number_heavy",
    "unit_heavy",
    "legal_citations",
    "address_pronouns",
    "rich_text_formatting",
    "identity_text",
    "hard_repetition",
)
_FINGERPRINT = "sha256:" + "a" * 64
_SOURCE_TREE = "sha256:" + "b" * 64


def _json_bytes(value: object, *, pretty: bool = True) -> bytes:
    options: dict[str, object] = {
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _hash(path: Path, *, prefixed: bool = False) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}" if prefixed else digest


def _reference_template() -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": "placeholder",
        "split": "test",
        "source_text": "bitte bis 31.07.2026 antworten",
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Eine Antwort ist erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Frist endet am 31.07.2026.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["31.07.2026"],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Antwortfrist"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": ["31.07.2026"],
        "slice_tags": ["release_builder"],
        "is_identity": False,
    }


def _reference_rows(suite: str) -> list[dict[str, object]]:
    template = _reference_template()
    split = "challenge" if suite == "critical_regression" else "test"
    rows: list[dict[str, object]] = []
    for index in range(_SUITE_COUNTS[suite]):
        row = dict(template)
        row["case_id"] = f"{suite}_{index:04d}"
        row["split"] = split
        row["slice_tags"] = [f"suite:{suite}"]
        if suite == "regular_test" and index == 0:
            row["is_identity"] = True
            row["source_text"] = row["target_plain_text"]
        rows.append(row)
    return rows


def _write_reference_package(root: Path, suite: str) -> tuple[Path, Path, list[dict[str, object]]]:
    rows = _reference_rows(suite)
    records = root / f"{suite}.references.jsonl"
    manifest = root / f"{suite}.references.manifest.json"
    records.write_bytes(b"".join(_json_bytes(row, pretty=False) for row in rows))
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "record_count": len(rows),
            "split_counts": {
                ("challenge" if suite == "critical_regression" else "test"): len(rows)
            },
            "records_sha256": _hash(records),
            "output_filename": records.name,
        },
    )
    return records, manifest, rows


def _write_prediction_package(
    root: Path,
    *,
    variant: str,
    suite: str,
    rows: list[dict[str, object]],
) -> tuple[Path, Path]:
    lane = root / "evaluation" / variant / suite
    lane.mkdir(parents=True, exist_ok=True)
    records = lane / "predictions.jsonl"
    manifest = lane / "predictions.manifest.json"
    payload = b"".join(
        _json_bytes(
            {
                "schema_version": 1,
                "case_id": row["case_id"],
                "prediction_sst": row["target_sst"],
            },
            pretty=False,
        )
        for row in rows
    )
    records.write_bytes(payload)
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "candidate": variant,
            "record_count": len(rows),
            "valid_sst_count": len(rows),
            "restoration_error_count": 0,
            "prediction_sha256": hashlib.sha256(payload).hexdigest(),
            "total_generation_seconds": 1.0,
            "total_input_tokens": len(rows) * 8,
            "total_output_tokens": len(rows) * 10,
            "batch_size": 8,
        },
    )
    return records, manifest


def _quantization(variant: str) -> dict[str, object]:
    if variant == "bf16":
        return {"quant_method": "none", "weight_dtype": "BF16"}
    if variant == "int8":
        return {"quant_method": "bitsandbytes", "load_in_8bit": True}
    if variant == "int4_nf4":
        return {
            "quant_method": "bitsandbytes",
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_compute_dtype": "bfloat16",
            "bnb_4bit_use_double_quant": True,
        }
    return {"quant_method": "llama_cpp", "quant_type": variant}


def _toolchain(variant: str) -> dict[str, object]:
    if variant in {"Q8_0", "Q4_K_M"}:
        return {
            "kind": "llama_cpp_bundle",
            "bundle_lock_sha256": "sha256:" + "1" * 64,
            "llama_cpp_commit": "2" * 40,
            "source_tree_sha256": "sha256:" + "3" * 64,
            "build_identity_sha256": "sha256:" + "4" * 64,
        }
    return {
        "kind": (
            "transformers_training"
            if variant == "bf16"
            else "transformers_bitsandbytes"
        ),
        "packages": {
            "bitsandbytes": "0.49.2",
            "torch": "2.11.0",
            "transformers": "4.57.6",
        },
    }


def _artifact_root(root: Path, variant: str) -> Path:
    if variant in {"Q8_0", "Q4_K_M"}:
        return root / "variants" / "gguf" / variant
    return root / "variants" / variant


def _build_artifact(root: Path, variant: str, policy_source: Path) -> dict[str, object]:
    artifact = _artifact_root(root, variant)
    artifact.mkdir(parents=True)
    _write_json(
        artifact / "config.json",
        {
            "architectures": ["Gemma3ForCausalLM"],
            "model_type": "gemma3_text",
            "scriber_protection_policy_id": "gemma_lexical_v1",
            "scriber_protection_policy_sha256": (
                "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
            ),
        },
    )
    _write_json(artifact / "tokenizer.json", {"version": "1.0"})
    _write_json(
        artifact / "tokenizer_config.json",
        {"chat_template": "{{ messages | tojson }}"},
    )
    (artifact / ("model.gguf" if variant in {"Q8_0", "Q4_K_M"} else "model.safetensors")).write_bytes(
        f"weights-{variant}".encode()
    )
    shutil.copyfile(policy_source, artifact / "scriber-protection-policy.json")
    reload_evidence = {
        "status": "passed",
        "process_boundary": "fresh_subprocess",
        "configuration_source": "saved_artifact",
        "loaded_quantization": variant,
        "deterministic": True,
        "valid_sst": True,
        "generated_tokens": 12,
        "protection_policy": frozen_lexical_policy_binding(),
    }
    return build_variant_artifact_manifest(
        artifact,
        variant=variant,
        backend=(
            "llama_cpp"
            if variant in {"Q8_0", "Q4_K_M"}
            else ("transformers" if variant == "bf16" else "transformers_bitsandbytes")
        ),
        source_id="scriber-1100-test",
        source_tree_sha256=_SOURCE_TREE,
        training_fingerprint=_FINGERPRINT,
        quantization=_quantization(variant),
        toolchain=_toolchain(variant),
        reload_evidence=reload_evidence,
    )


def _percentiles(value: float) -> dict[str, float]:
    return {"p50": value, "p95": value, "p99": value}


def _latency() -> dict[str, dict[str, float]]:
    return {
        "tokenization_seconds": _percentiles(0.001),
        "model_load_seconds": _percentiles(0.01),
        "prefill_seconds": _percentiles(0.01),
        "time_to_first_token_seconds": _percentiles(0.02),
        "decode_seconds": _percentiles(0.03),
        "end_to_end_seconds": _percentiles(0.05),
    }


def _summary() -> dict[str, object]:
    return {
        "sample_count": 1,
        "latency": _latency(),
        "throughput_tokens_per_second": _percentiles(100.0),
    }


def _sample(index: int) -> dict[str, object]:
    cold = index < 3
    return {
        "case_id": f"benchmark_case_{index}",
        "category": _CATEGORIES[index],
        "token_bucket": (50, 150, 300, 500)[index],
        "repetition": 1,
        "cold_start": cold,
        "phase": "cold_load" if cold else "warm_runtime",
        "adapter_load_seconds": 0.01 if cold else 0.0,
        "input_tokens": 10,
        "output_tokens": 10,
        "tokenization_seconds": 0.001,
        "model_load_seconds": 0.01 if cold else 0.0,
        "prefill_seconds": 0.01,
        "time_to_first_token_seconds": 0.02,
        "decode_seconds": 0.03,
        "end_to_end_seconds": 0.05,
        "tokens_per_second": 100.0,
    }


def _benchmark_report(
    variant: str,
    artifact: dict[str, object],
    quant_root: Path,
) -> dict[str, object]:
    quantization = artifact["quantization"]
    toolchain_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            artifact["toolchain"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime_identity = build_runtime_identity(
        kind="test_windows_cuda",
        platform_name="windows_x86_64",
        details={"runtime": "fixture-v4"},
    )
    return {
        "schema_version": 4,
        "status": "complete",
        "variant": variant,
        "backend": (
            "llama_cpp_server"
            if variant in {"Q8_0", "Q4_K_M"}
            else "transformers"
        ),
        "config_sha256": "sha256:" + "5" * 64,
        "cases_sha256": "sha256:" + "6" * 64,
        "artifact_manifest_sha256": _hash(
            _artifact_root(quant_root, variant) / VARIANT_MANIFEST_FILENAME,
            prefixed=True,
        ),
        "artifact_tree_sha256": artifact["artifact"]["tree_sha256"],
        "training_fingerprint": _FINGERPRINT,
        "artifact_identity": {
            "variant": variant,
            "backend": artifact["backend"],
            "quantization_config": quantization["config"],
            "quantization_config_sha256": quantization["config_sha256"],
            "toolchain_sha256": toolchain_hash,
        },
        "runtime_identity": runtime_identity,
        "runtime_execution": {
            "schema_version": 1,
            "status": "verified",
            "backend": (
                "llama_cpp_cuda"
                if variant in {"Q8_0", "Q4_K_M"}
                else "transformers_cuda"
            ),
            "device": "cuda",
            "loaded_variant": variant,
            "full_model_on_gpu": True,
            "evidence_sha256": "sha256:" + "7" * 64,
        },
        "artifact_size_bytes": artifact["artifact"]["total_bytes"],
        "model_size_bytes": 12,
        "measurement_definitions": {
            "model_load": "adapter_factory wall time through successful runtime health",
            "cold_start": "fresh model load plus first end-to-end generation on that runtime",
            "warm_start": "end-to-end generation after one excluded warmup on the same persistent runtime",
            "prefill": "model-reported prompt evaluation time excluding tokenization",
            "time_to_first_token": "tokenization plus request wall time through first non-empty completion token",
            "tokens_per_second": "model-reported decoded completion tokens per decode second",
            "end_to_end": "tokenization plus complete local generation wall time",
            "percentiles": "nearest-rank p50/p95/p99 over warm measured samples unless named cold",
            "resources": "peak process-tree RAM and physical-device VRAM plus sampled CPU/GPU utilization",
            "size": "model_size_bytes is loaded weight file/tree; artifact_size_bytes is manifest-bound package bytes",
        },
        "adapter_load_seconds": _percentiles(0.01),
        "warm_adapter_load_seconds": 0.01,
        "warmup_seconds": 0.01,
        "cold_start_seconds": _percentiles(0.06),
        "warm_start_seconds": _percentiles(0.05),
        "sample_count": 4,
        "cold_sample_count": 3,
        "warm_sample_count": 1,
        "runtime_protocol": {
            "cold_loads": {
                "adapter_instances": 3,
                "repetitions": 3,
                "case_id": "benchmark_case_0",
            },
            "warm_runtime": {
                "adapter_instances": 1,
                "persistent_model_process": True,
                "repetitions_per_case": 3,
                "warmup_repetitions": 1,
                "health_before_warmup": True,
                "health_after_measurement": True,
                "instance_identity_stable": True,
                "runtime_protocol": (
                    "llama_server_http_sse_v1"
                    if variant in {"Q8_0", "Q4_K_M"}
                    else "in_process_transformers_v1"
                ),
                "runtime_executable_sha256": "sha256:" + "8" * 64,
            },
        },
        "latency": _latency(),
        "throughput_tokens_per_second": _percentiles(100.0),
        "by_token_bucket": {
            str(bucket): _summary()
            for bucket in (50, 150, 300, 500, 1000)
        },
        "by_case_category": {category: _summary() for category in _CATEGORIES},
        "resources": {
            "telemetry_sample_count": 2,
            "peak_ram_bytes": 2_000_000,
            "peak_vram_bytes": 3_000_000,
            "baseline_vram_bytes": 1_000_000,
            "peak_vram_delta_bytes": 2_000_000,
            "peak_process_count": 1,
            "cpu_percent": _percentiles(10.0),
            "gpu_percent": _percentiles(50.0),
            "telemetry_scope": {
                "cpu_memory": "root_process_tree",
                "gpu": "physical_device",
            },
            "hardware": {
                "os": "Windows-11-test",
                "python": "3.12.0",
                "cpu_name": "Test CPU",
                "logical_cpu_count": 16,
                "physical_cpu_count": 8,
                "total_ram_bytes": 32_000_000_000,
                "gpu_name": "NVIDIA GeForce RTX 4070 Laptop GPU",
                "gpu_uuid": "GPU-test",
                "gpu_driver_version": "591.86",
                "gpu_total_memory_bytes": 8_188 * 1024 * 1024,
                "gpu_compute_capability": "8.9",
                "torch_bf16_supported": True,
            },
            "hardware_matches_expected": True,
            "hardware_checks": {
                "os_family": True,
                "gpu_name": True,
                "gpu_total_memory": True,
                "gpu_driver_version": True,
                "gpu_compute_capability": True,
                "bf16_support": True,
                "gpu_activity": True,
                "vram_growth": True,
            },
        },
        "samples": [_sample(index) for index in range(4)],
    }


def test_builder_materializes_all_five_complete_3250_case_records_for_existing_apis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).parents[1]
    quant_root = tmp_path / "quantization"
    reference_root = tmp_path / "references"
    benchmark_root = tmp_path / "benchmarks"
    reference_root.mkdir()
    benchmark_root.mkdir()
    suite_inputs: dict[str, tuple[Path, Path, list[dict[str, object]]]] = {
        suite: _write_reference_package(reference_root, suite)
        for suite in _SUITES
    }
    prediction_hashes: dict[str, dict[str, str]] = {}
    for variant in _VARIANTS:
        prediction_hashes[variant] = {}
        for suite in _SUITES:
            records, _ = _write_prediction_package(
                quant_root,
                variant=variant,
                suite=suite,
                rows=suite_inputs[suite][2],
            )
            prediction_hashes[variant][suite] = _hash(records, prefixed=True)
    plan = {
        "references": {
            suite: {
                "records_filename": suite_inputs[suite][0].name,
                "records_sha256": _hash(suite_inputs[suite][0]),
                "manifest_filename": suite_inputs[suite][1].name,
                "manifest_sha256": _hash(suite_inputs[suite][1]),
                "record_count": _SUITE_COUNTS[suite],
            }
            for suite in _SUITES
        },
        "evaluation": {
            "config_sha256": _hash(project_root / "configs" / "evaluation.yaml"),
        },
    }
    result = {
        "variants": {
            variant: {
                "evaluation": {
                    suite: {
                        "prediction_sha256": prediction_hashes[variant][suite],
                    }
                    for suite in _SUITES
                }
            }
            for variant in _VARIANTS
        }
    }
    _write_json(quant_root / "hf-quantization-plan.json", plan)
    _write_json(quant_root / "hf-quantization-result.json", result)
    policy_source = (
        project_root / "artifacts" / "protection" / "gemma_lexical_v1.json"
    )
    artifacts = {
        variant: _build_artifact(quant_root, variant, policy_source)
        for variant in _VARIANTS
    }
    benchmarks: dict[str, Path] = {}
    for variant in _VARIANTS:
        benchmark = benchmark_root / f"{variant}.benchmark.json"
        write_benchmark_report(
            benchmark,
            _benchmark_report(variant, artifacts[variant], quant_root),
        )
        benchmarks[variant] = benchmark

    from scriber_polishing import variant_release_builder

    monkeypatch.setattr(
        variant_release_builder,
        "validate_hf_quantization_plan",
        lambda value: dict(value),
    )
    monkeypatch.setattr(
        variant_release_builder,
        "verify_hf_quantization_result_file",
        lambda **_kwargs: result,
    )
    output = tmp_path / "release-matrix"
    build = build_variant_release_matrix(
        quantization_root=quant_root,
        reference_root=reference_root,
        benchmark_reports=benchmarks,
        quantization_config_path=project_root / "configs" / "quantization.yaml",
        evaluation_config_path=project_root / "configs" / "evaluation.yaml",
        output_dir=output,
    )

    assert validate_variant_release_matrix_input(build.matrix) == build.matrix
    assert build.matrix["coverage"]["actual_case_count"] == 3250
    assert build.matrix["coverage"]["suite_order"] == list(_SUITES)
    assert build.verified_report["status"] == "RELEASE_MATRIX_PASSED"
    assert build.verified_report["variants"] == list(_VARIANTS)
    for variant in _VARIANTS:
        variant_root = output / "variants" / variant
        prediction_manifest = json.loads(
            (variant_root / "predictions.manifest.json").read_text(encoding="utf-8")
        )
        reload_report = json.loads(
            (variant_root / "reload-report.json").read_text(encoding="utf-8")
        )
        assert prediction_manifest["record_count"] == 3250
        assert prediction_manifest["inputs"][0]["record_count"] == 1750
        assert prediction_manifest["inputs"][1]["record_count"] == 500
        assert prediction_manifest["inputs"][2]["record_count"] == 1000
        assert validate_variant_release_reload_report(reload_report) == reload_report

    config = load_quantization_config(project_root / "configs" / "quantization.yaml")
    records = collect_variant_release_records(build.matrix_path, config)
    independently_verified = verify_variant_release_records(config, records)
    assert independently_verified == build.verified_report
    assert all(
        summary["prediction_count"] == 3250
        for summary in independently_verified["variant_summaries"].values()
    )

    for suite in _SUITES:
        bf16_manifest_path = (
            quant_root
            / "evaluation"
            / "bf16"
            / suite
            / "predictions.manifest.json"
        )
        bf16_manifest = json.loads(
            bf16_manifest_path.read_text(encoding="utf-8")
        )
        bf16_manifest["candidate"] = "final"
        _write_json(bf16_manifest_path, bf16_manifest)

    failure_root = tmp_path / "failures"
    int8_failure = failure_root / "int8.json"
    int4_failure = failure_root / "int4_nf4.json"
    _write_json(int8_failure, {"status": "failed", "reason": "invalid_sst"})
    _write_json(
        int4_failure,
        {"status": "failed", "reason": "empty_completion"},
    )
    candidate = build_private_candidate_release_matrix(
        reference_packages={
            suite: EvaluationPackagePart(
                suite_inputs[suite][0],
                suite_inputs[suite][1],
            )
            for suite in _SUITES
        },
        variant_sources={
            variant: {
                "artifact_root": _artifact_root(quant_root, variant),
                "evaluation_root": quant_root / "evaluation" / variant,
                "benchmark_report": benchmarks[variant],
            }
            for variant in ("bf16", "Q8_0", "Q4_K_M")
        },
        ineligible_variants={
            "int8": {
                "stage": "reload",
                "reason_code": "invalid_sst",
                "evidence_path": int8_failure,
            },
            "int4_nf4": {
                "stage": "reload",
                "reason_code": "empty_completion",
                "evidence_path": int4_failure,
            },
        },
        quantization_config_path=project_root / "configs" / "quantization.yaml",
        evaluation_config_path=project_root / "configs" / "evaluation.yaml",
        output_dir=tmp_path / "private-candidate-matrix",
    )
    assert candidate.matrix["schema_version"] == 2
    assert candidate.verified_report["status"] == "PRIVATE_CANDIDATE_MATRIX"
    assert candidate.verified_report["variants"] == ["bf16", "Q8_0", "Q4_K_M"]
    assert set(candidate.verified_report["ineligible_variants"]) == {
        "int8",
        "int4_nf4",
    }
    combined_bf16_manifest = json.loads(
        (
            candidate.output_dir
            / "variants"
            / "bf16"
            / "predictions.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert combined_bf16_manifest["candidate"] == "bf16"

    duplicate_rows = reference_root / "critical_regression.references.jsonl"
    lines = duplicate_rows.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace(
        '"case_id":"critical_regression_0000"',
        '"case_id":"ai_gold_0000"',
    )
    duplicate_rows.write_bytes(("\n".join(lines) + "\n").encode())
    duplicate_manifest = reference_root / "critical_regression.references.manifest.json"
    duplicate_manifest_value = json.loads(
        duplicate_manifest.read_text(encoding="utf-8")
    )
    duplicate_manifest_value["records_sha256"] = _hash(duplicate_rows)
    _write_json(duplicate_manifest, duplicate_manifest_value)
    plan["references"]["critical_regression"]["records_sha256"] = _hash(
        duplicate_rows
    )
    plan["references"]["critical_regression"]["manifest_sha256"] = _hash(
        duplicate_manifest
    )
    _write_json(quant_root / "hf-quantization-plan.json", plan)
    with pytest.raises(
        VariantReleaseBuildError,
        match="duplicate reference case_id",
    ):
        build_variant_release_matrix(
            quantization_root=quant_root,
            reference_root=reference_root,
            benchmark_reports=benchmarks,
            quantization_config_path=project_root / "configs" / "quantization.yaml",
            evaluation_config_path=project_root / "configs" / "evaluation.yaml",
            output_dir=tmp_path / "duplicate-matrix",
        )

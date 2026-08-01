from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scriber_polishing.quantization import load_quantization_config
from scriber_polishing.variant_release import (
    VariantReleaseError,
    derive_qualification_failures,
    verify_variant_release_records,
)

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_FINGERPRINT = "sha256:" + "d" * 64
_POLICY_BINDING = {
    "filename": "scriber-protection-policy.json",
    "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
    "policy_id": "gemma_lexical_v1",
    "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
    "tokenizer_identity_sha256": ("sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"),
}


def _record(variant: str) -> dict[str, object]:
    return {
        "artifact": {
            "variant": variant,
            "manifest_sha256": _HASH_A,
            "artifact_tree_sha256": _HASH_B,
            "training_fingerprint": _FINGERPRINT,
            "quantization_config_sha256": _HASH_C,
            "toolchain_sha256": _HASH_A,
            "protection_policy": dict(_POLICY_BINDING),
        },
        "reload": {
            "status": "passed",
            "variant": variant,
            "artifact_tree_sha256": _HASH_B,
            "training_fingerprint": _FINGERPRINT,
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "valid_sst": True,
            "deterministic": True,
            "report_sha256": _HASH_B,
            "protection_policy": dict(_POLICY_BINDING),
        },
        "predictions": {
            "status": "complete",
            "variant": variant,
            "artifact_tree_sha256": _HASH_B,
            "reference_count": 4000,
            "prediction_count": 4000,
            "exact_case_coverage": True,
            "predictions_sha256": _HASH_C,
        },
        "evaluation": {
            "status": "complete",
            "candidate": variant,
            "passed": True,
            "exact_case_coverage": True,
            "predictions_sha256": _HASH_C,
            "report_sha256": _HASH_A,
        },
        "delta": {
            "status": "complete",
            "baseline_variant": "bf16",
            "candidate_variant": variant,
            "maximum_absolute_metric_drop": 0.0,
            "new_critical_errors": 0,
            "baseline_evaluation_sha256": _HASH_A,
            "candidate_evaluation_sha256": _HASH_A,
            "report_sha256": _HASH_C,
        },
        "benchmark": {
            "schema_version": 4,
            "status": "complete",
            "variant": variant,
            "artifact_manifest_sha256": _HASH_A,
            "artifact_tree_sha256": _HASH_B,
            "training_fingerprint": _FINGERPRINT,
            "artifact_identity": {
                "variant": variant,
                "quantization_config_sha256": _HASH_C,
                "toolchain_sha256": _HASH_A,
            },
            "cold_sample_count": 3,
            "warm_sample_count": 300,
            "runtime_protocol": {
                "cold_loads": {"adapter_instances": 3, "repetitions": 3},
                "warm_runtime": {
                    "adapter_instances": 1,
                    "persistent_model_process": True,
                    "repetitions_per_case": 5,
                },
            },
            "hardware_identity_complete": True,
            "report_sha256": _HASH_B,
        },
        "evidence_hashes": {
            "artifact_manifest": _HASH_A,
            "reload_report": _HASH_B,
            "predictions": _HASH_C,
            "evaluation_report": _HASH_A,
            "delta_report": _HASH_C,
            "benchmark_report": _HASH_B,
        },
    }


def _records() -> dict[str, dict[str, object]]:
    return {variant: _record(variant) for variant in ("bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M")}


def test_release_matrix_requires_every_enabled_variant_and_every_evidence_kind() -> None:
    config = load_quantization_config(Path(__file__).parents[1] / "configs" / "quantization.yaml")

    report = verify_variant_release_records(config, _records())

    assert report["status"] == "RELEASE_MATRIX_PASSED"
    assert report["passed"] is True
    assert report["protection_policy"] == _POLICY_BINDING
    assert report["variants"] == ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"]
    assert all(
        set(summary["evidence"])
        == {
            "artifact",
            "reload",
            "predictions",
            "evaluation",
            "delta",
            "benchmark",
        }
        for summary in report["variant_summaries"].values()
    )

    missing = _records()
    del missing["Q4_K_M"]["benchmark"]
    with pytest.raises(VariantReleaseError, match="Q4_K_M.*benchmark"):
        verify_variant_release_records(config, missing)


def test_release_matrix_recomputes_delta_budget_and_rejects_nonpersistent_warm_runtime() -> None:
    config = load_quantization_config(Path(__file__).parents[1] / "configs" / "quantization.yaml")
    records = _records()
    records["int4_nf4"]["delta"]["maximum_absolute_metric_drop"] = 0.011
    with pytest.raises(VariantReleaseError, match="delta budget"):
        verify_variant_release_records(config, records)

    records = _records()
    records["Q8_0"]["benchmark"]["runtime_protocol"]["warm_runtime"]["persistent_model_process"] = False
    with pytest.raises(VariantReleaseError, match="persistent warm runtime"):
        verify_variant_release_records(config, records)


def test_release_matrix_rejects_cross_variant_or_training_fingerprint_substitution() -> None:
    config = load_quantization_config(Path(__file__).parents[1] / "configs" / "quantization.yaml")
    records = deepcopy(_records())
    records["int8"]["artifact"]["variant"] = "int4_nf4"
    with pytest.raises(VariantReleaseError, match="int8.*artifact variant"):
        verify_variant_release_records(config, records)

    records = _records()
    records["Q4_K_M"]["reload"]["training_fingerprint"] = "sha256:" + "e" * 64
    with pytest.raises(VariantReleaseError, match="training fingerprint"):
        verify_variant_release_records(config, records)

    records = _records()
    records["Q8_0"]["evidence_hashes"]["benchmark_report"] = "sha256:" + "9" * 64
    with pytest.raises(VariantReleaseError, match="benchmark report hash"):
        verify_variant_release_records(config, records)

    records = _records()
    records["Q8_0"]["reload"]["protection_policy"]["artifact_sha256"] = "sha256:" + ("0" * 64)
    with pytest.raises(VariantReleaseError, match="protection policy"):
        verify_variant_release_records(config, records)


def test_release_matrix_accepts_only_current_benchmark_schema_v4() -> None:
    config = load_quantization_config(Path(__file__).parents[1] / "configs" / "quantization.yaml")

    assert verify_variant_release_records(config, _records())["passed"] is True

    stale = _records()
    stale["bf16"]["benchmark"]["schema_version"] = 3
    with pytest.raises(VariantReleaseError, match="benchmark evidence is incomplete"):
        verify_variant_release_records(config, stale)


def test_release_matrix_binds_tested_reload_failures_for_private_candidate() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    del records["int8"]
    del records["int4_nf4"]
    failures = {
        "int8": {
            "stage": "reload",
            "reason_code": "invalid_sst",
            "evidence_sha256": _HASH_A,
        },
        "int4_nf4": {
            "stage": "reload",
            "reason_code": "empty_completion",
            "evidence_sha256": _HASH_B,
        },
    }

    report = verify_variant_release_records(
        config,
        records,
        ineligible_variants=failures,
    )

    assert report["schema_version"] == 4
    assert report["status"] == "PRIVATE_CANDIDATE_MATRIX"
    assert report["passed"] is False
    assert report["tested_variants"] == [
        "bf16",
        "int8",
        "int4_nf4",
        "Q8_0",
        "Q4_K_M",
    ]
    assert report["variants"] == ["bf16", "Q8_0", "Q4_K_M"]
    assert report["ineligible_variants"] == failures
    assert report["qualification_failures"] == {}
    assert report["reasons"] == [
        "int8:reload:invalid_sst",
        "int4_nf4:reload:empty_completion",
    ]


def test_private_candidate_matrix_rejects_unbound_or_overlapping_failures() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    with pytest.raises(VariantReleaseError, match="both eligible and ineligible"):
        verify_variant_release_records(
            config,
            records,
            ineligible_variants={
                "int8": {
                    "stage": "reload",
                    "reason_code": "invalid_sst",
                    "evidence_sha256": _HASH_A,
                }
            },
        )


def test_private_candidate_matrix_binds_failed_quality_gate_to_real_evidence() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    records["bf16"]["evaluation"]["passed"] = False
    failures = {
        "bf16": {
            "stage": "evaluation",
            "reason_code": "hard_quality_gates_failed",
            "evidence_sha256": _HASH_A,
        }
    }

    report = verify_variant_release_records(
        config,
        records,
        qualification_failures=failures,
    )

    assert report["schema_version"] == 4
    assert report["status"] == "PRIVATE_CANDIDATE_MATRIX"
    assert report["passed"] is False
    assert report["variants"] == ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"]
    assert report["ineligible_variants"] == {}
    assert report["qualification_failures"] == failures
    assert report["reasons"] == [
        "bf16:qualification:evaluation:hard_quality_gates_failed"
    ]


def test_private_candidate_matrix_rejects_unbound_quality_failure() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    records["bf16"]["evaluation"]["passed"] = False
    with pytest.raises(VariantReleaseError, match="not bound"):
        verify_variant_release_records(config, records)

    with pytest.raises(VariantReleaseError, match="evidence hash"):
        verify_variant_release_records(
            config,
            records,
            qualification_failures={
                "bf16": {
                    "stage": "evaluation",
                    "reason_code": "hard_quality_gates_failed",
                    "evidence_sha256": "missing",
                }
            },
        )

    passed = _records()
    with pytest.raises(VariantReleaseError, match="although the gate passed"):
        verify_variant_release_records(
            config,
            passed,
            qualification_failures={
                "bf16": {
                    "stage": "evaluation",
                    "reason_code": "hard_quality_gates_failed",
                    "evidence_sha256": _HASH_B,
                }
            },
        )


def test_evaluation_failure_binding_also_exposes_delta_regression() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    records["bf16"]["evaluation"]["passed"] = False
    records["bf16"]["delta"]["maximum_absolute_metric_drop"] = 0.25

    report = verify_variant_release_records(
        config,
        records,
        qualification_failures={
            "bf16": {
                "stage": "evaluation",
                "reason_code": "hard_quality_gates_failed",
                "evidence_sha256": _HASH_A,
            }
        },
    )

    assert report["variant_summaries"]["bf16"][
        "maximum_absolute_metric_drop"
    ] == 0.25


def test_qualification_failures_are_derived_from_bound_records() -> None:
    config = load_quantization_config(
        Path(__file__).parents[1] / "configs" / "quantization.yaml"
    )
    records = _records()
    records["bf16"]["evaluation"]["passed"] = False
    records["Q8_0"]["delta"]["maximum_absolute_metric_drop"] = 0.02

    assert derive_qualification_failures(config, records) == {
        "bf16": {
            "stage": "evaluation",
            "reason_code": "hard_quality_gates_failed",
            "evidence_sha256": _HASH_A,
        },
        "Q8_0": {
            "stage": "delta",
            "reason_code": "quality_delta_budget_exceeded",
            "evidence_sha256": _HASH_C,
        },
    }

    del records["int8"]
    with pytest.raises(VariantReleaseError, match="evidence hash"):
        verify_variant_release_records(
            config,
            records,
            ineligible_variants={
                "int8": {
                    "stage": "reload",
                    "reason_code": "invalid_sst",
                    "evidence_sha256": "missing",
                }
            },
        )

from __future__ import annotations

import pytest

from scriber_polishing.quality_delta import (
    QualityDeltaError,
    build_quality_delta_report,
    verify_quality_delta_report,
)

_BASELINE_HASH = "sha256:" + "a" * 64
_CANDIDATE_HASH = "sha256:" + "b" * 64


def _evaluation(
    candidate: str,
    punctuation_f1: float,
    critical: int,
    *,
    critical_case_id: str = "critical-case",
) -> dict[str, object]:
    return {
        "candidate": candidate,
        "exact_case_coverage": True,
        "gate": {"passed": True},
        "metrics": {
            "case_results": [
                {
                    "case_id": case_id,
                    "critical_errors": (
                        ["changed_number"]
                        if critical and case_id == critical_case_id
                        else []
                    ),
                }
                for case_id in (
                    "baseline-only-case",
                    "candidate-only-case",
                    "critical-case",
                )
            ],
            "slice_reports": {
                "all": {
                    "total": 4000,
                    "sst_parse_rate": 1.0,
                    "punctuation_f1": punctuation_f1,
                    "hard_content_exact_rates": {
                        "numbers": 1.0,
                        "units": 1.0,
                    },
                    "critical_errors": {"changed_number": critical},
                },
                "split:challenge": {
                    "total": 1000,
                    "sst_parse_rate": 1.0,
                    "punctuation_f1": punctuation_f1,
                    "hard_content_exact_rates": {
                        "numbers": 1.0,
                        "units": 1.0,
                    },
                    "critical_errors": {"changed_number": critical},
                },
            }
        },
    }


def test_quality_delta_recomputes_metric_drop_and_new_critical_errors() -> None:
    report = build_quality_delta_report(
        _evaluation("bf16", 0.98, 0),
        _evaluation("int4_nf4", 0.971, 1),
        baseline_sha256=_BASELINE_HASH,
        candidate_sha256=_CANDIDATE_HASH,
        candidate_variant="int4_nf4",
    )

    assert report["status"] == "complete"
    assert report["baseline_variant"] == "bf16"
    assert report["candidate_variant"] == "int4_nf4"
    assert report["maximum_absolute_metric_drop"] == pytest.approx(0.009)
    assert report["new_critical_errors"] == 1
    assert report["new_critical_error_instances"] == [
        {
            "case_id": "critical-case",
            "error": "changed_number",
        }
    ]
    assert report["baseline_evaluation_sha256"] == _BASELINE_HASH
    assert report["candidate_evaluation_sha256"] == _CANDIDATE_HASH
    assert any(
        row["metric"] == "split:challenge.punctuation_f1"
        for row in report["metric_deltas"]
    )


def test_quality_delta_requires_exact_same_complete_metric_surface() -> None:
    candidate = _evaluation("Q8_0", 0.98, 0)
    del candidate["metrics"]["slice_reports"]["all"]["hard_content_exact_rates"]["units"]

    with pytest.raises(QualityDeltaError, match="metric surface"):
        build_quality_delta_report(
            _evaluation("bf16", 0.98, 0),
            candidate,
            baseline_sha256=_BASELINE_HASH,
            candidate_sha256=_CANDIDATE_HASH,
            candidate_variant="Q8_0",
        )


def test_quality_delta_detects_new_case_error_when_aggregate_count_is_unchanged() -> None:
    report = build_quality_delta_report(
        _evaluation(
            "bf16",
            0.98,
            1,
            critical_case_id="baseline-only-case",
        ),
        _evaluation(
            "Q4_K_M",
            0.98,
            1,
            critical_case_id="candidate-only-case",
        ),
        baseline_sha256=_BASELINE_HASH,
        candidate_sha256=_CANDIDATE_HASH,
        candidate_variant="Q4_K_M",
    )

    assert report["new_critical_errors"] == 1
    assert report["new_critical_error_instances"] == [
        {
            "case_id": "candidate-only-case",
            "error": "changed_number",
        }
    ]


def test_quality_delta_verifier_recomputes_and_rejects_tampering() -> None:
    baseline = _evaluation("bf16", 0.98, 0)
    candidate = _evaluation("Q8_0", 0.979, 0)
    report = build_quality_delta_report(
        baseline,
        candidate,
        baseline_sha256=_BASELINE_HASH,
        candidate_sha256=_CANDIDATE_HASH,
        candidate_variant="Q8_0",
    )
    report["maximum_absolute_metric_drop"] = 0.0

    with pytest.raises(QualityDeltaError, match="does not match recomputed"):
        verify_quality_delta_report(
            report,
            baseline,
            candidate,
            baseline_sha256=_BASELINE_HASH,
            candidate_sha256=_CANDIDATE_HASH,
            candidate_variant="Q8_0",
        )

"""Deterministic BF16-relative quality deltas for final quantized variants."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_SCHEMA = Path(__file__).parents[2] / "contracts" / "quality_delta_report_schema.json"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class QualityDeltaError(RuntimeError):
    """Evaluation reports cannot form a complete BF16-relative delta."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualityDeltaError(f"{label} must be a mapping")
    return value


def _quality_metrics(report: Mapping[str, object]) -> dict[str, float]:
    metrics = _mapping(report.get("metrics"), "evaluation metrics")
    slices = _mapping(metrics.get("slice_reports"), "evaluation slice reports")
    selected: dict[str, float] = {}

    def visit(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key in sorted(value):
                visit(value[key], (*path, str(key)))
            return
        if isinstance(value, bool) or not isinstance(value, int | float) or not path:
            return
        leaf = path[-1]
        if (
            "critical_errors" in path
            or leaf in {"total", "count", "minimum_cases"}
            or leaf.endswith("_count")
        ):
            return
        if (
            leaf.endswith(("_rate", "_f1", "_exact_match"))
            or "hard_content_exact_rates" in path
        ):
            selected[".".join(path)] = float(value)

    for slice_name in sorted(slices):
        visit(slices[slice_name], (str(slice_name),))
    if not selected:
        raise QualityDeltaError("evaluation report has no comparable quality metrics")
    return selected


def _critical_error_instances(
    report: Mapping[str, object],
) -> dict[str, frozenset[str]]:
    metrics = _mapping(report.get("metrics"), "evaluation metrics")
    raw_results = metrics.get("case_results")
    if not isinstance(raw_results, list) or not raw_results:
        raise QualityDeltaError(
            "evaluation metrics must contain non-empty per-case critical-error evidence"
        )
    instances: dict[str, frozenset[str]] = {}
    for index, raw_result in enumerate(raw_results):
        result = _mapping(raw_result, f"case result {index}")
        case_id = result.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise QualityDeltaError(f"case result {index} has no valid case_id")
        if case_id in instances:
            raise QualityDeltaError(f"duplicate evaluation case result: {case_id}")
        raw_errors = result.get("critical_errors")
        if (
            not isinstance(raw_errors, list | tuple)
            or any(not isinstance(error, str) or not error for error in raw_errors)
            or len(set(raw_errors)) != len(raw_errors)
        ):
            raise QualityDeltaError(
                f"case result {case_id} has invalid critical_errors"
            )
        instances[case_id] = frozenset(raw_errors)
    return instances


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))


def build_quality_delta_report(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    baseline_sha256: str,
    candidate_sha256: str,
    candidate_variant: str,
) -> dict[str, object]:
    """Compare complete metric surfaces and count newly introduced critical errors."""

    if baseline.get("candidate") != "bf16":
        raise QualityDeltaError("baseline evaluation candidate must be bf16")
    if candidate.get("candidate") != candidate_variant:
        raise QualityDeltaError("candidate evaluation identity differs from requested variant")
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        if report.get("exact_case_coverage") is not True:
            raise QualityDeltaError(f"{label} evaluation does not have exact case coverage")
        gate = _mapping(report.get("gate"), f"{label} evaluation gate")
        if gate.get("passed") is not True:
            raise QualityDeltaError(f"{label} evaluation gate did not pass")
    for label, value in (
        ("baseline evaluation hash", baseline_sha256),
        ("candidate evaluation hash", candidate_sha256),
    ):
        if _HASH_RE.fullmatch(value) is None:
            raise QualityDeltaError(f"{label} must use sha256:<64 lowercase hex>")
    baseline_metrics = _quality_metrics(baseline)
    candidate_metrics = _quality_metrics(candidate)
    if set(baseline_metrics) != set(candidate_metrics):
        missing = sorted(set(baseline_metrics).difference(candidate_metrics))
        extra = sorted(set(candidate_metrics).difference(baseline_metrics))
        raise QualityDeltaError(
            f"evaluation metric surface differs; missing={missing[:3]}, extra={extra[:3]}"
        )
    baseline_errors = _critical_error_instances(baseline)
    candidate_errors = _critical_error_instances(candidate)
    if set(baseline_errors) != set(candidate_errors):
        missing = sorted(set(baseline_errors).difference(candidate_errors))
        extra = sorted(set(candidate_errors).difference(baseline_errors))
        raise QualityDeltaError(
            f"evaluation case surface differs; missing={missing[:3]}, extra={extra[:3]}"
        )
    new_critical_error_instances = [
        {
            "case_id": case_id,
            "error": error,
        }
        for case_id in sorted(candidate_errors)
        for error in sorted(candidate_errors[case_id].difference(baseline_errors[case_id]))
    ]
    rows = [
        {
            "metric": metric,
            "baseline": baseline_metrics[metric],
            "candidate": candidate_metrics[metric],
            "drop": round(
                max(0.0, baseline_metrics[metric] - candidate_metrics[metric]),
                12,
            ),
        }
        for metric in sorted(baseline_metrics)
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "complete",
        "baseline_variant": "bf16",
        "candidate_variant": candidate_variant,
        "baseline_evaluation_sha256": baseline_sha256,
        "candidate_evaluation_sha256": candidate_sha256,
        "maximum_absolute_metric_drop": max(float(row["drop"]) for row in rows),
        "new_critical_errors": len(new_critical_error_instances),
        "new_critical_error_instances": new_critical_error_instances,
        "metric_deltas": rows,
    }
    errors = sorted(
        _validator().iter_errors(report),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise QualityDeltaError(
            f"quality delta report schema failed at {location}: {errors[0].message}"
        )
    return report


def verify_quality_delta_report(
    report: Mapping[str, object],
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    baseline_sha256: str,
    candidate_sha256: str,
    candidate_variant: str,
) -> dict[str, object]:
    """Recompute a delta from the bound evaluations and require byte-level semantics."""

    expected = build_quality_delta_report(
        baseline,
        candidate,
        baseline_sha256=baseline_sha256,
        candidate_sha256=candidate_sha256,
        candidate_variant=candidate_variant,
    )
    if dict(report) != expected:
        raise QualityDeltaError(
            "quality delta report does not match recomputed evaluation evidence"
        )
    return expected

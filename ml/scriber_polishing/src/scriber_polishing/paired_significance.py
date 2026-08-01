"""Hash-bound, seeded paired-bootstrap comparisons of two evaluation reports."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_PROJECT_ROOT = Path(__file__).parents[2]
_REPORT_SCHEMA = _PROJECT_ROOT / "contracts" / "paired_significance_report_schema.json"

PAIRABLE_CASE_METRICS = frozenset(
    {
        "sst_valid",
        "output_only_compliant",
        "strict_exact_match",
        "normalized_exact_match",
        "protected_spans_exact",
        "ast_exact_match",
        "renderer_round_trip",
        "safe_renderer",
        "block_sequence_exact",
    }
)


@cache
def _report_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_REPORT_SCHEMA.read_text(encoding="utf-8")))


def validate_paired_significance_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate report shape and the deterministic relationships that bind its conclusion."""

    report = dict(value)
    errors = sorted(_report_validator().iter_errors(report), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"paired significance report schema failed: {errors[0].message}")
    inputs = report["inputs"]
    candidates = report["candidates"]
    paired_delta = report["paired_delta"]
    bootstrap = report["bootstrap"]
    if inputs["base_report_sha256"] == inputs["fine_tuned_report_sha256"]:
        raise ValueError("paired significance report binds the same input hash twice")
    if candidates["base"] == candidates["fine_tuned"]:
        raise ValueError("paired significance report compares the same candidate twice")
    for name, number in (
        ("base_mean", paired_delta["base_mean"]),
        ("fine_tuned_mean", paired_delta["fine_tuned_mean"]),
        ("mean", paired_delta["mean"]),
        ("minimum", paired_delta["minimum"]),
        ("lower", bootstrap["lower"]),
        ("median", bootstrap["median"]),
        ("upper", bootstrap["upper"]),
    ):
        if not math.isfinite(float(number)):
            raise ValueError(f"paired significance report {name} must be finite")
    expected_mean = float(paired_delta["fine_tuned_mean"]) - float(paired_delta["base_mean"])
    if not math.isclose(float(paired_delta["mean"]), expected_mean, abs_tol=1e-12):
        raise ValueError("paired significance report mean delta is inconsistent")
    if not float(bootstrap["lower"]) <= float(bootstrap["median"]) <= float(bootstrap["upper"]):
        raise ValueError("paired significance report bootstrap interval is not ordered")
    expected_passed = float(bootstrap["lower"]) > float(paired_delta["minimum"])
    if bootstrap["passed"] is not expected_passed:
        raise ValueError("paired significance report bootstrap decision is inconsistent")
    expected_decision = "SIGNIFICANT_IMPROVEMENT" if expected_passed else "NO_SIGNIFICANT_IMPROVEMENT"
    if report["decision"] != expected_decision:
        raise ValueError("paired significance report decision is inconsistent")
    return report


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hexadecimal digest")
    return value


def _require_probability(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    probability = float(value)
    if not 0.0 < probability < 1.0:
        raise ValueError(f"{label} must be strictly between zero and one")
    return probability


def _case_metric_values(report: Mapping[str, Any], *, metric: str) -> dict[str, float]:
    if report.get("exact_case_coverage") is not True:
        raise ValueError("evaluation report must prove exact case coverage")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("evaluation report metrics must be an object")
    case_results = metrics.get("case_results")
    if not isinstance(case_results, list) or not case_results:
        raise ValueError("evaluation report must contain non-empty per-case results")
    values: dict[str, float] = {}
    for case in case_results:
        if not isinstance(case, Mapping):
            raise ValueError("evaluation report case result must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("evaluation report case result has an invalid case_id")
        if case_id in values:
            raise ValueError(f"evaluation report contains duplicate case_id: {case_id}")
        value = case.get(metric)
        if isinstance(value, bool) or (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        ):
            values[case_id] = float(value)
        else:
            raise ValueError(f"evaluation report case {case_id} is missing finite metric {metric!r}")
    return values


def _nearest_rank(values: list[float], probability: float) -> float:
    index = max(0, min(len(values) - 1, math.ceil(probability * len(values)) - 1))
    return values[index]


def _paired_bootstrap(
    deltas: tuple[float, ...],
    *,
    seed: int,
    resamples: int,
    confidence_level: float,
) -> dict[str, Any]:
    generator = random.Random(seed)
    case_count = len(deltas)
    samples = sorted(
        sum(deltas[generator.randrange(case_count)] for _ in range(case_count)) / case_count
        for _ in range(resamples)
    )
    tail = (1.0 - confidence_level) / 2.0
    return {
        "method": "paired_case_mean_with_replacement_nearest_rank_v1",
        "seed": seed,
        "resamples": resamples,
        "confidence_level": confidence_level,
        "lower": _nearest_rank(samples, tail),
        "median": _nearest_rank(samples, 0.5),
        "upper": _nearest_rank(samples, 1.0 - tail),
    }


def _case_id_sha256(case_ids: tuple[str, ...]) -> str:
    payload = json.dumps(case_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compare_paired_evaluations(
    base_report: Mapping[str, Any],
    fine_tuned_report: Mapping[str, Any],
    *,
    base_report_sha256: str,
    fine_tuned_report_sha256: str,
    metric: str,
    seed: int,
    resamples: int,
    confidence_level: float,
    minimum_delta: float,
) -> dict[str, Any]:
    """Compare the same frozen case IDs with a reproducible paired bootstrap."""

    if metric not in PAIRABLE_CASE_METRICS:
        raise ValueError(f"unsupported paired case metric: {metric}")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    confidence = _require_probability(confidence_level, label="confidence_level")
    if isinstance(minimum_delta, bool) or not isinstance(minimum_delta, int | float) or not math.isfinite(minimum_delta):
        raise ValueError("minimum_delta must be a finite number")
    base_hash = _require_sha256(base_report_sha256, label="base_report_sha256")
    fine_tuned_hash = _require_sha256(fine_tuned_report_sha256, label="fine_tuned_report_sha256")
    if base_hash == fine_tuned_hash:
        raise ValueError("base and fine-tuned reports must have different hashes")

    base_candidate = base_report.get("candidate")
    fine_tuned_candidate = fine_tuned_report.get("candidate")
    if not isinstance(base_candidate, str) or not base_candidate:
        raise ValueError("base evaluation report candidate must be a non-empty string")
    if not isinstance(fine_tuned_candidate, str) or not fine_tuned_candidate:
        raise ValueError("fine-tuned evaluation report candidate must be a non-empty string")
    if base_candidate == fine_tuned_candidate:
        raise ValueError("base and fine-tuned candidates must differ")

    base_reference_hash = _require_sha256(base_report.get("reference_sha256"), label="base reference_sha256")
    fine_tuned_reference_hash = _require_sha256(
        fine_tuned_report.get("reference_sha256"), label="fine-tuned reference_sha256"
    )
    if base_reference_hash != fine_tuned_reference_hash:
        raise ValueError("base and fine-tuned reports must use the same reference SHA-256")
    base_config_hash = _require_sha256(
        base_report.get("evaluation_config_sha256"), label="base evaluation_config_sha256"
    )
    fine_tuned_config_hash = _require_sha256(
        fine_tuned_report.get("evaluation_config_sha256"), label="fine-tuned evaluation_config_sha256"
    )
    if base_config_hash != fine_tuned_config_hash:
        raise ValueError("base and fine-tuned reports must use the same evaluation config SHA-256")

    base_values = _case_metric_values(base_report, metric=metric)
    fine_tuned_values = _case_metric_values(fine_tuned_report, metric=metric)
    if set(base_values) != set(fine_tuned_values):
        raise ValueError("base and fine-tuned reports must contain identical case IDs")
    case_ids = tuple(sorted(base_values))
    deltas = tuple(fine_tuned_values[case_id] - base_values[case_id] for case_id in case_ids)
    bootstrap = _paired_bootstrap(
        deltas,
        seed=seed,
        resamples=resamples,
        confidence_level=confidence,
    )
    mean_delta = sum(deltas) / len(deltas)
    significant_improvement = float(bootstrap["lower"]) > float(minimum_delta)
    report = {
        "schema_version": 1,
        "kind": "scriber_paired_evaluation_significance",
        "status": "complete",
        "decision": "SIGNIFICANT_IMPROVEMENT" if significant_improvement else "NO_SIGNIFICANT_IMPROVEMENT",
        "inputs": {
            "base_report_sha256": base_hash,
            "fine_tuned_report_sha256": fine_tuned_hash,
            "reference_sha256": base_reference_hash,
            "evaluation_config_sha256": base_config_hash,
            "case_count": len(case_ids),
            "case_id_sha256": _case_id_sha256(case_ids),
        },
        "candidates": {"base": base_candidate, "fine_tuned": fine_tuned_candidate},
        "metric": {"name": metric, "direction": "higher_is_better"},
        "paired_delta": {
            "base_mean": sum(base_values[case_id] for case_id in case_ids) / len(case_ids),
            "fine_tuned_mean": sum(fine_tuned_values[case_id] for case_id in case_ids) / len(case_ids),
            "mean": mean_delta,
            "minimum": float(minimum_delta),
        },
        "bootstrap": {**bootstrap, "passed": significant_improvement},
    }
    return validate_paired_significance_report(report)

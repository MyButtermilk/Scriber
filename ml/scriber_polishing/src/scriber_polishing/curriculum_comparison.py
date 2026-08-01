"""Hard-first matched curriculum comparison with deterministic paired bootstrap."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .curriculum import write_immutable_json
from .metrics import CaseEvaluation, EvaluationCase, evaluate_predictions

_PROJECT_ROOT = Path(__file__).parents[2]
_CONFIG_SCHEMA = _PROJECT_ROOT / "contracts" / "curriculum_comparison_config_schema.json"
_REPORT_SCHEMA = _PROJECT_ROOT / "contracts" / "curriculum_comparison_report_schema.json"
_EPSILON = 1e-12


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def validate_curriculum_comparison_config(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        materialized = json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except ValueError as error:
        raise ValueError("curriculum comparison config requires finite numbers") from error
    errors = sorted(_validator(_CONFIG_SCHEMA).iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"curriculum comparison config schema failed: {errors[0].message}")
    weights = materialized["weights"]
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("curriculum comparison weights must sum to exactly 1")
    return materialized


def load_curriculum_comparison_config(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"curriculum comparison config must be a regular file: {candidate}")
    try:
        value = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"curriculum comparison config is invalid YAML: {error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("curriculum comparison config must contain an object")
    return validate_curriculum_comparison_config(value)


def validate_curriculum_comparison_report(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        materialized = json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except ValueError as error:
        raise ValueError("curriculum comparison report requires finite numbers") from error
    errors = sorted(_validator(_REPORT_SCHEMA).iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"curriculum comparison report schema failed: {errors[0].message}")
    weights = materialized["policy"]["weights"]
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("curriculum comparison report weights must sum to exactly 1")
    for candidate in materialized["candidates"].values():
        expected_composite = sum(
            float(weights[name]) * float(candidate["components"][name])
            for name in weights
        )
        if not math.isclose(
            float(candidate["composite"]),
            expected_composite,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("curriculum comparison candidate composite is inconsistent")
    hard_safety = materialized["hard_safety"]
    expected_critical_names = sorted(
        set(materialized["candidates"]["control"]["critical_errors"])
        | set(materialized["candidates"]["staged"]["critical_errors"])
    )
    if sorted(hard_safety["critical_error_checks"]) != expected_critical_names:
        raise ValueError("curriculum comparison critical-error evidence is incomplete")
    for name in expected_critical_names:
        check = hard_safety["critical_error_checks"][name]
        if (
            check["control"]
            != materialized["candidates"]["control"]["critical_errors"].get(name, 0)
            or check["staged"]
            != materialized["candidates"]["staged"]["critical_errors"].get(name, 0)
        ):
            raise ValueError("curriculum comparison critical-error evidence is inconsistent")
    expected_metric_values: dict[str, tuple[float, float]] = {}
    for name in (
        "sst_parse_rate",
        "output_only_compliance_rate",
        "protected_span_exact_rate",
        "safe_renderer_rate",
        "renderer_round_trip_rate",
    ):
        expected_metric_values[name] = (
            materialized["candidates"]["control"]["safety_metrics"][name],
            materialized["candidates"]["staged"]["safety_metrics"][name],
        )
    hard_names = sorted(
        set(materialized["candidates"]["control"]["hard_content_exact_rates"])
        | set(materialized["candidates"]["staged"]["hard_content_exact_rates"])
    )
    for name in hard_names:
        expected_metric_values[f"hard_content:{name}"] = (
            materialized["candidates"]["control"]["hard_content_exact_rates"].get(
                name,
                1.0,
            ),
            materialized["candidates"]["staged"]["hard_content_exact_rates"].get(
                name,
                1.0,
            ),
        )
    if set(hard_safety["metric_checks"]) != set(expected_metric_values):
        raise ValueError("curriculum comparison metric evidence is incomplete")
    for name, (control_value, staged_value) in expected_metric_values.items():
        check = hard_safety["metric_checks"][name]
        if (
            check["control"] != control_value
            or check["staged"] != staged_value
        ):
            raise ValueError("curriculum comparison metric evidence is inconsistent")
    hard_checks = [
        *hard_safety["critical_error_checks"].values(),
        *hard_safety["metric_checks"].values(),
    ]
    if any(
        check["passed"]
        != (
            check["staged"] <= check["control"]
            if name == "critical_error_checks"
            else check["staged"] + _EPSILON >= check["control"]
        )
        for name in ("critical_error_checks", "metric_checks")
        for check in hard_safety[name].values()
    ):
        raise ValueError("curriculum comparison hard-safety check is inconsistent")
    if hard_safety["passed"] != all(check["passed"] for check in hard_checks):
        raise ValueError("curriculum comparison hard-safety summary is inconsistent")
    loss = materialized["eval_loss"]
    if not math.isclose(
        float(loss["ratio"]),
        float(loss["staged"]) / float(loss["control"]),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("curriculum comparison eval-loss ratio is inconsistent")
    if loss["passed"] != (loss["ratio"] <= loss["maximum_ratio"] + _EPSILON):
        raise ValueError("curriculum comparison eval-loss check is inconsistent")
    composite_delta = materialized["composite_delta"]
    expected_delta = (
        float(materialized["candidates"]["staged"]["composite"])
        - float(materialized["candidates"]["control"]["composite"])
    )
    if not math.isclose(
        float(composite_delta["actual"]),
        expected_delta,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("curriculum comparison composite delta is inconsistent")
    if composite_delta["passed"] != (
        composite_delta["actual"] + _EPSILON >= composite_delta["minimum"]
    ):
        raise ValueError("curriculum comparison composite-delta check is inconsistent")
    bootstrap = materialized["bootstrap"]
    if not bootstrap["lower"] <= bootstrap["median"] <= bootstrap["upper"]:
        raise ValueError("curriculum comparison bootstrap interval is not ordered")
    if bootstrap["passed"] != (bootstrap["lower"] > 0.0):
        raise ValueError("curriculum comparison bootstrap check is inconsistent")
    expected_checks = {
        "hard_safety": hard_safety["passed"],
        "eval_loss": loss["passed"],
        "minimum_composite_delta": composite_delta["passed"],
        "bootstrap_lower_bound": bootstrap["passed"],
    }
    if materialized["checks"] != expected_checks:
        raise ValueError("curriculum comparison binding checks are inconsistent")
    expected = "ADOPT_STAGED" if all(materialized["checks"].values()) else "KEEP_SHUFFLED"
    if materialized["decision"] != expected:
        raise ValueError("curriculum comparison decision differs from binding checks")
    return materialized


def _f1(expected: int, predicted: int, true_positive: int) -> float:
    precision = true_positive / predicted if predicted else float(expected == 0)
    recall = true_positive / expected if expected else float(predicted == 0)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("cannot average an empty metric collection")
    return sum(values) / len(values)


def _aggregate(results: Sequence[CaseEvaluation]) -> dict[str, Any]:
    if not results:
        raise ValueError("curriculum comparison requires at least one case")
    total = len(results)
    identity = [result for result in results if result.identity_exact_match is not None]
    hard_categories = sorted({category for result in results for category in result.hard_content})
    hard_rates: dict[str, float] = {}
    for category in hard_categories:
        applicable = [
            result
            for result in results
            if result.hard_content[category]["reference"]
            or result.hard_content[category]["candidate"]
        ]
        hard_rates[category] = (
            sum(bool(result.hard_content[category]["exact"]) for result in applicable)
            / len(applicable)
            if applicable
            else 1.0
        )

    quality_f1: dict[str, float] = {}
    for category in ("punctuation", "filler", "repetition_cleanup"):
        expected = sum(result.quality_counts[category]["expected"] for result in results)
        predicted = sum(result.quality_counts[category]["predicted"] for result in results)
        true_positive = sum(result.quality_counts[category]["true_positive"] for result in results)
        quality_f1[category] = _f1(expected, predicted, true_positive)

    reference_blocks: Counter[str] = Counter()
    predicted_blocks: Counter[str] = Counter()
    for result in results:
        reference_blocks.update(result.reference_block_types)
        predicted_blocks.update(result.prediction_block_types)
    block_type_f1 = _f1(
        sum(reference_blocks.values()),
        sum(predicted_blocks.values()),
        sum((reference_blocks & predicted_blocks).values()),
    )
    critical_errors = Counter(error for result in results for error in result.critical_errors)
    return {
        "sst_parse_rate": sum(result.sst_valid for result in results) / total,
        "output_only_compliance_rate": sum(result.output_only_compliant for result in results) / total,
        "normalized_exact_match": sum(result.normalized_exact_match for result in results) / total,
        "identity_exact_match": (
            sum(bool(result.identity_exact_match) for result in identity) / len(identity)
            if identity
            else 1.0
        ),
        "protected_span_exact_rate": sum(result.protected_spans_exact for result in results) / total,
        "ast_exact_match_rate": sum(result.ast_exact_match for result in results) / total,
        "renderer_round_trip_rate": sum(result.renderer_round_trip for result in results) / total,
        "safe_renderer_rate": sum(result.safe_renderer for result in results) / total,
        "block_sequence_exact_rate": sum(result.block_sequence_exact for result in results) / total,
        "block_type_f1": block_type_f1,
        "punctuation_f1": quality_f1["punctuation"],
        "filler_f1": quality_f1["filler"],
        "repetition_cleanup_f1": quality_f1["repetition_cleanup"],
        "hard_content_exact_rates": hard_rates,
        "critical_errors": dict(sorted(critical_errors.items())),
    }


def _score(
    aggregate: Mapping[str, Any],
    *,
    eval_loss_score: float,
    weights: Mapping[str, float],
) -> dict[str, Any]:
    hard_rates = aggregate["hard_content_exact_rates"]
    safety = _mean(
        [
            float(aggregate["protected_span_exact_rate"]),
            float(aggregate["output_only_compliance_rate"]),
            float(aggregate["safe_renderer_rate"]),
            float(aggregate["renderer_round_trip_rate"]),
            *(float(value) for value in hard_rates.values()),
        ]
    )
    quality = _mean(
        [
            float(aggregate["identity_exact_match"]),
            float(aggregate["punctuation_f1"]),
            float(aggregate["filler_f1"]),
            float(aggregate["repetition_cleanup_f1"]),
            float(aggregate["normalized_exact_match"]),
        ]
    )
    structure = _mean(
        [
            float(aggregate["sst_parse_rate"]),
            float(aggregate["ast_exact_match_rate"]),
            float(aggregate["block_sequence_exact_rate"]),
            float(aggregate["block_type_f1"]),
        ]
    )
    components = {
        "safety_content": safety,
        "task_quality": quality,
        "structure": structure,
        "eval_loss": eval_loss_score,
    }
    composite = sum(float(weights[name]) * value for name, value in components.items())
    return {"components": components, "composite": composite}


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    index = max(0, min(len(sorted_values) - 1, math.ceil(probability * len(sorted_values)) - 1))
    return float(sorted_values[index])


def _paired_bootstrap(
    control: Sequence[CaseEvaluation],
    staged: Sequence[CaseEvaluation],
    *,
    control_loss_score: float,
    staged_loss_score: float,
    weights: Mapping[str, float],
    seed: int,
    resamples: int,
    confidence_level: float,
) -> dict[str, float | int]:
    if len(control) != len(staged) or not control:
        raise ValueError("paired bootstrap requires equal non-empty case collections")
    generator = random.Random(seed)
    count = len(control)
    deltas: list[float] = []
    for _ in range(resamples):
        indices = [generator.randrange(count) for _ in range(count)]
        control_score = _score(
            _aggregate([control[index] for index in indices]),
            eval_loss_score=control_loss_score,
            weights=weights,
        )["composite"]
        staged_score = _score(
            _aggregate([staged[index] for index in indices]),
            eval_loss_score=staged_loss_score,
            weights=weights,
        )["composite"]
        deltas.append(float(staged_score) - float(control_score))
    deltas.sort()
    tail = (1.0 - confidence_level) / 2.0
    return {
        "seed": seed,
        "resamples": resamples,
        "confidence_level": confidence_level,
        "lower": _percentile(deltas, tail),
        "median": _percentile(deltas, 0.5),
        "upper": _percentile(deltas, 1.0 - tail),
    }


def aggregate_case_evaluations(
    results: Sequence[CaseEvaluation],
) -> dict[str, Any]:
    """Expose the fixed comparison aggregation to non-Pilot adapters."""

    return _aggregate(results)


def score_comparison_candidate(
    aggregate: Mapping[str, Any],
    *,
    eval_loss_score: float,
    weights: Mapping[str, float],
) -> dict[str, Any]:
    """Apply the pre-bound composite definition to one aggregate."""

    return _score(
        aggregate,
        eval_loss_score=eval_loss_score,
        weights=weights,
    )


def paired_bootstrap_composite_delta(
    control: Sequence[CaseEvaluation],
    staged: Sequence[CaseEvaluation],
    *,
    control_loss_score: float,
    staged_loss_score: float,
    weights: Mapping[str, float],
    seed: int,
    resamples: int,
    confidence_level: float,
) -> dict[str, float | int]:
    """Bootstrap the matched composite delta without changing metric logic."""

    return _paired_bootstrap(
        control,
        staged,
        control_loss_score=control_loss_score,
        staged_loss_score=staged_loss_score,
        weights=weights,
        seed=seed,
        resamples=resamples,
        confidence_level=confidence_level,
    )


def _case_results(cases: Sequence[EvaluationCase]) -> tuple[CaseEvaluation, ...]:
    return evaluate_predictions(cases).case_results


def compare_curriculum(
    control_cases: Sequence[EvaluationCase],
    staged_cases: Sequence[EvaluationCase],
    *,
    control_eval_loss: float,
    staged_eval_loss: float,
    config: Mapping[str, Any],
    config_sha256: str,
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare matched predictions; hard non-regression dominates every score."""

    policy = validate_curriculum_comparison_config(config)
    if (
        not isinstance(config_sha256, str)
        or len(config_sha256) != 64
        or any(character not in "0123456789abcdef" for character in config_sha256)
    ):
        raise ValueError("curriculum comparison config SHA-256 must be lowercase hexadecimal")
    for value, label in (
        (control_eval_loss, "control_eval_loss"),
        (staged_eval_loss, "staged_eval_loss"),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be a finite positive number")
    control_ids = [case.case_id for case in control_cases]
    staged_ids = [case.case_id for case in staged_cases]
    if control_ids != staged_ids:
        raise ValueError("curriculum comparison cases must have identical ordered IDs")
    if inputs.get("case_count") != len(control_ids):
        raise ValueError("curriculum comparison input count differs from cases")

    control_results = _case_results(control_cases)
    staged_results = _case_results(staged_cases)
    control_aggregate = _aggregate(control_results)
    staged_aggregate = _aggregate(staged_results)
    best_loss = min(float(control_eval_loss), float(staged_eval_loss))
    control_loss_score = best_loss / float(control_eval_loss)
    staged_loss_score = best_loss / float(staged_eval_loss)
    weights = policy["weights"]
    control_score = _score(
        control_aggregate,
        eval_loss_score=control_loss_score,
        weights=weights,
    )
    staged_score = _score(
        staged_aggregate,
        eval_loss_score=staged_loss_score,
        weights=weights,
    )

    critical_checks: dict[str, dict[str, float | bool]] = {}
    error_names = sorted(
        set(control_aggregate["critical_errors"]) | set(staged_aggregate["critical_errors"])
    )
    for name in error_names:
        control_value = int(control_aggregate["critical_errors"].get(name, 0))
        staged_value = int(staged_aggregate["critical_errors"].get(name, 0))
        critical_checks[name] = {
            "control": control_value,
            "staged": staged_value,
            "passed": staged_value <= control_value,
        }

    metric_values: dict[str, tuple[float, float]] = {}
    for name in (
        "sst_parse_rate",
        "output_only_compliance_rate",
        "protected_span_exact_rate",
        "safe_renderer_rate",
        "renderer_round_trip_rate",
    ):
        metric_values[name] = (
            float(control_aggregate[name]),
            float(staged_aggregate[name]),
        )
    hard_names = sorted(
        set(control_aggregate["hard_content_exact_rates"])
        | set(staged_aggregate["hard_content_exact_rates"])
    )
    for name in hard_names:
        metric_values[f"hard_content:{name}"] = (
            float(control_aggregate["hard_content_exact_rates"].get(name, 1.0)),
            float(staged_aggregate["hard_content_exact_rates"].get(name, 1.0)),
        )
    metric_checks = {
        name: {
            "control": control_value,
            "staged": staged_value,
            "passed": staged_value + _EPSILON >= control_value,
        }
        for name, (control_value, staged_value) in metric_values.items()
    }
    hard_safety_passed = all(
        check["passed"] for check in [*critical_checks.values(), *metric_checks.values()]
    )
    loss_ratio = float(staged_eval_loss) / float(control_eval_loss)
    eval_loss_passed = loss_ratio <= float(policy["maximum_eval_loss_ratio"]) + _EPSILON
    delta = float(staged_score["composite"]) - float(control_score["composite"])
    delta_passed = delta + _EPSILON >= float(policy["minimum_composite_delta"])
    bootstrap = _paired_bootstrap(
        control_results,
        staged_results,
        control_loss_score=control_loss_score,
        staged_loss_score=staged_loss_score,
        weights=weights,
        seed=int(policy["seed"]),
        resamples=int(policy["bootstrap_resamples"]),
        confidence_level=float(policy["confidence_level"]),
    )
    bootstrap_passed = float(bootstrap["lower"]) > 0.0
    bootstrap["passed"] = bootstrap_passed
    checks = {
        "hard_safety": hard_safety_passed,
        "eval_loss": eval_loss_passed,
        "minimum_composite_delta": delta_passed,
        "bootstrap_lower_bound": bootstrap_passed,
    }

    candidate_payloads: dict[str, dict[str, Any]] = {}
    for name, aggregate, score, eval_loss in (
        ("control", control_aggregate, control_score, float(control_eval_loss)),
        ("staged", staged_aggregate, staged_score, float(staged_eval_loss)),
    ):
        candidate_payloads[name] = {
            "eval_loss": eval_loss,
            "components": score["components"],
            "composite": score["composite"],
            "critical_errors": aggregate["critical_errors"],
            "safety_metrics": {
                metric: aggregate[metric]
                for metric in (
                    "sst_parse_rate",
                    "output_only_compliance_rate",
                    "protected_span_exact_rate",
                    "safe_renderer_rate",
                    "renderer_round_trip_rate",
                )
            },
            "hard_content_exact_rates": aggregate["hard_content_exact_rates"],
        }
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_curriculum_comparison",
        "status": "complete",
        "decision": "ADOPT_STAGED" if all(checks.values()) else "KEEP_SHUFFLED",
        "inputs": dict(inputs),
        "policy": {
            "config_sha256": config_sha256,
            "weights": dict(weights),
        },
        "candidates": candidate_payloads,
        "hard_safety": {
            "passed": hard_safety_passed,
            "critical_error_checks": critical_checks,
            "metric_checks": metric_checks,
        },
        "eval_loss": {
            "control": float(control_eval_loss),
            "staged": float(staged_eval_loss),
            "ratio": loss_ratio,
            "maximum_ratio": float(policy["maximum_eval_loss_ratio"]),
            "passed": eval_loss_passed,
        },
        "composite_delta": {
            "actual": delta,
            "minimum": float(policy["minimum_composite_delta"]),
            "passed": delta_passed,
        },
        "bootstrap": bootstrap,
        "checks": checks,
    }
    return validate_curriculum_comparison_report(report)


def write_curriculum_comparison_report(
    report: Mapping[str, Any],
    destination: str | Path,
) -> Path:
    validated = validate_curriculum_comparison_report(report)
    return write_immutable_json(
        destination,
        validated,
        label="curriculum comparison report",
    )


def sha256_path(path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"comparison input must be a regular file: {candidate}")
    return hashlib.sha256(candidate.read_bytes()).hexdigest()

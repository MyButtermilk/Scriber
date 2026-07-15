from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


PERCENTILE_WEIGHTS = {
    "p50": 0.40,
    "p95": 0.60,
}

SCENARIO_WEIGHTS = {
    "overlay_warm": 0.20,
    "overlay_cold": 0.10,
    "microsoft_local_tail": 0.25,
    "soniox_local_tail": 0.30,
    "app_ux": 0.15,
}

SCENARIO_METRICS = {
    scenario: {
        percentile: f"{scenario}_{percentile}_ms"
        for percentile in PERCENTILE_WEIGHTS
    }
    for scenario in SCENARIO_WEIGHTS
}


def finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def load_baseline_metrics(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "benchmarks" / "results" / "baseline.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    return metrics if isinstance(metrics, dict) else {}


def compute_scenario_score(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    scenario: str,
) -> float | str:
    """Return the B7 p50/p95 score for one required local UX scenario."""

    metric_names = SCENARIO_METRICS.get(scenario)
    if metric_names is None:
        return "unknown"

    score = 0.0
    for percentile, percentile_weight in PERCENTILE_WEIGHTS.items():
        metric_name = metric_names[percentile]
        candidate_value = finite_positive(metrics.get(metric_name))
        baseline_value = finite_positive(baseline.get(metric_name))
        if candidate_value is None or baseline_value is None:
            return "unknown"
        ratio = candidate_value / baseline_value
        if not math.isfinite(ratio) or ratio <= 0:
            return "unknown"
        score += percentile_weight * ratio

    return score if math.isfinite(score) and score > 0 else "unknown"


def compute_local_wux(metrics: dict[str, Any], baseline: dict[str, Any]) -> float | str:
    """Compute the B7 local-WUX geometric composite from all five scenarios.

    Readiness and resource metrics are intentionally outside this scorer. A
    missing, non-finite, or non-positive required p50/p95 value fails closed.
    """

    weighted_log_sum = 0.0
    for scenario, scenario_weight in SCENARIO_WEIGHTS.items():
        scenario_score = compute_scenario_score(metrics, baseline, scenario)
        if scenario_score == "unknown":
            return "unknown"
        weighted_log_sum += scenario_weight * math.log(scenario_score)

    if not math.isfinite(weighted_log_sum):
        return "unknown"
    try:
        result = math.exp(weighted_log_sum)
    except OverflowError:
        return "unknown"
    if not math.isfinite(result) or result <= 0:
        return "unknown"
    return round(result, 6)

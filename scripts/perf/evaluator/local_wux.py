from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


PERCENTILE_WEIGHTS = {
    "p50": 0.40,
    "p95": 0.60,
}

PROVIDER_REPLAY_DURATION_SECONDS = (5, 15, 30, 60)
PROVIDER_REPLAY_KPIS = (
    "activation_received_to_final_text_observed",
    "stop_requested_to_final_text_observed",
)
PROVIDER_REPLAY_WEIGHTS = {
    "microsoft_local": 0.25,
    "soniox_local": 0.30,
    # Speechmatics is an independent fail-closed/non-regression gate. Keeping
    # its score weight at zero preserves the existing Microsoft/Soniox score
    # contribution while still requiring every exact Speechmatics series.
    "speechmatics_local": 0.0,
}


def provider_replay_scenario_name(
    provider_scenario: str,
    duration_seconds: int,
    kpi: str,
) -> str:
    if provider_scenario not in PROVIDER_REPLAY_WEIGHTS:
        raise ValueError("provider replay scenario is not scoreable")
    if duration_seconds not in PROVIDER_REPLAY_DURATION_SECONDS:
        raise ValueError("provider replay duration is not scoreable")
    if kpi not in PROVIDER_REPLAY_KPIS:
        raise ValueError("provider replay KPI is not scoreable")
    return f"{provider_scenario}_{duration_seconds}s_{kpi}"


PROVIDER_REPLAY_SCENARIO_WEIGHTS = {
    provider_replay_scenario_name(provider, duration, kpi): (
        provider_weight
        / len(PROVIDER_REPLAY_DURATION_SECONDS)
        / len(PROVIDER_REPLAY_KPIS)
    )
    for provider, provider_weight in PROVIDER_REPLAY_WEIGHTS.items()
    for duration in PROVIDER_REPLAY_DURATION_SECONDS
    for kpi in PROVIDER_REPLAY_KPIS
}

SCENARIO_WEIGHTS = {
    "overlay_warm": 0.20,
    "overlay_cold": 0.10,
    **PROVIDER_REPLAY_SCENARIO_WEIGHTS,
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


def finite_non_negative(value: Any) -> float | None:
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
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def canonical_provider_replay_metric_names() -> tuple[str, ...]:
    return tuple(
        metric_name
        for scenario in PROVIDER_REPLAY_SCENARIO_WEIGHTS
        for metric_name in (
            f"{scenario}_p50_ms",
            f"{scenario}_p95_ms",
        )
    )


def canonical_provider_replay_evidence_valid(metrics: dict[str, Any]) -> bool:
    """Require complete, non-negative evidence for every provider-duration KPI.

    Failure rates and sample counts stay provider- and duration-specific.  A
    pooled aggregate is deliberately not accepted as a substitute.
    """

    for scenario in PROVIDER_REPLAY_SCENARIO_WEIGHTS:
        for percentile in PERCENTILE_WEIGHTS:
            if finite_positive(metrics.get(f"{scenario}_{percentile}_ms")) is None:
                return False
        failure_rate = finite_non_negative(metrics.get(f"{scenario}_failure_rate"))
        sample_count = metrics.get(f"{scenario}_sample_count")
        capture_attested = metrics.get(f"{scenario}_capture_attested")
        if failure_rate is None or failure_rate != 0.0:
            return False
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count <= 0
        ):
            return False
        if (
            isinstance(capture_attested, bool)
            or not isinstance(capture_attested, int)
            or capture_attested != 1
        ):
            return False
    return True


def canonical_provider_replay_non_regression(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> bool:
    """Reject a candidate when any canonical provider-duration KPI regresses."""

    for metric_name in canonical_provider_replay_metric_names():
        candidate = finite_positive(metrics.get(metric_name))
        reference = finite_positive(baseline.get(metric_name))
        if candidate is None or reference is None or candidate > reference:
            return False
    return True


def canonical_provider_replay_promotion_eligible(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> bool:
    return bool(
        canonical_provider_replay_evidence_valid(metrics)
        and canonical_provider_replay_non_regression(metrics, baseline)
    )


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
    """Compute local-WUX from UI and canonical visible-text latency.

    Provider-final tails are diagnostics only.  Provider replay contributes
    through each exact provider x duration x canonical KPI series, so a short
    sample cannot hide a long-sample regression (or vice versa).  Readiness and
    resource metrics remain outside this scorer.  Missing, non-finite, or
    non-positive required p50/p95 evidence fails closed.
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

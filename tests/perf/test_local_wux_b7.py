from __future__ import annotations

import math

import pytest

from scripts.perf.evaluator.local_wux import (
    PERCENTILE_WEIGHTS,
    SCENARIO_METRICS,
    SCENARIO_WEIGHTS,
    compute_local_wux,
    compute_scenario_score,
)


SCENARIOS = (
    "overlay_warm",
    "overlay_cold",
    "microsoft_local_tail",
    "soniox_local_tail",
    "app_ux",
)


def complete_metrics(value: float = 100.0) -> dict[str, float]:
    return {
        metric_name: value
        for metric_names in SCENARIO_METRICS.values()
        for metric_name in metric_names.values()
    }


def test_b7_contract_uses_only_the_five_local_ux_scenarios() -> None:
    assert tuple(SCENARIO_WEIGHTS) == SCENARIOS
    assert SCENARIO_WEIGHTS == {
        "overlay_warm": 0.20,
        "overlay_cold": 0.10,
        "microsoft_local_tail": 0.25,
        "soniox_local_tail": 0.30,
        "app_ux": 0.15,
    }
    assert PERCENTILE_WEIGHTS == {"p50": 0.40, "p95": 0.60}
    assert math.isclose(sum(SCENARIO_WEIGHTS.values()), 1.0)
    assert math.isclose(sum(PERCENTILE_WEIGHTS.values()), 1.0)
    assert set(SCENARIO_METRICS) == set(SCENARIOS)
    assert all(set(metric_names) == {"p50", "p95"} for metric_names in SCENARIO_METRICS.values())


def test_identical_candidate_and_baseline_score_one() -> None:
    baseline = complete_metrics()
    assert compute_local_wux(dict(baseline), baseline) == 1.0


def test_uniform_improvement_is_preserved_by_the_composite() -> None:
    baseline = complete_metrics(250.0)
    candidate = {name: value * 0.8 for name, value in baseline.items()}
    assert compute_local_wux(candidate, baseline) == 0.8


def test_scenario_score_weights_p50_and_p95_before_geometric_composition() -> None:
    baseline = complete_metrics()
    candidate = dict(baseline)
    candidate["overlay_warm_p50_ms"] = 50.0
    candidate["overlay_warm_p95_ms"] = 150.0

    expected_scenario_score = 0.40 * 0.50 + 0.60 * 1.50
    expected_composite = round(expected_scenario_score**0.20, 6)

    assert compute_scenario_score(candidate, baseline, "overlay_warm") == pytest.approx(
        expected_scenario_score
    )
    assert compute_local_wux(candidate, baseline) == expected_composite


def test_nonuniform_scenarios_use_the_goal_geometric_weights() -> None:
    baseline = complete_metrics()
    scenario_ratios = {
        "overlay_warm": (0.7, 0.8),
        "overlay_cold": (0.8, 0.9),
        "microsoft_local_tail": (0.9, 1.0),
        "soniox_local_tail": (1.0, 1.1),
        "app_ux": (1.1, 1.2),
    }
    candidate = dict(baseline)
    expected_log_sum = 0.0
    for scenario, (p50_ratio, p95_ratio) in scenario_ratios.items():
        candidate[f"{scenario}_p50_ms"] *= p50_ratio
        candidate[f"{scenario}_p95_ms"] *= p95_ratio
        scenario_score = 0.40 * p50_ratio + 0.60 * p95_ratio
        expected_log_sum += SCENARIO_WEIGHTS[scenario] * math.log(scenario_score)

    assert compute_local_wux(candidate, baseline) == round(math.exp(expected_log_sum), 6)


@pytest.mark.parametrize(
    "invalid_value",
    [None, "unknown", "", 0, -1, float("nan"), float("inf"), True],
)
@pytest.mark.parametrize("invalid_side", ["candidate", "baseline"])
def test_missing_nonpositive_and_nonfinite_values_fail_closed(
    invalid_value: object,
    invalid_side: str,
) -> None:
    baseline: dict[str, object] = complete_metrics()
    candidate: dict[str, object] = complete_metrics()
    target = candidate if invalid_side == "candidate" else baseline
    target["soniox_local_tail_p50_ms"] = invalid_value

    assert compute_local_wux(candidate, baseline) == "unknown"


def test_missing_any_required_percentile_fails_closed() -> None:
    baseline = complete_metrics()
    candidate = complete_metrics()
    del candidate["app_ux_p95_ms"]
    assert compute_local_wux(candidate, baseline) == "unknown"


def test_old_b6_p95_and_readiness_package_is_not_accepted() -> None:
    old_b6_metrics = {
        "overlay_warm_p95_ms": 100.0,
        "overlay_cold_p95_ms": 200.0,
        "microsoft_local_tail_p95_ms": 300.0,
        "soniox_local_tail_p95_ms": 400.0,
        "app_ux_p95_ms": 500.0,
        "hotkey_mic_ready_p95_ms": 600.0,
        "hotkey_first_audio_frame_p95_ms": 700.0,
    }
    assert compute_local_wux(old_b6_metrics, old_b6_metrics) == "unknown"


def test_unknown_scenario_name_fails_closed() -> None:
    metrics = complete_metrics()
    assert compute_scenario_score(metrics, metrics, "readiness") == "unknown"

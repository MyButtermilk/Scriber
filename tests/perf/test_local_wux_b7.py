from __future__ import annotations

import math

import pytest

from scripts.perf.evaluator.local_wux import (
    PERCENTILE_WEIGHTS,
    PROVIDER_REPLAY_DURATION_SECONDS,
    PROVIDER_REPLAY_KPIS,
    PROVIDER_REPLAY_SCENARIO_WEIGHTS,
    PROVIDER_REPLAY_WEIGHTS,
    SCENARIO_METRICS,
    SCENARIO_WEIGHTS,
    canonical_provider_replay_evidence_valid,
    canonical_provider_replay_promotion_eligible,
    compute_local_wux,
    compute_scenario_score,
    provider_replay_scenario_name,
)


SCENARIOS = tuple(SCENARIO_WEIGHTS)


def complete_metrics(value: float = 100.0) -> dict[str, float]:
    metrics = {
        metric_name: value
        for metric_names in SCENARIO_METRICS.values()
        for metric_name in metric_names.values()
    }
    for scenario in PROVIDER_REPLAY_SCENARIO_WEIGHTS:
        metrics[f"{scenario}_failure_rate"] = 0.0
        metrics[f"{scenario}_sample_count"] = 5
        metrics[f"{scenario}_capture_attested"] = 1
    return metrics


def test_issue18_contract_scores_each_provider_duration_and_canonical_kpi() -> None:
    assert PROVIDER_REPLAY_DURATION_SECONDS == (5, 15, 30, 60)
    assert PROVIDER_REPLAY_KPIS == (
        "activation_received_to_final_text_observed",
        "stop_requested_to_final_text_observed",
    )
    assert PROVIDER_REPLAY_WEIGHTS == {
        "microsoft_local": 0.25,
        "soniox_local": 0.30,
        "speechmatics_local": 0.0,
    }
    assert len(PROVIDER_REPLAY_SCENARIO_WEIGHTS) == 24
    assert SCENARIO_WEIGHTS["overlay_warm"] == 0.20
    assert SCENARIO_WEIGHTS["overlay_cold"] == 0.10
    assert SCENARIO_WEIGHTS["app_ux"] == 0.15
    assert "microsoft_local_tail" not in SCENARIO_WEIGHTS
    assert "soniox_local_tail" not in SCENARIO_WEIGHTS
    assert "speechmatics_local_tail" not in SCENARIO_WEIGHTS
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
        scenario: (0.7 + index * 0.01, 0.8 + index * 0.01)
        for index, scenario in enumerate(SCENARIOS)
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
    target[
        "soniox_local_60s_activation_received_to_final_text_observed_p50_ms"
    ] = invalid_value

    assert compute_local_wux(candidate, baseline) == "unknown"


def test_missing_any_required_percentile_fails_closed() -> None:
    baseline = complete_metrics()
    candidate = complete_metrics()
    del candidate["app_ux_p95_ms"]
    assert compute_local_wux(candidate, baseline) == "unknown"


def test_pooled_provider_or_duration_metrics_cannot_replace_one_exact_series() -> None:
    baseline = complete_metrics()
    candidate = complete_metrics()
    missing = provider_replay_scenario_name(
        "microsoft_local",
        60,
        "activation_received_to_final_text_observed",
    )
    del candidate[f"{missing}_p50_ms"]
    del candidate[f"{missing}_p95_ms"]
    candidate["activation_received_to_final_text_observed_p50_ms"] = 1.0
    candidate["activation_received_to_final_text_observed_p95_ms"] = 1.0
    candidate["microsoft_local_activation_received_to_final_text_observed_p50_ms"] = 1.0
    candidate["microsoft_local_activation_received_to_final_text_observed_p95_ms"] = 1.0

    assert compute_local_wux(candidate, baseline) == "unknown"
    assert canonical_provider_replay_evidence_valid(candidate) is False


def test_missing_speechmatics_series_fails_closed_without_reweighting_existing_providers() -> None:
    baseline = complete_metrics()
    candidate = complete_metrics()
    scenario = provider_replay_scenario_name(
        "speechmatics_local",
        30,
        "stop_requested_to_final_text_observed",
    )
    del candidate[f"{scenario}_p95_ms"]

    assert math.isclose(sum(SCENARIO_WEIGHTS.values()), 1.0)
    assert compute_local_wux(candidate, baseline) == "unknown"
    assert canonical_provider_replay_evidence_valid(candidate) is False


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


def test_promotion_rejects_better_legacy_tail_when_activation_kpi_regresses() -> None:
    baseline = complete_metrics()
    candidate = dict(baseline)
    candidate["microsoft_local_tail_p50_ms"] = 1.0
    candidate["microsoft_local_tail_p95_ms"] = 1.0
    activation = provider_replay_scenario_name(
        "microsoft_local",
        30,
        "activation_received_to_final_text_observed",
    )
    candidate[f"{activation}_p95_ms"] = 101.0

    assert compute_local_wux(candidate, baseline) > 1.0
    assert canonical_provider_replay_promotion_eligible(candidate, baseline) is False


@pytest.mark.parametrize(
    ("field_suffix", "value"),
    [
        ("p50_ms", -1.0),
        ("p95_ms", float("nan")),
        ("failure_rate", -0.1),
        ("failure_rate", 0.01),
        ("sample_count", 0),
        ("sample_count", True),
        ("capture_attested", 0),
        ("capture_attested", True),
    ],
)
def test_canonical_evidence_fails_closed_on_invalid_series_values(
    field_suffix: str,
    value: object,
) -> None:
    metrics: dict[str, object] = complete_metrics()
    scenario = provider_replay_scenario_name(
        "soniox_local",
        15,
        "stop_requested_to_final_text_observed",
    )
    metrics[f"{scenario}_{field_suffix}"] = value

    assert canonical_provider_replay_evidence_valid(metrics) is False


def test_unknown_scenario_name_fails_closed() -> None:
    metrics = complete_metrics()
    assert compute_scenario_score(metrics, metrics, "readiness") == "unknown"

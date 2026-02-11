from src.core.golden_trace import evaluate_golden_trace, percentile


def test_percentile_uses_nearest_rank():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 50.0) == 30.0
    assert percentile(values, 95.0) == 50.0


def test_golden_trace_passes_within_target_and_regression():
    result = evaluate_golden_trace(
        [100.0, 120.0, 130.0, 140.0, 150.0],
        target_p95_ms=200.0,
        baseline_p95_ms=145.0,
        max_regression_pct=10.0,
    )
    assert result.passed is True
    assert result.target_ok is True
    assert result.regression_ok is True


def test_golden_trace_fails_on_regression():
    result = evaluate_golden_trace(
        [100.0, 150.0, 180.0, 260.0, 300.0],
        target_p95_ms=400.0,
        baseline_p95_ms=200.0,
        max_regression_pct=10.0,
    )
    assert result.passed is False
    assert result.target_ok is True
    assert result.regression_ok is False


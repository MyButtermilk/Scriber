from __future__ import annotations

from pathlib import Path


def read_runner() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )


def test_hybrid_baseline_runner_exposes_phase8_startup_budget() -> None:
    script = read_runner()

    assert "[switch]$FailOnPerformanceBudget" in script
    assert "[double]$MaxUiVisibleP95Ms = 3000.0" in script
    assert "[double]$MaxBackendReadyP95Ms = 5000.0" in script
    assert "function New-PerformanceBudgetCheck" in script
    assert "function New-PerformanceBudget" in script
    assert '-Name "ui_visible_p95"' in script
    assert '-Name "backend_ready_p95"' in script
    assert "performanceBudget = $performanceBudget" in script
    assert "failOnPerformanceBudget = [bool]$FailOnPerformanceBudget" in script
    assert "maxUiVisibleP95Ms = $MaxUiVisibleP95Ms" in script
    assert "maxBackendReadyP95Ms = $MaxBackendReadyP95Ms" in script


def test_hybrid_baseline_runner_can_fail_on_performance_budget() -> None:
    script = read_runner()

    assert 'status = $(if ($p95 -le $MaxP95Ms) { "passed" } else { "failed" })' in script
    assert "failedBudgets = @($notPassed | ForEach-Object { $_.name })" in script
    assert "if ($FailOnPerformanceBudget -and -not $result.performanceBudget.complete)" in script
    assert "exit 1" in script.split(
        "if ($FailOnPerformanceBudget -and -not $result.performanceBudget.complete)",
        maxsplit=1,
    )[1]

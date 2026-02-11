from __future__ import annotations

import os
import sys

from src.core.golden_trace import evaluate_golden_trace
from src.data.latency_metrics_store import LatencyMetricsStore


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> int:
    target_p95_ms = _env_float("SCRIBER_HOT_PATH_TARGET_P95_MS", 250.0)
    max_regression_pct = _env_float("SCRIBER_HOT_PATH_MAX_REGRESSION_PCT", 10.0)
    baseline_raw = os.getenv("SCRIBER_HOT_PATH_BASELINE_P95_MS", "").strip()
    baseline_p95 = float(baseline_raw) if baseline_raw else None
    sample_limit = max(10, _env_int("SCRIBER_HOT_PATH_SAMPLE_LIMIT", 200))

    store = LatencyMetricsStore()
    recent = store.latest(limit=sample_limit)
    samples = [m.total_ms for m in recent]
    result = evaluate_golden_trace(
        samples,
        target_p95_ms=target_p95_ms,
        baseline_p95_ms=baseline_p95,
        max_regression_pct=max_regression_pct,
    )
    print(
        "GoldenTrace",
        f"samples={result.sample_count}",
        f"p95_ms={result.p95_ms:.2f}",
        f"target_p95_ms={result.target_p95_ms:.2f}",
        f"baseline_p95_ms={result.baseline_p95_ms:.2f}",
        f"max_regression_pct={result.max_regression_pct:.2f}",
        f"passed={result.passed}",
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


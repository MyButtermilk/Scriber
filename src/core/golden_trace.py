from __future__ import annotations

from dataclasses import dataclass
from math import ceil


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    idx = max(0, min(len(ordered) - 1, int(ceil((pct / 100.0) * len(ordered)) - 1)))
    return float(ordered[idx])


@dataclass(frozen=True)
class GoldenTraceResult:
    sample_count: int
    p95_ms: float
    target_p95_ms: float
    baseline_p95_ms: float
    max_regression_pct: float
    target_ok: bool
    regression_ok: bool

    @property
    def passed(self) -> bool:
        return self.target_ok and self.regression_ok


def evaluate_golden_trace(
    samples_ms: list[float],
    *,
    target_p95_ms: float,
    baseline_p95_ms: float | None = None,
    max_regression_pct: float = 10.0,
) -> GoldenTraceResult:
    p95_ms = percentile(samples_ms, 95.0)
    baseline = p95_ms if baseline_p95_ms is None else float(baseline_p95_ms)
    allowed = baseline * (1.0 + (max(0.0, max_regression_pct) / 100.0))
    target_ok = p95_ms <= float(target_p95_ms)
    regression_ok = p95_ms <= allowed
    return GoldenTraceResult(
        sample_count=len(samples_ms),
        p95_ms=p95_ms,
        target_p95_ms=float(target_p95_ms),
        baseline_p95_ms=baseline,
        max_regression_pct=float(max_regression_pct),
        target_ok=target_ok,
        regression_ok=regression_ok,
    )


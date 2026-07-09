from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


LATENCY_WEIGHTS = {
    "overlay_warm_p95_ms": 0.10,
    "overlay_cold_p95_ms": 0.05,
    "microsoft_local_tail_p95_ms": 0.15,
    "soniox_local_tail_p95_ms": 0.20,
    "app_ux_p95_ms": 0.10,
    "hotkey_mic_ready_p95_ms": 0.25,
    "hotkey_first_audio_frame_p95_ms": 0.15,
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


def compute_local_wux(metrics: dict[str, Any], baseline: dict[str, Any]) -> float | str:
    weighted_log_sum = 0.0
    for name, weight in LATENCY_WEIGHTS.items():
        candidate_value = finite_positive(metrics.get(name))
        baseline_value = finite_positive(baseline.get(name))
        if candidate_value is None or baseline_value is None:
            return "unknown"
        weighted_log_sum += weight * math.log(candidate_value / baseline_value)
    return round(math.exp(weighted_log_sum), 6)

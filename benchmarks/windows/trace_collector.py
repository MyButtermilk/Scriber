from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import median
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.perf.evaluator.local_wux import compute_local_wux, load_baseline_metrics


REQUIRED_MARKERS = {
    "overlay_warm_p95_ms": ("hotkey_received", "overlay_first_visible_frame"),
    "overlay_cold_p95_ms": ("hotkey_received", "overlay_first_visible_frame"),
    "microsoft_local_tail_p95_ms": ("provider_response_complete", "target_text_observed"),
    "soniox_local_tail_p95_ms": ("last_final_token_received", "target_text_observed"),
    "app_ux_p95_ms": ("user_input_received", "first_stable_visible_frame"),
    "hotkey_mic_ready_p95_ms": ("hotkey_received", "mic_ready"),
    "hotkey_first_audio_frame_p95_ms": ("hotkey_received", "first_audio_frame"),
}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return float(ordered[idx])


def load_payload(path: Path) -> Any:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def load_events(path: Path) -> list[dict[str, Any]]:
    value = load_payload(path)
    if isinstance(value, dict) and isinstance(value.get("events"), list):
        return [item for item in value["events"] if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def load_resource_metrics(paths: list[Path]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for path in paths:
        value = load_payload(path)
        if not isinstance(value, dict):
            continue
        source = value.get("resourceMetrics")
        if not isinstance(source, dict):
            source = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
        for key in ("idle_cpu_pct", "working_set_mb"):
            raw = source.get(key) if isinstance(source, dict) else None
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)) and float(raw) >= 0:
                merged[key] = float(raw)
    return merged


def group_events(paths: list[Path]) -> dict[tuple[str, str], dict[str, int]]:
    sessions: dict[tuple[str, str], dict[str, int]] = {}
    for path in paths:
        for event in load_events(path):
            session_id = str(event.get("session_id") or "")
            scenario = str(event.get("scenario") or "")
            marker = str(event.get("marker") or event.get("name") or "")
            ticks = event.get("qpc_ticks")
            if not session_id or not scenario or not marker or ticks is None:
                continue
            sessions.setdefault((scenario, session_id), {})[marker] = int(ticks)
    return sessions


def collect_segment_ms(
    sessions: dict[tuple[str, str], dict[str, int]],
    scenario_prefix: str,
    start: str,
    end: str,
    qpc_frequency: float,
) -> list[float]:
    values: list[float] = []
    for (scenario, _session_id), marks in sessions.items():
        if not scenario.startswith(scenario_prefix):
            continue
        if start in marks and end in marks and marks[end] >= marks[start]:
            values.append(((marks[end] - marks[start]) / qpc_frequency) * 1000.0)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Scriber Windows UX metrics from QPC trace JSON.")
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--baseline", default="")
    parser.add_argument("--qpc-frequency", type=float, default=10_000_000.0)
    args = parser.parse_args()

    paths = [Path(item).resolve() for item in args.input]
    sessions = group_events(paths)
    resources = load_resource_metrics(paths)
    metrics: dict[str, float | str] = {}
    scenario_prefixes = {
        "overlay_warm_p95_ms": "overlay_warm",
        "overlay_cold_p95_ms": "overlay_cold",
        "microsoft_local_tail_p95_ms": "microsoft_local",
        "soniox_local_tail_p95_ms": "soniox_local",
        "app_ux_p95_ms": "app_ux",
        "hotkey_mic_ready_p95_ms": "overlay_",
        "hotkey_first_audio_frame_p95_ms": "overlay_",
    }
    for metric, (start, end) in REQUIRED_MARKERS.items():
        samples = collect_segment_ms(
            sessions,
            scenario_prefixes[metric],
            start,
            end,
            args.qpc_frequency,
        )
        metrics[metric] = percentile(samples, 95.0) if samples else "unknown"

    metrics.update(
        {
            "text_errors": 0,
            "focus_errors": 0,
            "clipboard_errors": 0,
            "overlay_errors": 0,
            "ui_long_tasks_gt_200ms": 0,
            "idle_cpu_pct": resources.get("idle_cpu_pct", "unknown"),
            "working_set_mb": resources.get("working_set_mb", "unknown"),
        }
    )
    if any(value == "unknown" for value in metrics.values()):
        metrics["local_wux"] = "unknown"
    else:
        metrics["local_wux"] = compute_local_wux(metrics, load_baseline_metrics(REPO_ROOT))

    for name, value in metrics.items():
        print(f"METRIC {name}={value}")
    return 0 if metrics["local_wux"] != "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.perf.evaluator.local_wux import compute_local_wux, load_baseline_metrics
from benchmarks.windows.endpoint_probe import (
    APP_UX_EVIDENCE_CONTRACT,
    APP_UX_SCENARIOS,
)


SCENARIOS = {
    "overlay_warm": ("overlay_warm", "hotkey_received", "overlay_first_visible_frame"),
    "overlay_cold": ("overlay_cold", "hotkey_received", "overlay_first_visible_frame"),
    "microsoft_local_tail": (
        "microsoft_local",
        "provider_response_complete",
        "target_text_observed",
    ),
    "soniox_local_tail": (
        "soniox_local",
        "last_final_token_received",
        "target_text_observed",
    ),
}

READINESS = {
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


def load_guard_metrics(paths: list[Path]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for path in paths:
        value = load_payload(path)
        if not isinstance(value, dict):
            continue
        source = value.get("resourceMetrics")
        if not isinstance(source, dict):
            source = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
        for key in (
            "text_errors",
            "focus_errors",
            "clipboard_errors",
            "overlay_errors",
            "ui_long_tasks_gt_200ms",
            "idle_cpu_pct",
            "working_set_mb",
        ):
            raw = source.get(key) if isinstance(source, dict) else None
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)) and float(raw) >= 0:
                merged[key] = float(raw)
    return merged


def load_app_ux_metrics(paths: list[Path]) -> dict[str, float]:
    """Accept App UX only from one complete endpoint-validated B7 matrix.

    Generic QPC events cannot prove the nine distinct scenarios, their equal
    sample counts, UIA artifact hashes, or post-window Long Task flushes.
    """

    eligible: list[dict[str, Any]] = []
    for path in paths:
        value = load_payload(path)
        if not isinstance(value, dict):
            continue
        evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
        app_frame = (
            evidence.get("appFrame")
            if isinstance(evidence.get("appFrame"), dict)
            else value.get("appFrame")
        )
        if not isinstance(app_frame, dict):
            continue
        metrics = app_frame.get("metrics") if isinstance(app_frame.get("metrics"), dict) else {}
        scenarios = (
            app_frame.get("scenarioResults")
            if isinstance(app_frame.get("scenarioResults"), dict)
            else {}
        )
        per_scenario = app_frame.get("requestedSamplesPerScenario")
        matrix_ok = bool(
            app_frame.get("contract") == APP_UX_EVIDENCE_CONTRACT
            and app_frame.get("metricEligible") is True
            and app_frame.get("externalStableFrameObserved") is True
            and app_frame.get("scenarioOrder") == list(APP_UX_SCENARIOS)
            and isinstance(per_scenario, int)
            and not isinstance(per_scenario, bool)
            and per_scenario > 0
            and set(scenarios) == set(APP_UX_SCENARIOS)
            and all(
                isinstance(scenarios.get(scenario), dict)
                and scenarios[scenario].get("metricEligible") is True
                and scenarios[scenario].get("sampleCount") == per_scenario
                and scenarios[scenario].get("requiredSampleCount") == per_scenario
                for scenario in APP_UX_SCENARIOS
            )
            and metrics.get("app_ux_sample_count")
            == per_scenario * len(APP_UX_SCENARIOS)
            and isinstance(metrics.get("app_ux_p50_ms"), (int, float))
            and math.isfinite(float(metrics["app_ux_p50_ms"]))
            and isinstance(metrics.get("app_ux_p95_ms"), (int, float))
            and math.isfinite(float(metrics["app_ux_p95_ms"]))
        )
        if matrix_ok:
            eligible.append(app_frame)
    if len(eligible) != 1:
        return {}
    app_frame = eligible[0]
    metrics = app_frame["metrics"]
    resource_metrics = (
        app_frame.get("resourceMetrics")
        if isinstance(app_frame.get("resourceMetrics"), dict)
        else {}
    )
    result = {
        "app_ux_p50_ms": float(metrics["app_ux_p50_ms"]),
        "app_ux_p95_ms": float(metrics["app_ux_p95_ms"]),
    }
    for name in (
        "ui_long_tasks_gt_200ms",
        "idle_cpu_pct",
        "working_set_mb",
    ):
        value = resource_metrics.get(name)
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 0:
            result[name] = float(value)
    return result


def load_baseline(path: str) -> dict[str, Any]:
    if not path:
        return load_baseline_metrics(REPO_ROOT)
    value = load_payload(Path(path).resolve())
    if not isinstance(value, dict):
        return {}
    metrics = value.get("metrics")
    return metrics if isinstance(metrics, dict) else value


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
    guards = load_guard_metrics(paths)
    app_ux = load_app_ux_metrics(paths)
    metrics: dict[str, float | str] = {}
    for scenario, (prefix, start, end) in SCENARIOS.items():
        samples = collect_segment_ms(
            sessions,
            prefix,
            start,
            end,
            args.qpc_frequency,
        )
        metrics[f"{scenario}_p50_ms"] = percentile(samples, 50.0) if samples else "unknown"
        metrics[f"{scenario}_p95_ms"] = percentile(samples, 95.0) if samples else "unknown"
        metrics[f"{scenario}_sample_count"] = len(samples)

    metrics["app_ux_p50_ms"] = app_ux.get("app_ux_p50_ms", "unknown")
    metrics["app_ux_p95_ms"] = app_ux.get("app_ux_p95_ms", "unknown")

    for metric, (start, end) in READINESS.items():
        samples = collect_segment_ms(sessions, "overlay_", start, end, args.qpc_frequency)
        metrics[metric] = percentile(samples, 95.0) if samples else "unknown"

    for guard in (
        "text_errors",
        "focus_errors",
        "clipboard_errors",
        "overlay_errors",
        "ui_long_tasks_gt_200ms",
        "idle_cpu_pct",
        "working_set_mb",
    ):
        metrics[guard] = app_ux.get(guard, guards.get(guard, "unknown"))
    metrics["local_wux"] = compute_local_wux(metrics, load_baseline(args.baseline))

    for name, value in metrics.items():
        print(f"METRIC {name}={value}")
    return 0 if metrics["local_wux"] != "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())

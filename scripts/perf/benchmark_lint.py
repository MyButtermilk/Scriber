from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path


REQUIRED_METRICS = [
    "local_wux",
    "overlay_warm_p95_ms",
    "overlay_cold_p95_ms",
    "microsoft_local_tail_p95_ms",
    "soniox_local_tail_p95_ms",
    "app_ux_p95_ms",
    "hotkey_mic_ready_p95_ms",
    "hotkey_first_audio_frame_p95_ms",
    "text_errors",
    "focus_errors",
    "clipboard_errors",
    "overlay_errors",
    "ui_long_tasks_gt_200ms",
    "idle_cpu_pct",
    "working_set_mb",
]

INTEGER_METRICS = {
    "text_errors",
    "focus_errors",
    "clipboard_errors",
    "overlay_errors",
    "ui_long_tasks_gt_200ms",
}

METRIC_RE = re.compile(r"^METRIC\s+([A-Za-z0-9_]+)=([^\s]+)\s*$")


def read_input(path: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    return sys.stdin.read()


def parse_metrics(text: str) -> tuple[dict[str, str], list[str]]:
    metrics: dict[str, str] = {}
    errors: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.startswith("METRIC "):
            continue
        match = METRIC_RE.match(line.strip())
        if not match:
            errors.append(f"line {line_no}: malformed METRIC line")
            continue
        name, value = match.groups()
        if name in metrics:
            errors.append(f"line {line_no}: duplicate metric {name}")
        metrics[name] = value
    return metrics, errors


def validate_value(name: str, value: str, *, allow_unknown: bool) -> str | None:
    if value.lower() == "unknown":
        return None if allow_unknown else f"{name} is unknown"
    try:
        parsed = float(value)
    except ValueError:
        return f"{name} is not numeric: {value!r}"
    if not math.isfinite(parsed):
        return f"{name} is not finite: {value!r}"
    if name in INTEGER_METRICS and int(parsed) != parsed:
        return f"{name} must be an integer"
    if name == "local_wux" and parsed <= 0:
        return "local_wux must be positive"
    if name.endswith("_ms") and parsed < 0:
        return f"{name} must be non-negative"
    if name in {"idle_cpu_pct", "working_set_mb"} and parsed < 0:
        return f"{name} must be non-negative"
    return None


def lint(text: str, *, allow_unknown: bool = False) -> list[str]:
    metrics, errors = parse_metrics(text)
    for name in REQUIRED_METRICS:
        if name not in metrics:
            errors.append(f"missing metric {name}")
            continue
        value_error = validate_value(name, metrics[name], allow_unknown=allow_unknown)
        if value_error:
            errors.append(value_error)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Scriber autoresearch METRIC output.")
    parser.add_argument("--input", default="")
    parser.add_argument("--allow-unknown", action="store_true")
    args = parser.parse_args(argv)

    errors = lint(read_input(args.input), allow_unknown=args.allow_unknown)
    if errors:
        print("benchmark-lint: failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("benchmark-lint: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

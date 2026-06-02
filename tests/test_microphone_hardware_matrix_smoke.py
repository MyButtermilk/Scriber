from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_microphone_hardware_matrix import (
    Device,
    evaluate_expectations,
    summarize_change,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_plan_only_outputs_manual_hardware_matrix(tmp_path: Path) -> None:
    output = tmp_path / "hardware-plan.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "smoke_microphone_hardware_matrix.py"),
            "--plan-only",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["planOnly"] is True
    assert "usb-add" in payload["scenarios"]
    assert "dock-disconnect" in payload["scenarios"]
    assert "favorite-fallback" in payload["scenarios"]
    assert written == payload


def test_change_summary_tracks_added_removed_and_default_change() -> None:
    before = [
        Device("default", "Default"),
        Device("Built-in Mic", "Built-in Mic (Default)"),
        Device("Dock Mic", "Dock Mic"),
    ]
    after = [
        Device("default", "Default"),
        Device("Built-in Mic", "Built-in Mic"),
        Device("USB Mic", "USB Mic (Default)"),
    ]

    change = summarize_change(before, after)

    assert change["added"] == [{"deviceId": "USB Mic", "label": "USB Mic (Default)"}]
    assert change["removed"] == [{"deviceId": "Dock Mic", "label": "Dock Mic"}]
    assert change["defaultChanged"] is True


def test_expectation_evaluator_accepts_hardware_matrix_success() -> None:
    before = [
        Device("default", "Default"),
        Device("Built-in Mic", "Built-in Mic (Default)"),
    ]
    after = [
        Device("default", "Default"),
        Device("Built-in Mic", "Built-in Mic"),
        Device("USB Mic", "USB Mic (Default)"),
    ]

    failures = evaluate_expectations(
        before=before,
        after=after,
        settings_after={
            "micDevice": "Built-in Mic",
            "favoriteMic": "Dock Mic",
            "favoriteMicAvailable": False,
        },
        expect_added="usb",
        expect_removed="dock",
        expect_default_changed=True,
        expect_favorite_fallback=True,
    )

    assert failures == []


def test_expectation_evaluator_rejects_noop_without_explicit_expectation() -> None:
    devices = [
        Device("default", "Default"),
        Device("Built-in Mic", "Built-in Mic (Default)"),
    ]

    failures = evaluate_expectations(
        before=devices,
        after=devices,
        settings_after={},
        expect_added="",
        expect_removed="",
        expect_default_changed=False,
        expect_favorite_fallback=False,
    )

    assert failures == [
        "expected microphone list/default marker to change or an explicit expectation flag"
    ]

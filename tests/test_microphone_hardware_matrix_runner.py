from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_microphone_hardware_matrix.ps1"


def powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is required for the microphone hardware matrix runner.")
    return exe


def run_powershell(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script_env = os.environ.copy()
    if env:
        script_env.update(env)
    return subprocess.run(
        [powershell_exe(), *args],
        cwd=REPO_ROOT,
        env=script_env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_microphone_matrix_runner_powershell_parses() -> None:
    command = (
        "$tokens = $null; "
        "$errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseFile($env:SCRIPT_TO_PARSE, [ref]$tokens, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }; "
        "Write-Host 'OK'"
    )

    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
        env={"SCRIPT_TO_PARSE": str(RUNNER_SCRIPT)},
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_microphone_matrix_runner_plan_only_writes_redacted_plan(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(tmp_path),
        "-UsbLabel",
        "USB Mic",
        "-DockLabel",
        "Dock Mic",
        "-BluetoothLabel",
        "Bluetooth Headset",
        "-FavoriteLabel",
        "Favorite Mic",
        "-Token",
        "real-session-token",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    plan_path = tmp_path / "microphone-hardware-matrix-runner-plan.json"
    assert not plan_path.read_bytes().startswith(b"\xef\xbb\xbf")
    written = json.loads(plan_path.read_text(encoding="utf-8"))
    scenarios = [entry["scenario"] for entry in payload["scenarios"]]
    assert scenarios == [
        "usb-add",
        "usb-remove",
        "dock-disconnect",
        "dock-connect",
        "bluetooth-add",
        "bluetooth-remove",
        "default-mic-change",
        "favorite-fallback",
    ]
    assert payload["ok"] is True
    assert payload["planOnly"] is True
    assert payload["readyForPhysicalRun"] is True
    assert payload["forceRefreshEachPoll"] is False
    assert payload["requireRustEndpointInventory"] is False
    assert payload["requireDeviceRefreshEvidence"] is False
    assert payload["missingLabelParameters"] == []
    assert "validate_microphone_hardware_matrix.py" in payload["validationCommand"]
    assert "--force-refresh-each-poll" not in result.stdout
    assert "real-session-token" not in result.stdout
    assert any("<session token>" in entry["command"] for entry in payload["scenarios"])
    assert written == payload


def test_microphone_matrix_runner_plan_only_can_require_rust_endpoint_inventory(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(tmp_path),
        "-UsbLabel",
        "USB Mic",
        "-DockLabel",
        "Dock Mic",
        "-BluetoothLabel",
        "Bluetooth Headset",
        "-FavoriteLabel",
        "Favorite Mic",
        "-RequireRustEndpointInventory",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "--require-rust-endpoint-inventory" in payload["validationCommand"]


def test_microphone_matrix_runner_plan_only_can_require_device_refresh_evidence(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(tmp_path),
        "-UsbLabel",
        "USB Mic",
        "-DockLabel",
        "Dock Mic",
        "-BluetoothLabel",
        "Bluetooth Headset",
        "-FavoriteLabel",
        "Favorite Mic",
        "-RequireDeviceRefreshEvidence",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["requireDeviceRefreshEvidence"] is True
    assert "--require-device-refresh-evidence" in payload["validationCommand"]


def test_microphone_matrix_runner_plan_only_can_use_legacy_forced_refresh(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(tmp_path),
        "-UsbLabel",
        "USB Mic",
        "-DockLabel",
        "Dock Mic",
        "-BluetoothLabel",
        "Bluetooth Headset",
        "-FavoriteLabel",
        "Favorite Mic",
        "-ForceRefreshEachPoll",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["forceRefreshEachPoll"] is True
    assert any("--force-refresh-each-poll" in entry["command"] for entry in payload["scenarios"])


def test_microphone_matrix_runner_plan_only_reports_missing_labels(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["planOnly"] is True
    assert payload["readyForPhysicalRun"] is False
    assert payload["missingLabelParameters"] == [
        "UsbLabel",
        "DockLabel",
        "BluetoothLabel",
        "FavoriteLabel",
    ]
    usb_add = next(entry for entry in payload["scenarios"] if entry["scenario"] == "usb-add")
    assert "<UsbLabel>" in usb_add["command"]
    assert '--expect-added ""' not in usb_add["command"]


def test_microphone_matrix_runner_requires_labels_before_backend_access(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-OutputDir",
        str(tmp_path),
    )

    assert result.returncode == 1
    assert "Missing -UsbLabel" in result.stderr


def test_microphone_matrix_runner_wires_smoke_and_validation_scripts() -> None:
    script = RUNNER_SCRIPT.read_text(encoding="utf-8")

    assert "scripts\\smoke_microphone_hardware_matrix.py" in script
    assert "scripts\\validate_microphone_hardware_matrix.py" in script
    assert "microphone-hardware-matrix-validation.json" in script
    assert "microphone-hardware-matrix-runner-plan.json" in script
    for scenario in [
        "usb-add",
        "usb-remove",
        "dock-disconnect",
        "dock-connect",
        "bluetooth-add",
        "bluetooth-remove",
        "default-mic-change",
        "favorite-fallback",
    ]:
        assert scenario in script

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_microphone_hardware_matrix import DEFAULT_SCENARIOS
from scripts.validate_microphone_hardware_matrix import validate_matrix


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_artifact(
    directory: Path,
    scenario: str,
    *,
    expectations: dict[str, object],
    change: dict[str, object],
    rust_change: dict[str, object] | None = None,
    device_refresh: dict[str, object] | None = None,
    settings_after: dict[str, object] | None = None,
    ok: bool = True,
    plan_only: bool = False,
    assume_completed: bool = True,
) -> None:
    payload = {
        "ok": ok,
        "baseUrl": "http://127.0.0.1:8765",
        "scenarios": [scenario],
        "planOnly": plan_only,
        "assumeCompleted": assume_completed,
        "instructions": [],
        "plan": [],
        "result": {
            "before": [],
            "after": [],
            "settingsBefore": {},
            "settingsAfter": settings_after or {},
            "change": change,
            "rustNativeEndpointInventoryChange": rust_change,
            "deviceMonitorRefresh": device_refresh,
            "expectations": expectations,
            "failures": [],
        },
    }
    (directory / f"microphone-hardware-{scenario}.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def write_success_matrix(directory: Path) -> None:
    write_artifact(
        directory,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "usb-remove",
        expectations={"expectAdded": "", "expectRemoved": "usb", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [], "removed": [{"deviceId": "USB Mic", "label": "USB Mic"}], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "dock-disconnect",
        expectations={"expectAdded": "", "expectRemoved": "dock", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [], "removed": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "dock-connect",
        expectations={"expectAdded": "dock", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "removed": [], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "bluetooth-add",
        expectations={"expectAdded": "bluetooth", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "removed": [], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "bluetooth-remove",
        expectations={"expectAdded": "", "expectRemoved": "bluetooth", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [], "removed": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "defaultChanged": False},
    )
    write_artifact(
        directory,
        "default-mic-change",
        expectations={"expectAdded": "", "expectRemoved": "", "expectDefaultChanged": True, "expectFavoriteFallback": False},
        change={"added": [], "removed": [], "defaultChanged": True},
    )
    write_artifact(
        directory,
        "favorite-fallback",
        expectations={"expectAdded": "", "expectRemoved": "favorite", "expectDefaultChanged": False, "expectFavoriteFallback": True},
        change={"added": [], "removed": [{"deviceId": "Favorite Mic", "label": "Favorite Mic"}], "defaultChanged": False},
        settings_after={"micDevice": "Built-in Mic", "favoriteMic": "Favorite Mic", "favoriteMicAvailable": False},
    )


def test_validate_matrix_accepts_all_physical_scenario_artifacts(tmp_path: Path) -> None:
    write_success_matrix(tmp_path)

    result = validate_matrix(input_dir=tmp_path)

    assert result["ok"] is True
    assert result["passedCount"] == len(DEFAULT_SCENARIOS)
    assert result["failedCount"] == 0


def test_validate_matrix_rejects_missing_scenario_artifact(tmp_path: Path) -> None:
    write_success_matrix(tmp_path)
    (tmp_path / "microphone-hardware-usb-add.json").unlink()

    result = validate_matrix(input_dir=tmp_path)

    assert result["ok"] is False
    usb_result = next(item for item in result["scenarios"] if item["scenario"] == "usb-add")
    assert usb_result["failures"] == ["missing artifact for scenario 'usb-add'"]


def test_validate_matrix_rejects_plan_only_and_placeholder_expectation(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "<usb label substring>", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        plan_only=True,
    )

    result = validate_matrix(input_dir=tmp_path, scenarios=["usb-add"])

    assert result["ok"] is False
    failures = result["scenarios"][0]["failures"]
    assert "plan-only artifact is not physical hardware evidence" in failures
    assert "expectAdded must be a real label substring" in failures


def test_validate_matrix_accepts_required_rust_endpoint_inventory(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        rust_change={
            "availableAfter": True,
            "sourceAfter": "rust-wasapi",
            "after": [
                {
                    "endpointIdHash": "usb-hash",
                    "friendlyName": "USB Mic",
                    "flow": "capture",
                    "isDefault": True,
                    "defaultRoles": ["console"],
                }
            ],
            "added": [
                {
                    "endpointIdHash": "usb-hash",
                    "friendlyName": "USB Mic",
                    "flow": "capture",
                    "isDefault": True,
                    "defaultRoles": ["console"],
                }
            ],
            "removed": [],
            "defaultChanged": False,
        },
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_rust_endpoint_inventory=True,
    )

    assert result["ok"] is True
    assert result["requireRustEndpointInventory"] is True


def test_validate_matrix_rejects_missing_required_rust_endpoint_inventory(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_rust_endpoint_inventory=True,
    )

    assert result["ok"] is False
    failures = result["scenarios"][0]["failures"]
    assert "result.rustNativeEndpointInventoryChange must be present" in failures


def test_validate_matrix_accepts_required_device_refresh_evidence(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        device_refresh={
            "availableAfter": True,
            "strategy": {"mode": "monitor-events", "forcedRefreshRequests": 0},
            "nativeEventsActiveAfter": True,
            "pollModeAfter": "native-event-safety",
            "pollIntervalSecondsAfter": 900,
            "pollRefreshDelta": 0,
            "eventRefreshDelta": 1,
            "portAudioRefreshDelta": 1,
            "nativeHintDelta": 1,
            "nativeHintPortAudioDelta": 1,
            "after": {
                "nativeEventsActive": True,
                "pollMode": "native-event-safety",
                "lastNativeHint": {"kind": "endpoint", "eventKind": "stateChanged"},
            },
        },
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_device_refresh_evidence=True,
    )

    assert result["ok"] is True
    assert result["requireDeviceRefreshEvidence"] is True


def test_validate_matrix_rejects_unredacted_hardware_evidence(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        rust_change={
            "availableAfter": True,
            "sourceAfter": "rust-wasapi",
            "after": [
                {
                    "endpointIdHash": "usb-hash",
                    "friendlyName": "USB Mic",
                    "flow": "capture",
                    "isDefault": True,
                    "defaultRoles": ["console"],
                }
            ],
            "added": [
                {
                    "endpointIdHash": "usb-hash",
                    "friendlyName": "USB Mic",
                    "flow": "capture",
                    "isDefault": True,
                    "defaultRoles": ["console"],
                    "diagnostics": {
                        "endpointId": r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-matrix-device}",
                    },
                }
            ],
            "removed": [],
            "defaultChanged": False,
        },
        device_refresh={
            "availableAfter": True,
            "strategy": {"mode": "monitor-events", "forcedRefreshRequests": 0},
            "nativeEventsActiveAfter": True,
            "pollModeAfter": "native-event-safety",
            "pollIntervalSecondsAfter": 900,
            "pollRefreshDelta": 0,
            "eventRefreshDelta": 1,
            "portAudioRefreshDelta": 1,
            "nativeHintDelta": 1,
            "nativeHintPortAudioDelta": 1,
            "after": {
                "nativeEventsActive": True,
                "pollMode": "native-event-safety",
                "lastNativeHint": {"kind": "endpoint", "eventKind": "stateChanged"},
                "debug": {
                    "framePipe": r"\\.\pipe\scriber-audio-matrix-secret",
                    "sessionToken": "raw-matrix-token",
                },
            },
        },
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_rust_endpoint_inventory=True,
        require_device_refresh_evidence=True,
    )

    assert result["ok"] is False
    failures = result["scenarios"][0]["failures"]
    assert (
        "artifact contains raw native endpoint ID at "
        "result.rustNativeEndpointInventoryChange.added[0].diagnostics.endpointId"
        in failures
    )
    assert (
        "artifact contains raw Scriber pipe name at result.deviceMonitorRefresh.after.debug.framePipe"
        in failures
    )
    assert (
        "artifact contains unredacted token-like value at result.deviceMonitorRefresh.after.debug.sessionToken"
        in failures
    )


def test_validate_matrix_rejects_forced_refresh_when_device_refresh_evidence_required(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        device_refresh={
            "availableAfter": True,
            "strategy": {"mode": "forced-refresh-each-poll", "forcedRefreshRequests": 5},
            "nativeEventsActiveAfter": False,
            "pollModeAfter": "fallback",
            "pollIntervalSecondsAfter": 60,
            "pollRefreshDelta": 5,
            "eventRefreshDelta": 0,
            "portAudioRefreshDelta": 5,
            "nativeHintDelta": 0,
            "nativeHintPortAudioDelta": 0,
            "after": {},
        },
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_device_refresh_evidence=True,
    )

    assert result["ok"] is False
    failures = result["scenarios"][0]["failures"]
    assert "device monitor refresh strategy must use monitor-events, not forced polling" in failures
    assert "device monitor refresh evidence must not use forced refresh requests" in failures
    assert "device monitor native events must be active after the hardware action" in failures


def test_validate_matrix_rejects_device_refresh_without_native_tauri_hint(tmp_path: Path) -> None:
    write_artifact(
        tmp_path,
        "usb-add",
        expectations={"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        change={"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        device_refresh={
            "availableAfter": True,
            "strategy": {"mode": "monitor-events", "forcedRefreshRequests": 0},
            "nativeEventsActiveAfter": True,
            "pollModeAfter": "native-event-safety",
            "pollIntervalSecondsAfter": 900,
            "pollRefreshDelta": 0,
            "eventRefreshDelta": 1,
            "portAudioRefreshDelta": 1,
            "nativeHintDelta": 0,
            "nativeHintPortAudioDelta": 0,
            "after": {
                "nativeEventsActive": True,
                "pollMode": "native-event-safety",
                "lastNativeHint": {"kind": "endpoint", "eventKind": "stateChanged"},
            },
        },
    )

    result = validate_matrix(
        input_dir=tmp_path,
        scenarios=["usb-add"],
        require_device_refresh_evidence=True,
    )

    assert result["ok"] is False
    failures = result["scenarios"][0]["failures"]
    assert "device monitor nativeHintDelta must show at least one native Tauri refresh hint" in failures
    assert (
        "device monitor nativeHintPortAudioDelta must show a native hint requested PortAudio refresh"
        in failures
    )


def test_validate_matrix_cli_writes_summary(tmp_path: Path) -> None:
    write_success_matrix(tmp_path)
    output = tmp_path / "matrix-summary.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_microphone_hardware_matrix.py"),
            "--input-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout_payload = json.loads(completed.stdout)
    written_payload = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_payload["ok"] is True
    assert written_payload == stdout_payload

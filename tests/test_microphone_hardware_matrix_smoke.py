from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import scripts.smoke_microphone_hardware_matrix as matrix_smoke
from scripts.smoke_microphone_hardware_matrix import (
    Device,
    evaluate_expectations,
    summarize_rust_inventory_change,
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
    assert len(payload["plan"]) == len(payload["scenarios"])

    usb_plan = next(item for item in payload["plan"] if item["scenario"] == "usb-add")
    assert usb_plan["expectationFlags"] == {"expectAdded": "<usb label substring>"}
    assert "After snapshot contains" in usb_plan["evidence"]
    assert "--scenario usb-add" in usb_plan["exampleCommand"]
    assert "--expect-added" in usb_plan["exampleCommand"]
    assert "microphone-hardware-usb-add.json" in usb_plan["exampleCommand"]

    favorite_plan = next(item for item in payload["plan"] if item["scenario"] == "favorite-fallback")
    assert favorite_plan["expectationFlags"]["expectFavoriteFallback"] is True
    assert "--expect-favorite-fallback" in favorite_plan["exampleCommand"]
    assert written == payload


def test_plan_only_token_placeholder_does_not_leak_real_token(tmp_path: Path) -> None:
    output = tmp_path / "hardware-plan-token.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "smoke_microphone_hardware_matrix.py"),
            "--scenario",
            "dock-connect",
            "--token",
            "real-session-token",
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
    command = payload["plan"][0]["exampleCommand"]
    assert payload["scenarios"] == ["dock-connect"]
    assert "--token" in command
    assert "<session token>" in command
    assert "real-session-token" not in result.stdout
    assert "real-session-token" not in output.read_text(encoding="utf-8")


def test_prompted_physical_run_records_operator_completion(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "microphone-hardware-usb-add.json"
    prompted: list[bool] = []

    class FakeClient:
        def __init__(self, base_url: str, token: str = "", timeout_sec: float = 5.0) -> None:
            self.base_url = base_url

        def get_microphones(self) -> list[Device]:
            return [Device("Built-in Mic", "Built-in Mic")]

        def get_settings(self) -> dict[str, object]:
            return {}

        def get_audio_diagnostics(self) -> dict[str, object]:
            return {
                "microphone": {
                    "rustNativeEndpointInventory": {
                        "available": True,
                        "source": "rust-wasapi",
                        "endpoints": [
                            {
                                "endpointIdHash": "built-in-hash",
                                "endpointId": r"SWD\MMDEVAPI\{raw-built-in}",
                                "friendlyName": "Built-in Mic",
                                "flow": "capture",
                                "isDefault": True,
                                "defaultRoles": ["console"],
                            }
                        ],
                    },
                    "nativeEndpointMapping": {
                        "mappings": [
                            {
                                "deviceIndex": 1,
                                "endpointId": r"SWD\MMDEVAPI\{raw-mapping}",
                                "endpointIdHash": "mapping-hash",
                            }
                        ]
                    },
                }
            }

    def fake_wait_for_condition(**_kwargs: object) -> tuple[list[Device], dict[str, object], list[str]]:
        return [Device("USB Mic", "USB Mic")], {}, []

    monkeypatch.setattr(matrix_smoke, "HttpClient", FakeClient)
    monkeypatch.setattr(matrix_smoke, "wait_for_condition", fake_wait_for_condition)
    monkeypatch.setattr("builtins.input", lambda: prompted.append(True) or "")

    result = matrix_smoke.main(
        [
            "--scenario",
            "usb-add",
            "--expect-added",
            "usb",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert prompted == [True]
    assert payload["planOnly"] is False
    assert payload["assumeCompleted"] is True
    rust_change = payload["result"]["rustNativeEndpointInventoryChange"]
    assert rust_change["availableAfter"] is True
    assert rust_change["sourceAfter"] == "rust-wasapi"
    assert rust_change["after"][0]["endpointIdHash"] == "built-in-hash"
    assert "endpointId" not in rust_change["after"][0]
    after_diagnostics = payload["result"]["audioDiagnosticsAfter"]
    assert "endpointId" not in after_diagnostics["rustNativeEndpointInventory"]["endpoints"][0]
    assert "endpointId" not in after_diagnostics["nativeEndpointMapping"]["mappings"][0]


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


def test_rust_inventory_summary_tracks_hash_changes_without_raw_ids() -> None:
    before = {
        "microphone": {
            "rustNativeEndpointInventory": {
                "available": True,
                "source": "rust-wasapi",
                "endpoints": [
                    {
                        "endpointIdHash": "built-in-hash",
                        "friendlyName": "Built-in Mic",
                        "flow": "capture",
                        "isDefault": True,
                        "defaultRoles": ["console"],
                    }
                ],
            }
        }
    }
    after = {
        "microphone": {
            "rustNativeEndpointInventory": {
                "available": True,
                "source": "rust-wasapi",
                "endpoints": [
                    {
                        "endpointIdHash": "built-in-hash",
                        "friendlyName": "Built-in Mic",
                        "flow": "capture",
                        "isDefault": False,
                        "defaultRoles": [],
                    },
                    {
                        "endpointIdHash": "usb-hash",
                        "friendlyName": "USB Mic",
                        "flow": "capture",
                        "isDefault": True,
                        "defaultRoles": ["console"],
                    },
                ],
            }
        }
    }

    change = summarize_rust_inventory_change(before, after)

    assert change["availableBefore"] is True
    assert change["availableAfter"] is True
    assert change["added"] == [
        {
            "endpointIdHash": "usb-hash",
            "friendlyName": "USB Mic",
            "flow": "capture",
            "isDefault": True,
            "defaultRoles": ["console"],
        }
    ]
    assert change["defaultChanged"] is True
    assert "endpointId" not in change["added"][0]


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

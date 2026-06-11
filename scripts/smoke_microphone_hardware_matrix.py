from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCENARIOS = [
    "usb-add",
    "usb-remove",
    "dock-disconnect",
    "dock-connect",
    "bluetooth-add",
    "bluetooth-remove",
    "default-mic-change",
    "favorite-fallback",
]


SCENARIO_INSTRUCTIONS = {
    "usb-add": "Plug in the USB microphone, wait until Windows exposes it, then press Enter.",
    "usb-remove": "Unplug the USB microphone, wait until Windows removes it, then press Enter.",
    "dock-disconnect": "Disconnect the laptop from the dock, wait for dock audio endpoints to disappear, then press Enter.",
    "dock-connect": "Reconnect the laptop to the dock, wait for dock audio endpoints to appear, then press Enter.",
    "bluetooth-add": "Connect the Bluetooth microphone/headset, wait until Windows exposes it, then press Enter.",
    "bluetooth-remove": "Disconnect the Bluetooth microphone/headset, wait until Windows removes it, then press Enter.",
    "default-mic-change": "Change the Windows default input device, wait for the default marker to move, then press Enter.",
    "favorite-fallback": "Make the configured favorite microphone unavailable, wait for Scriber to fall back, then press Enter.",
}


SCENARIO_EXPECTATION_HINTS = {
    "usb-add": {
        "flags": {"expectAdded": "<usb label substring>"},
        "evidence": "After snapshot contains a newly available USB microphone endpoint.",
    },
    "usb-remove": {
        "flags": {"expectRemoved": "<usb label substring>"},
        "evidence": "After snapshot no longer contains the removed USB microphone endpoint.",
    },
    "dock-disconnect": {
        "flags": {"expectRemoved": "<dock label substring>"},
        "evidence": "After snapshot no longer contains dock-provided microphone endpoints.",
    },
    "dock-connect": {
        "flags": {"expectAdded": "<dock label substring>"},
        "evidence": "After snapshot contains dock-provided microphone endpoints.",
    },
    "bluetooth-add": {
        "flags": {"expectAdded": "<bluetooth label substring>"},
        "evidence": "After snapshot contains the connected Bluetooth microphone/headset endpoint.",
    },
    "bluetooth-remove": {
        "flags": {"expectRemoved": "<bluetooth label substring>"},
        "evidence": "After snapshot no longer contains the disconnected Bluetooth microphone/headset endpoint.",
    },
    "default-mic-change": {
        "flags": {"expectDefaultChanged": True},
        "evidence": "Default marker moves to a different microphone endpoint.",
    },
    "favorite-fallback": {
        "flags": {"expectFavoriteFallback": True, "expectRemoved": "<favorite mic label substring>"},
        "evidence": "favoriteMicAvailable=false and micDevice falls back away from the unavailable favorite mic.",
    },
}


@dataclass(frozen=True)
class Device:
    device_id: str
    label: str


class HttpClient:
    def __init__(self, base_url: str, token: str = "", timeout_sec: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_sec = timeout_sec

    def request_json(self, method: str, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Scriber-Token"] = self.token
        request = urllib.request.Request(url, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {body}") from exc

    def get_microphones(self) -> list[Device]:
        payload = self.request_json("GET", "/api/microphones")
        return normalize_devices(payload.get("devices") or [])

    def refresh_microphones(self) -> dict[str, Any]:
        return self.request_json("POST", "/api/microphones/refresh")

    def get_settings(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/settings")

    def get_audio_diagnostics(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/runtime/audio-diagnostics")


def normalize_devices(raw_devices: list[Any]) -> list[Device]:
    devices: list[Device] = []
    for item in raw_devices:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("deviceId") or "")
        label = str(item.get("label") or "")
        if device_id or label:
            devices.append(Device(device_id=device_id, label=label))
    return devices


def device_signature(devices: list[Device]) -> list[dict[str, str]]:
    return [{"deviceId": device.device_id, "label": device.label} for device in devices]


def contains_text(device: Device, expected: str) -> bool:
    needle = expected.casefold()
    return needle in device.device_id.casefold() or needle in device.label.casefold()


def has_default_marker(device: Device) -> bool:
    return "(default)" in device.label.casefold()


def summarize_change(before: list[Device], after: list[Device]) -> dict[str, Any]:
    before_ids = {device.device_id for device in before}
    after_ids = {device.device_id for device in after}
    added = [device for device in after if device.device_id not in before_ids]
    removed = [device for device in before if device.device_id not in after_ids]
    before_default = next((device for device in before if has_default_marker(device)), None)
    after_default = next((device for device in after if has_default_marker(device)), None)
    return {
        "beforeCount": len(before),
        "afterCount": len(after),
        "added": device_signature(added),
        "removed": device_signature(removed),
        "beforeDefault": device_signature([before_default]) if before_default else [],
        "afterDefault": device_signature([after_default]) if after_default else [],
        "defaultChanged": bool(
            before_default
            and after_default
            and before_default.device_id != after_default.device_id
        ),
    }


def _rust_inventory_payload(audio_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(audio_diagnostics, dict):
        return {}
    microphone = audio_diagnostics.get("microphone")
    if not isinstance(microphone, dict):
        return {}
    inventory = microphone.get("rustNativeEndpointInventory")
    return inventory if isinstance(inventory, dict) else {}


def rust_inventory_signature(audio_diagnostics: dict[str, Any] | None) -> list[dict[str, Any]]:
    inventory = _rust_inventory_payload(audio_diagnostics)
    endpoints = inventory.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    result: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        endpoint_hash = str(endpoint.get("endpointIdHash") or "").strip()
        friendly_name = str(endpoint.get("friendlyName") or "").strip()
        if not endpoint_hash or not friendly_name:
            continue
        default_roles = endpoint.get("defaultRoles")
        if not isinstance(default_roles, list):
            default_roles = []
        result.append(
            {
                "endpointIdHash": endpoint_hash,
                "friendlyName": friendly_name,
                "flow": str(endpoint.get("flow") or ""),
                "isDefault": bool(endpoint.get("isDefault")),
                "defaultRoles": [
                    str(role) for role in default_roles if str(role or "").strip()
                ],
            }
        )
    return result


def rust_inventory_snapshot(audio_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    inventory = _rust_inventory_payload(audio_diagnostics)
    if not inventory:
        return {}
    snapshot: dict[str, Any] = {
        "available": bool(inventory.get("available")),
        "source": inventory.get("source"),
        "endpoints": rust_inventory_signature(audio_diagnostics),
    }
    error = str(inventory.get("error") or "").strip()
    if error:
        snapshot["error"] = error
    return snapshot


DEVICE_MONITOR_SNAPSHOT_FIELDS = (
    "nativeEventsSupported",
    "nativeEventsActive",
    "pollMode",
    "pollIntervalSeconds",
    "pollRefreshCount",
    "eventRefreshCount",
    "portAudioRefreshCount",
    "nativeHintCount",
    "nativeHintIgnoredCount",
    "nativeHintPortAudioCount",
    "lastPollRefreshAgoSeconds",
    "lastEventRefreshAgoSeconds",
    "lastNativeHintAgoSeconds",
    "lastDevicesChangedAgoSeconds",
    "pendingRefresh",
    "pendingRefreshRequiresPortAudio",
    "refreshDeferredUntilIdle",
    "deferredRefreshTrigger",
)


def _device_monitor_payload(audio_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(audio_diagnostics, dict):
        return {}
    microphone = audio_diagnostics.get("microphone")
    if not isinstance(microphone, dict):
        return {}
    monitor = microphone.get("deviceMonitor")
    return monitor if isinstance(monitor, dict) else {}


def device_monitor_snapshot(audio_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    monitor = _device_monitor_payload(audio_diagnostics)
    if not monitor:
        return {}
    snapshot = {
        key: monitor.get(key)
        for key in DEVICE_MONITOR_SNAPSHOT_FIELDS
        if key in monitor
    }
    raw_hint = monitor.get("lastNativeHint")
    if isinstance(raw_hint, dict):
        snapshot["lastNativeHint"] = {
            key: raw_hint.get(key)
            for key in (
                "kind",
                "eventKind",
                "flow",
                "forcePortAudioRefresh",
                "immediate",
                "reason",
            )
            if key in raw_hint
        }
    return snapshot


def _counter_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int | None:
    before_value = before.get(key)
    after_value = after.get(key)
    if not isinstance(before_value, (int, float)) or not isinstance(after_value, (int, float)):
        return None
    return int(after_value - before_value)


def summarize_device_monitor_refresh(
    before_audio: dict[str, Any] | None,
    after_audio: dict[str, Any] | None,
    *,
    forced_refresh_requests: int,
    force_refresh_each_poll: bool,
) -> dict[str, Any]:
    before = device_monitor_snapshot(before_audio)
    after = device_monitor_snapshot(after_audio)
    return {
        "availableBefore": bool(before),
        "availableAfter": bool(after),
        "strategy": {
            "mode": "forced-refresh-each-poll" if force_refresh_each_poll else "monitor-events",
            "forcedRefreshRequests": int(forced_refresh_requests),
        },
        "nativeEventsActiveBefore": before.get("nativeEventsActive"),
        "nativeEventsActiveAfter": after.get("nativeEventsActive"),
        "pollModeBefore": before.get("pollMode"),
        "pollModeAfter": after.get("pollMode"),
        "pollIntervalSecondsBefore": before.get("pollIntervalSeconds"),
        "pollIntervalSecondsAfter": after.get("pollIntervalSeconds"),
        "pollRefreshDelta": _counter_delta(before, after, "pollRefreshCount"),
        "eventRefreshDelta": _counter_delta(before, after, "eventRefreshCount"),
        "portAudioRefreshDelta": _counter_delta(before, after, "portAudioRefreshCount"),
        "nativeHintDelta": _counter_delta(before, after, "nativeHintCount"),
        "nativeHintPortAudioDelta": _counter_delta(before, after, "nativeHintPortAudioCount"),
        "pendingRefreshObserved": bool(before.get("pendingRefresh") or after.get("pendingRefresh")),
        "pendingRefreshRequiresPortAudioObserved": bool(
            before.get("pendingRefreshRequiresPortAudio")
            or after.get("pendingRefreshRequiresPortAudio")
        ),
        "refreshDeferredUntilIdleObserved": bool(
            before.get("refreshDeferredUntilIdle")
            or after.get("refreshDeferredUntilIdle")
        ),
        "before": before,
        "after": after,
    }


def omit_raw_endpoint_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: omit_raw_endpoint_ids(item)
            for key, item in value.items()
            if key != "endpointId"
        }
    if isinstance(value, list):
        return [omit_raw_endpoint_ids(item) for item in value]
    return value


def summarize_rust_inventory_change(
    before_audio: dict[str, Any] | None,
    after_audio: dict[str, Any] | None,
) -> dict[str, Any]:
    before_inventory = _rust_inventory_payload(before_audio)
    after_inventory = _rust_inventory_payload(after_audio)
    before = rust_inventory_signature(before_audio)
    after = rust_inventory_signature(after_audio)
    before_hashes = {str(endpoint["endpointIdHash"]) for endpoint in before}
    after_hashes = {str(endpoint["endpointIdHash"]) for endpoint in after}
    before_default = sorted(
        str(endpoint["endpointIdHash"]) for endpoint in before if endpoint.get("isDefault")
    )
    after_default = sorted(
        str(endpoint["endpointIdHash"]) for endpoint in after if endpoint.get("isDefault")
    )
    return {
        "availableBefore": bool(before_inventory.get("available")),
        "availableAfter": bool(after_inventory.get("available")),
        "sourceBefore": before_inventory.get("source"),
        "sourceAfter": after_inventory.get("source"),
        "beforeCount": len(before),
        "afterCount": len(after),
        "before": before,
        "after": after,
        "added": [
            endpoint for endpoint in after if endpoint["endpointIdHash"] not in before_hashes
        ],
        "removed": [
            endpoint
            for endpoint in before
            if endpoint["endpointIdHash"] not in after_hashes
        ],
        "defaultChanged": before_default != after_default,
    }


def evaluate_expectations(
    *,
    before: list[Device],
    after: list[Device],
    settings_after: dict[str, Any],
    expect_added: str,
    expect_removed: str,
    expect_default_changed: bool,
    expect_favorite_fallback: bool,
) -> list[str]:
    failures: list[str] = []
    has_explicit_expectation = bool(
        expect_added
        or expect_removed
        or expect_default_changed
        or expect_favorite_fallback
    )
    if not has_explicit_expectation and device_signature(before) == device_signature(after):
        failures.append("expected microphone list/default marker to change or an explicit expectation flag")
    if expect_added and not any(contains_text(device, expect_added) for device in after):
        failures.append(f"expected added/available microphone containing '{expect_added}'")
    if expect_removed and any(contains_text(device, expect_removed) for device in after):
        failures.append(f"expected microphone containing '{expect_removed}' to be absent")
    if expect_default_changed:
        before_default = next((device for device in before if has_default_marker(device)), None)
        after_default = next((device for device in after if has_default_marker(device)), None)
        if not before_default or not after_default or before_default.device_id == after_default.device_id:
            failures.append("expected default input marker to move to a different device")
    if expect_favorite_fallback:
        favorite_available = settings_after.get("favoriteMicAvailable")
        mic_device = str(settings_after.get("micDevice") or "")
        favorite_mic = str(settings_after.get("favoriteMic") or "")
        if favorite_available is not False:
            failures.append("expected favoriteMicAvailable=false after favorite microphone became unavailable")
        if favorite_mic and mic_device == favorite_mic:
            failures.append("expected micDevice to fall back away from unavailable favoriteMic")
    return failures


def wait_for_condition(
    *,
    client: HttpClient,
    before: list[Device],
    timeout_sec: float,
    poll_sec: float,
    expect_added: str,
    expect_removed: str,
    expect_default_changed: bool,
    expect_favorite_fallback: bool,
    force_refresh_each_poll: bool,
) -> tuple[list[Device], dict[str, Any], list[str], int]:
    deadline = time.monotonic() + timeout_sec
    last_after = before
    last_settings: dict[str, Any] = {}
    last_failures: list[str] = ["no post-action sample collected"]
    forced_refresh_requests = 0
    while time.monotonic() <= deadline:
        if force_refresh_each_poll:
            client.refresh_microphones()
            forced_refresh_requests += 1
        time.sleep(max(0.05, poll_sec))
        last_after = client.get_microphones()
        last_settings = client.get_settings()
        last_failures = evaluate_expectations(
            before=before,
            after=last_after,
            settings_after=last_settings,
            expect_added=expect_added,
            expect_removed=expect_removed,
            expect_default_changed=expect_default_changed,
            expect_favorite_fallback=expect_favorite_fallback,
        )
        if not last_failures:
            return last_after, last_settings, [], forced_refresh_requests
    return last_after, last_settings, last_failures, forced_refresh_requests


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manual microphone hardware matrix smoke gate for Scriber backend REST endpoints.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", default="")
    parser.add_argument("--scenario", action="append", choices=DEFAULT_SCENARIOS)
    parser.add_argument("--instruction", default="")
    parser.add_argument("--expect-added", default="")
    parser.add_argument("--expect-removed", default="")
    parser.add_argument("--expect-default-changed", action="store_true")
    parser.add_argument("--expect-favorite-fallback", action="store_true")
    parser.add_argument("--wait-sec", type=float, default=30.0)
    parser.add_argument("--poll-sec", type=float, default=1.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--assume-completed", action="store_true")
    parser.add_argument(
        "--force-refresh-each-poll",
        action="store_true",
        help="Legacy fallback: explicitly POST /api/microphones/refresh in every poll iteration.",
    )
    return parser.parse_args(argv)


def write_output(payload: dict[str, Any], path: str) -> None:
    if not path:
        return
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def safe_audio_diagnostics(client: HttpClient) -> dict[str, Any]:
    try:
        return client.get_audio_diagnostics()
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def flag_args(flags: dict[str, Any]) -> list[str]:
    args: list[str] = []
    if flags.get("expectAdded"):
        args.extend(["--expect-added", str(flags["expectAdded"])])
    if flags.get("expectRemoved"):
        args.extend(["--expect-removed", str(flags["expectRemoved"])])
    if flags.get("expectDefaultChanged"):
        args.append("--expect-default-changed")
    if flags.get("expectFavoriteFallback"):
        args.append("--expect-favorite-fallback")
    return args


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def build_plan_entry(scenario: str, *, base_url: str, token_required: bool) -> dict[str, Any]:
    hints = SCENARIO_EXPECTATION_HINTS[scenario]
    flags = dict(hints["flags"])
    command_parts = [
        "python",
        "scripts\\smoke_microphone_hardware_matrix.py",
        "--scenario",
        scenario,
        "--base-url",
        base_url,
    ]
    if token_required:
        command_parts.extend(["--token", "<session token>"])
    command_parts.extend(flag_args(flags))
    command_parts.extend([
        "--output",
        f"tmp\\hybrid-baseline\\microphone-hardware-{scenario}.json",
    ])
    return {
        "scenario": scenario,
        "instruction": SCENARIO_INSTRUCTIONS[scenario],
        "expectationFlags": flags,
        "evidence": hints["evidence"],
        "exampleCommand": " ".join(quote_arg(part) for part in command_parts),
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scenarios = args.scenario or DEFAULT_SCENARIOS
    plan = [
        build_plan_entry(
            scenario,
            base_url=args.base_url,
            token_required=bool(args.token),
        )
        for scenario in scenarios
    ]
    payload: dict[str, Any] = {
        "ok": False,
        "baseUrl": args.base_url,
        "scenarios": scenarios,
        "planOnly": bool(args.plan_only),
        "assumeCompleted": False,
        "plan": plan,
        "instructions": [
            {"scenario": scenario, "instruction": SCENARIO_INSTRUCTIONS[scenario]}
            for scenario in scenarios
        ],
        "result": None,
    }

    if args.plan_only:
        payload["ok"] = True
        write_output(payload, args.output)
        print(json.dumps(payload, separators=(",", ":")))
        return 0

    client = HttpClient(args.base_url, token=args.token)
    before = client.get_microphones()
    settings_before = client.get_settings()
    audio_before = safe_audio_diagnostics(client)
    instruction = args.instruction or "Perform the planned hardware action, wait until Windows settles, then press Enter."
    if not args.assume_completed:
        print(instruction)
        input()
    payload["assumeCompleted"] = True

    after, settings_after, failures, forced_refresh_requests = wait_for_condition(
        client=client,
        before=before,
        timeout_sec=args.wait_sec,
        poll_sec=args.poll_sec,
        expect_added=args.expect_added,
        expect_removed=args.expect_removed,
        expect_default_changed=args.expect_default_changed,
        expect_favorite_fallback=args.expect_favorite_fallback,
        force_refresh_each_poll=bool(args.force_refresh_each_poll),
    )
    audio_after = safe_audio_diagnostics(client)
    result = {
        "before": device_signature(before),
        "after": device_signature(after),
        "settingsBefore": {
            "micDevice": settings_before.get("micDevice"),
            "favoriteMic": settings_before.get("favoriteMic"),
            "favoriteMicAvailable": settings_before.get("favoriteMicAvailable"),
        },
        "settingsAfter": {
            "micDevice": settings_after.get("micDevice"),
            "favoriteMic": settings_after.get("favoriteMic"),
            "favoriteMicAvailable": settings_after.get("favoriteMicAvailable"),
        },
        "change": summarize_change(before, after),
        "rustNativeEndpointInventoryChange": summarize_rust_inventory_change(
            audio_before,
            audio_after,
        ),
        "deviceMonitorRefresh": summarize_device_monitor_refresh(
            audio_before,
            audio_after,
            forced_refresh_requests=forced_refresh_requests,
            force_refresh_each_poll=bool(args.force_refresh_each_poll),
        ),
        "audioDiagnosticsBefore": {
            "rustNativeEndpointInventory": rust_inventory_snapshot(audio_before),
            "deviceMonitor": device_monitor_snapshot(audio_before),
            "nativeEndpointMapping": omit_raw_endpoint_ids(
                audio_before.get("microphone", {}).get("nativeEndpointMapping")
                if isinstance(audio_before.get("microphone"), dict)
                else None
            ),
        },
        "audioDiagnosticsAfter": {
            "rustNativeEndpointInventory": rust_inventory_snapshot(audio_after),
            "deviceMonitor": device_monitor_snapshot(audio_after),
            "nativeEndpointMapping": omit_raw_endpoint_ids(
                audio_after.get("microphone", {}).get("nativeEndpointMapping")
                if isinstance(audio_after.get("microphone"), dict)
                else None
            ),
        },
        "expectations": {
            "expectAdded": args.expect_added,
            "expectRemoved": args.expect_removed,
            "expectDefaultChanged": bool(args.expect_default_changed),
            "expectFavoriteFallback": bool(args.expect_favorite_fallback),
        },
        "failures": failures,
    }
    payload["result"] = result
    payload["ok"] = not failures
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

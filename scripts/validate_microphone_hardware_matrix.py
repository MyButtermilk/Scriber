from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_microphone_hardware_matrix import DEFAULT_SCENARIOS, SCENARIO_EXPECTATION_HINTS


REDACTED_TEXT_MARKERS = {"[REDACTED]", "[redacted]", "<redacted>", "***REDACTED***"}
REDACTED_ENDPOINT_MARKERS = {"[REDACTED_ENDPOINT]", "[redacted-endpoint]", "<redacted-endpoint>"}


@dataclass(frozen=True)
class ScenarioValidation:
    scenario: str
    path: str
    ok: bool
    failures: list[str]

    def to_public(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "path": self.path,
            "ok": self.ok,
            "failures": self.failures,
        }


def validate_matrix(
    *,
    input_dir: Path,
    scenarios: list[str] | None = None,
    require_rust_endpoint_inventory: bool = False,
    require_device_refresh_evidence: bool = False,
) -> dict[str, Any]:
    selected = scenarios or list(DEFAULT_SCENARIOS)
    results = [
        validate_scenario_artifact(
            input_dir,
            scenario,
            require_rust_endpoint_inventory=require_rust_endpoint_inventory,
            require_device_refresh_evidence=require_device_refresh_evidence,
        )
        for scenario in selected
    ]
    ok = bool(results) and all(result.ok for result in results)
    return {
        "ok": ok,
        "inputDir": str(input_dir),
        "requiredScenarios": selected,
        "requireRustEndpointInventory": bool(require_rust_endpoint_inventory),
        "requireDeviceRefreshEvidence": bool(require_device_refresh_evidence),
        "passedCount": sum(1 for result in results if result.ok),
        "failedCount": sum(1 for result in results if not result.ok),
        "scenarios": [result.to_public() for result in results],
    }


def validate_scenario_artifact(
    input_dir: Path,
    scenario: str,
    *,
    require_rust_endpoint_inventory: bool = False,
    require_device_refresh_evidence: bool = False,
) -> ScenarioValidation:
    path = input_dir / f"microphone-hardware-{scenario}.json"
    failures: list[str] = []
    if not path.is_file():
        return ScenarioValidation(
            scenario=scenario,
            path=str(path),
            ok=False,
            failures=[f"missing artifact for scenario '{scenario}'"],
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ScenarioValidation(
            scenario=scenario,
            path=str(path),
            ok=False,
            failures=[f"invalid JSON: {exc}"],
        )

    if not isinstance(payload, dict):
        failures.append("artifact root must be a JSON object")
        payload = {}
    else:
        failures.extend(_find_redaction_failures(payload))

    if payload.get("ok") is not True:
        failures.append("artifact ok must be true")
    if payload.get("planOnly") is True:
        failures.append("plan-only artifact is not physical hardware evidence")
    if payload.get("assumeCompleted") is not True:
        failures.append("physical artifact should record assumeCompleted=true after operator action")

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or scenario not in scenarios:
        failures.append(f"artifact scenarios must include '{scenario}'")

    result = payload.get("result")
    if not isinstance(result, dict):
        failures.append("artifact result must be present")
        result = {}

    run_failures = result.get("failures")
    if run_failures:
        failures.append(f"artifact result has failures: {run_failures}")

    expectations = result.get("expectations")
    if not isinstance(expectations, dict):
        failures.append("result.expectations must be present")
        expectations = {}
    settings_after = result.get("settingsAfter")
    if not isinstance(settings_after, dict):
        settings_after = {}
    change = result.get("change")
    if not isinstance(change, dict):
        failures.append("result.change must be present")
        change = {}

    failures.extend(_validate_required_expectations(scenario, expectations))
    failures.extend(_validate_change_evidence(scenario, expectations, change, settings_after))
    if require_rust_endpoint_inventory:
        failures.extend(_validate_rust_inventory_evidence(scenario, expectations, result))
    if require_device_refresh_evidence:
        failures.extend(_validate_device_refresh_evidence(result))

    return ScenarioValidation(
        scenario=scenario,
        path=str(path),
        ok=not failures,
        failures=failures,
    )


def _validate_rust_inventory_evidence(
    scenario: str,
    expectations: dict[str, Any],
    result: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    rust_change = result.get("rustNativeEndpointInventoryChange")
    if not isinstance(rust_change, dict):
        return ["result.rustNativeEndpointInventoryChange must be present"]
    if rust_change.get("availableAfter") is not True:
        failures.append("rust endpoint inventory must be available after the hardware action")
    if rust_change.get("sourceAfter") != "rust-wasapi":
        failures.append("rust endpoint inventory sourceAfter must be rust-wasapi")
    after = rust_change.get("after")
    if not isinstance(after, list) or not after:
        failures.append("rust endpoint inventory after snapshot must contain endpoints")
        after = []
    for endpoint in after:
        if not isinstance(endpoint, dict):
            failures.append("rust endpoint inventory endpoints must be objects")
            continue
        if not str(endpoint.get("endpointIdHash") or "").strip():
            failures.append("rust endpoint inventory endpointIdHash must be present")
        if "endpointId" in endpoint:
            failures.append("rust endpoint inventory must not expose raw endpointId")

    if scenario.endswith("-add") or scenario == "dock-connect":
        expected = str(expectations.get("expectAdded") or "")
        if not _rust_endpoints_contain(rust_change.get("added"), expected):
            failures.append("rust inventory added endpoints must contain the expected hardware label")
    if scenario.endswith("-remove") or scenario in {"dock-disconnect", "favorite-fallback"}:
        expected = str(expectations.get("expectRemoved") or "")
        if not _rust_endpoints_contain(rust_change.get("removed"), expected):
            failures.append("rust inventory removed endpoints must contain the expected hardware label")
    if scenario == "default-mic-change" and rust_change.get("defaultChanged") is not True:
        failures.append("rust inventory defaultChanged must be true")
    return failures


def _find_redaction_failures(value: Any) -> list[str]:
    failures: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_str = str(key)
                child_path = f"{path}.{key_str}" if path else key_str
                walk(item, child_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            if _looks_like_raw_scriber_pipe(node):
                failures.append(f"artifact contains raw Scriber pipe name at {path}")
            if _looks_like_raw_native_endpoint_id(node):
                failures.append(f"artifact contains raw native endpoint ID at {path}")
            if _looks_like_unredacted_endpoint_id_field(path, node):
                failures.append(f"artifact contains unredacted endpointId value at {path}")
            if _looks_like_unredacted_token_field(path, node):
                failures.append(f"artifact contains unredacted token-like value at {path}")

    walk(value, "")
    return failures


def _normalized_windows_identifier(value: str) -> str:
    normalized = str(value).lower().replace("/", "\\")
    for _ in range(6):
        collapsed = normalized.replace("\\\\", "\\")
        if collapsed == normalized:
            break
        normalized = collapsed
    return normalized


def _looks_like_raw_scriber_pipe(value: str) -> bool:
    return "\\.\\pipe\\scriber-" in _normalized_windows_identifier(value)


def _looks_like_raw_native_endpoint_id(value: str) -> bool:
    normalized = _normalized_windows_identifier(value)
    return "swd\\mmdevapi\\" in normalized or "swd#mmdevapi#" in str(value).lower()


def _looks_like_unredacted_endpoint_id_field(path: str, value: str) -> bool:
    tokens = [token for token in re.split(r"[.\[\]]+", path.lower()) if token]
    if not any(token.endswith("endpointid") and not token.endswith("hash") for token in tokens):
        return False
    normalized = str(value).strip()
    return bool(normalized) and normalized not in REDACTED_ENDPOINT_MARKERS


def _looks_like_unredacted_token_field(path: str, value: str) -> bool:
    path_lower = path.lower()
    if "tokenconfigured" in path_lower:
        return False
    if "token" not in path_lower:
        return False
    normalized = str(value).strip()
    return bool(normalized) and normalized not in REDACTED_TEXT_MARKERS


def _validate_device_refresh_evidence(result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    refresh = result.get("deviceMonitorRefresh")
    if not isinstance(refresh, dict):
        return ["result.deviceMonitorRefresh must be present"]
    if refresh.get("availableAfter") is not True:
        failures.append("device monitor refresh evidence must be available after the hardware action")

    strategy = refresh.get("strategy")
    if not isinstance(strategy, dict):
        failures.append("device monitor refresh strategy must be present")
        strategy = {}
    if strategy.get("mode") != "monitor-events":
        failures.append("device monitor refresh strategy must use monitor-events, not forced polling")
    if strategy.get("forcedRefreshRequests") != 0:
        failures.append("device monitor refresh evidence must not use forced refresh requests")

    if refresh.get("nativeEventsActiveAfter") is not True:
        failures.append("device monitor native events must be active after the hardware action")
    if refresh.get("pollModeAfter") != "native-event-safety":
        failures.append("device monitor pollModeAfter must be native-event-safety")
    poll_interval = refresh.get("pollIntervalSecondsAfter")
    if not isinstance(poll_interval, (int, float)) or poll_interval < 300:
        failures.append("device monitor native safety poll interval must be at least 300 seconds")

    for key in (
        "pollRefreshDelta",
        "eventRefreshDelta",
        "portAudioRefreshDelta",
        "nativeHintDelta",
        "nativeHintPortAudioDelta",
    ):
        value = refresh.get(key)
        if not isinstance(value, int):
            failures.append(f"device monitor {key} must be recorded as an integer")
    poll_delta = refresh.get("pollRefreshDelta")
    if isinstance(poll_delta, int) and poll_delta > 1:
        failures.append("device monitor pollRefreshDelta must not show repeated polling during the scenario")
    event_delta = refresh.get("eventRefreshDelta")
    if isinstance(event_delta, int) and event_delta < 1:
        failures.append("device monitor eventRefreshDelta must show at least one native event refresh")
    portaudio_delta = refresh.get("portAudioRefreshDelta")
    if isinstance(portaudio_delta, int) and portaudio_delta < 1:
        failures.append("device monitor portAudioRefreshDelta must show the event-triggered PortAudio refresh")
    native_hint_delta = refresh.get("nativeHintDelta")
    if isinstance(native_hint_delta, int) and native_hint_delta < 1:
        failures.append("device monitor nativeHintDelta must show at least one native Tauri refresh hint")
    native_hint_portaudio_delta = refresh.get("nativeHintPortAudioDelta")
    if isinstance(native_hint_portaudio_delta, int) and native_hint_portaudio_delta < 1:
        failures.append("device monitor nativeHintPortAudioDelta must show a native hint requested PortAudio refresh")

    after = refresh.get("after")
    if not isinstance(after, dict):
        failures.append("device monitor refresh after snapshot must be present")
    elif "lastNativeHint" in after:
        hint = after.get("lastNativeHint")
        if isinstance(hint, dict) and any("endpointId" in key for key in hint):
            failures.append("device monitor lastNativeHint must not expose raw endpoint IDs")
    return failures


def _validate_required_expectations(scenario: str, expectations: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    required_flags = SCENARIO_EXPECTATION_HINTS[scenario]["flags"]
    if "expectAdded" in required_flags:
        expected = str(expectations.get("expectAdded") or "")
        if _is_placeholder_or_empty(expected):
            failures.append("expectAdded must be a real label substring")
    if "expectRemoved" in required_flags:
        expected = str(expectations.get("expectRemoved") or "")
        if _is_placeholder_or_empty(expected):
            failures.append("expectRemoved must be a real label substring")
    if required_flags.get("expectDefaultChanged") and expectations.get("expectDefaultChanged") is not True:
        failures.append("expectDefaultChanged must be true")
    if required_flags.get("expectFavoriteFallback") and expectations.get("expectFavoriteFallback") is not True:
        failures.append("expectFavoriteFallback must be true")
    return failures


def _validate_change_evidence(
    scenario: str,
    expectations: dict[str, Any],
    change: dict[str, Any],
    settings_after: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    if scenario.endswith("-add") or scenario == "dock-connect":
        expected = str(expectations.get("expectAdded") or "")
        if not _devices_contain(change.get("added"), expected):
            failures.append("change.added must contain the expected hardware label")
    if scenario.endswith("-remove") or scenario in {"dock-disconnect", "favorite-fallback"}:
        expected = str(expectations.get("expectRemoved") or "")
        if not _devices_contain(change.get("removed"), expected):
            failures.append("change.removed must contain the expected hardware label")
    if scenario == "default-mic-change" and change.get("defaultChanged") is not True:
        failures.append("change.defaultChanged must be true")
    if scenario == "favorite-fallback":
        if settings_after.get("favoriteMicAvailable") is not False:
            failures.append("settingsAfter.favoriteMicAvailable must be false")
        favorite_mic = str(settings_after.get("favoriteMic") or "")
        mic_device = str(settings_after.get("micDevice") or "")
        if favorite_mic and mic_device == favorite_mic:
            failures.append("settingsAfter.micDevice must fall back away from favoriteMic")
    return failures


def _devices_contain(raw_devices: Any, expected: str) -> bool:
    if _is_placeholder_or_empty(expected) or not isinstance(raw_devices, list):
        return False
    needle = expected.casefold()
    for item in raw_devices:
        if not isinstance(item, dict):
            continue
        haystack = f"{item.get('deviceId') or ''} {item.get('label') or ''}".casefold()
        if needle in haystack:
            return True
    return False


def _rust_endpoints_contain(raw_endpoints: Any, expected: str) -> bool:
    if _is_placeholder_or_empty(expected) or not isinstance(raw_endpoints, list):
        return False
    needle = expected.casefold()
    for item in raw_endpoints:
        if not isinstance(item, dict):
            continue
        haystack = f"{item.get('friendlyName') or ''} {item.get('endpointIdHash') or ''}".casefold()
        if needle in haystack:
            return True
    return False


def _is_placeholder_or_empty(value: str) -> bool:
    stripped = (value or "").strip()
    return not stripped or stripped.startswith("<") or stripped.endswith(">")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate completed physical microphone hardware matrix artifacts.",
    )
    parser.add_argument("--input-dir", default="tmp/hybrid-baseline")
    parser.add_argument("--scenario", action="append", choices=DEFAULT_SCENARIOS)
    parser.add_argument("--require-rust-endpoint-inventory", action="store_true")
    parser.add_argument("--require-device-refresh-evidence", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def write_output(payload: dict[str, Any], path: str) -> None:
    if not path:
        return
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    payload = validate_matrix(
        input_dir=Path(args.input_dir).expanduser().resolve(),
        scenarios=args.scenario,
        require_rust_endpoint_inventory=bool(args.require_rust_endpoint_inventory),
        require_device_refresh_evidence=bool(args.require_device_refresh_evidence),
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

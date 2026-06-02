from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_microphone_hardware_matrix import DEFAULT_SCENARIOS, SCENARIO_EXPECTATION_HINTS


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
) -> dict[str, Any]:
    selected = scenarios or list(DEFAULT_SCENARIOS)
    results = [validate_scenario_artifact(input_dir, scenario) for scenario in selected]
    ok = bool(results) and all(result.ok for result in results)
    return {
        "ok": ok,
        "inputDir": str(input_dir),
        "requiredScenarios": selected,
        "passedCount": sum(1 for result in results if result.ok),
        "failedCount": sum(1 for result in results if not result.ok),
        "scenarios": [result.to_public() for result in results],
    }


def validate_scenario_artifact(input_dir: Path, scenario: str) -> ScenarioValidation:
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

    return ScenarioValidation(
        scenario=scenario,
        path=str(path),
        ok=not failures,
        failures=failures,
    )


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


def _is_placeholder_or_empty(value: str) -> bool:
    stripped = (value or "").strip()
    return not stripped or stripped.startswith("<") or stripped.endswith(">")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate completed physical microphone hardware matrix artifacts.",
    )
    parser.add_argument("--input-dir", default="tmp/hybrid-baseline")
    parser.add_argument("--scenario", action="append", choices=DEFAULT_SCENARIOS)
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
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.validate_hybrid_release_readiness import (
    OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS,
    REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS,
    validate_tauri_text_injection_matrix_report,
)


def default_input_dir() -> Path:
    return _REPO_ROOT / "tmp" / "hybrid-baseline" / "tauri-text-injection"


def default_output_path() -> Path:
    return _REPO_ROOT / "tmp" / "hybrid-baseline" / "tauri-text-injection-matrix.json"


def parse_mapping(raw_values: list[str], *, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in raw_values:
        if "=" not in raw:
            raise ValueError(f"{label} must use id=value format: {raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(f"{label} must include a non-empty id and value: {raw}")
        values[key] = value
    return values


def candidate_report_paths(input_dir: Path, scenario_id: str) -> list[Path]:
    return [
        input_dir / f"tauri-text-injection-{scenario_id}.json",
        input_dir / f"{scenario_id}.json",
    ]


def resolve_report_path(
    scenario_id: str,
    *,
    input_dir: Path,
    explicit_reports: dict[str, str],
) -> Path | None:
    if scenario_id in explicit_reports:
        return Path(explicit_reports[scenario_id]).expanduser().resolve()
    for candidate in candidate_report_paths(input_dir, scenario_id):
        if candidate.is_file():
            return candidate.resolve()
    return None


def read_json_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} root must be a JSON object")
    return payload


def build_matrix_payload(
    *,
    input_dir: Path,
    explicit_reports: dict[str, str],
    unsupported_optional: dict[str, str],
) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    missing_reports: list[str] = []
    load_errors: list[str] = []
    known_labels = {
        **REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS,
        **OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS,
    }
    scenario_ids = list(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS)
    scenario_ids.extend(
        scenario_id
        for scenario_id in OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS
        if scenario_id in unsupported_optional
        or resolve_report_path(
            scenario_id,
            input_dir=input_dir,
            explicit_reports=explicit_reports,
        )
        is not None
    )

    for scenario_id in scenario_ids:
        label = known_labels.get(scenario_id, scenario_id)
        if scenario_id in unsupported_optional:
            scenarios.append(
                {
                    "id": scenario_id,
                    "label": label,
                    "unsupported": True,
                    "unsupportedReason": unsupported_optional[scenario_id],
                }
            )
            continue

        report_path = resolve_report_path(
            scenario_id,
            input_dir=input_dir,
            explicit_reports=explicit_reports,
        )
        if report_path is None:
            missing_reports.append(scenario_id)
            continue
        try:
            report = read_json_report(report_path)
        except Exception as exc:
            load_errors.append(f"{scenario_id}: {exc}")
            continue
        scenarios.append(
            {
                "id": scenario_id,
                "label": label,
                "reportPath": str(report_path),
                "report": report,
            }
        )

    unknown_explicit = sorted(set(explicit_reports).difference(known_labels))
    for scenario_id in unknown_explicit:
        report_path = resolve_report_path(
            scenario_id,
            input_dir=input_dir,
            explicit_reports=explicit_reports,
        )
        if report_path is None:
            missing_reports.append(scenario_id)
            continue
        try:
            report = read_json_report(report_path)
        except Exception as exc:
            load_errors.append(f"{scenario_id}: {exc}")
            continue
        scenarios.append(
            {
                "id": scenario_id,
                "label": scenario_id,
                "reportPath": str(report_path),
                "report": report,
            }
        )

    unknown_unsupported = sorted(set(unsupported_optional).difference(OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS))
    load_errors.extend(
        f"{scenario_id}: unsupported markers are only allowed for optional scenarios"
        for scenario_id in unknown_unsupported
    )

    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "tauri",
        "ok": not missing_reports and not load_errors,
        "inputDir": str(input_dir),
        "requiredScenarioIds": list(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
        "optionalScenarioIds": list(OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
        "missingReports": missing_reports,
        "loadErrors": load_errors,
        "scenarios": scenarios,
        "summary": {
            "scenarioCount": len(scenarios),
            "requiredScenarioCount": len(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
            "optionalScenarioCount": len(OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
            "missingReportCount": len(missing_reports),
            "loadErrorCount": len(load_errors),
        },
    }


def write_matrix(payload: dict[str, Any], output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    check = validate_tauri_text_injection_matrix_report(output_path, required=True)
    payload["ok"] = payload.get("ok") is True and check.ok
    payload["validationFailures"] = check.failures
    payload["validationDetails"] = check.details
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate real Tauri text-injection target-app smoke reports into the release matrix artifact.",
    )
    parser.add_argument("--input-dir", default=str(default_input_dir()))
    parser.add_argument("--output", default=str(default_output_path()))
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="Explicit report path for a matrix scenario. May be repeated.",
    )
    parser.add_argument(
        "--unsupported-optional",
        action="append",
        default=[],
        metavar="ID=REASON",
        help="Mark an optional scenario, such as remote-desktop, unavailable with a reason.",
    )
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="Write the matrix artifact and return success even when validation fails.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        explicit_reports = parse_mapping(args.scenario, label="--scenario")
        unsupported_optional = parse_mapping(args.unsupported_optional, label="--unsupported-optional")
        payload = build_matrix_payload(
            input_dir=Path(args.input_dir).expanduser().resolve(),
            explicit_reports=explicit_reports,
            unsupported_optional=unsupported_optional,
        )
        payload = write_matrix(payload, Path(args.output).expanduser().resolve())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")))
        return 2

    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload.get("ok") or args.allow_invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())

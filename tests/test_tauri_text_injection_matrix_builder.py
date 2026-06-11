from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_hybrid_release_readiness import REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_tauri_text_injection_matrix.py"


def valid_tauri_smoke_payload() -> dict:
    return {
        "schemaVersion": 1,
        "generatedAtUtc": "2026-06-11T12:00:00Z",
        "method": "tauri",
        "status": "passed",
        "ok": True,
        "callbackVerified": True,
        "targetTextVerified": True,
        "callbackElapsedMs": 10.0,
        "targetTextElapsedMs": 30.0,
        "capturedChars": 42,
        "targetError": "",
        "expectedChars": 42,
        "targetFocus": {"attempted": True, "ok": True},
        "shellIpc": {
            "available": True,
            "lastCommand": "injectText",
            "lastSuccess": True,
            "lastErrorCode": None,
            "lastResponse": {
                "success": True,
                "payload": {
                    "method": "tauri",
                    "markers": ["clipboard_set", "paste"],
                    "timingsMs": {
                        "clipboardSet": 1.0,
                        "pasteDispatch": 2.0,
                        "total": 3.0,
                    },
                },
            },
        },
    }


def write_required_smokes(input_dir: Path, *, skip: set[str] | None = None) -> None:
    skip = skip or set()
    input_dir.mkdir(parents=True, exist_ok=True)
    for scenario_id in REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS:
        if scenario_id in skip:
            continue
        (input_dir / f"tauri-text-injection-{scenario_id}.json").write_text(
            json.dumps(valid_tauri_smoke_payload()),
            encoding="utf-8",
        )


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_build_tauri_text_injection_matrix_accepts_complete_reports(tmp_path: Path) -> None:
    input_dir = tmp_path / "reports"
    output = tmp_path / "tauri-text-injection-matrix.json"
    write_required_smokes(input_dir)

    result = run_builder(
        "--input-dir",
        str(input_dir),
        "--output",
        str(output),
        "--unsupported-optional",
        "remote-desktop=Remote Desktop is not available on this test machine",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["validationFailures"] == []
    assert payload["summary"]["missingReportCount"] == 0
    assert len(payload["scenarios"]) == len(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS) + 1
    remote = next(scenario for scenario in payload["scenarios"] if scenario["id"] == "remote-desktop")
    assert remote["unsupported"] is True


def test_build_tauri_text_injection_matrix_fails_when_required_report_is_missing(tmp_path: Path) -> None:
    input_dir = tmp_path / "reports"
    output = tmp_path / "tauri-text-injection-matrix.json"
    write_required_smokes(input_dir, skip={"outlook"})

    result = run_builder("--input-dir", str(input_dir), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "outlook" in payload["missingReports"]
    assert any("missing required scenario(s): outlook" in failure for failure in payload["validationFailures"])


def test_build_tauri_text_injection_matrix_rejects_required_unsupported_marker(tmp_path: Path) -> None:
    input_dir = tmp_path / "reports"
    output = tmp_path / "tauri-text-injection-matrix.json"
    write_required_smokes(input_dir, skip={"notepad"})

    result = run_builder(
        "--input-dir",
        str(input_dir),
        "--output",
        str(output),
        "--unsupported-optional",
        "notepad=Not available",
    )

    assert result.returncode == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert any("required scenario notepad cannot be marked unsupported" in failure for failure in payload["validationFailures"])

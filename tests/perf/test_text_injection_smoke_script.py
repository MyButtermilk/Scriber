from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_text_injection_target import evaluate_result


def test_text_injection_smoke_validate_only_writes_artifact(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "text-injection-smoke.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_text_injection_target.py",
            "--validate-only",
            "--text",
            "Scriber validate injection text",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["status"] == "passed"
    assert payload["callbackVerified"] is True
    assert payload["targetTextVerified"] is True
    assert payload["targetFocus"] == {"attempted": False, "validateOnly": True}
    assert "shellIpc" in payload
    assert payload["validateOnly"] is True


def test_text_injection_smoke_validate_only_accepts_tauri_method(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "text-injection-tauri-smoke.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_text_injection_target.py",
            "--validate-only",
            "--method",
            "tauri",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["method"] == "tauri"
    assert "available" in payload["shellIpc"]
    assert payload["validateOnly"] is True


def test_text_injection_smoke_classifies_callback_without_target_text() -> None:
    result = evaluate_result(
        expected_text="expected",
        callback_text="expected",
        target_text="",
        callback_elapsed_ms=12.0,
        target_elapsed_ms=None,
    )

    assert result["ok"] is False
    assert result["status"] == "callback_without_target_text"
    assert result["callbackVerified"] is True
    assert result["targetTextVerified"] is False


def test_text_injection_smoke_classifies_target_text_without_callback() -> None:
    result = evaluate_result(
        expected_text="expected",
        callback_text="",
        target_text="prefix expected suffix",
        callback_elapsed_ms=None,
        target_elapsed_ms=20.0,
    )

    assert result["ok"] is False
    assert result["status"] == "target_text_without_callback"
    assert result["callbackVerified"] is False
    assert result["targetTextVerified"] is True


def test_text_injection_smoke_uses_real_injector_and_safe_target_window() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "smoke_text_injection_target.py").read_text(
        encoding="utf-8"
    )

    assert "TextInjector" in script
    assert "InjectionTargetGuard" in script
    assert "Config.INJECT_TARGET_TITLE = args.target_title" in script
    assert 'os.environ["SCRIBER_INJECT_TARGET_TITLE"] = args.target_title' in script
    assert "target_guard=InjectionTargetGuard(title=args.target_title)" in script
    assert "injector._inject_text(args.text)" in script
    assert "powershell_text_target_command" in script
    assert "SCRIBER_TEXT_TARGET_OUTPUT" in script
    assert "click_target_window" in script
    assert "targetFocus" in script
    assert '"tauri"' in script
    assert "shell_ipc_snapshot" in script
    assert "--skip-target-click" in script
    assert "mouse_event" in script
    assert "callback_without_target_text" in script
    assert "target_text_without_callback" in script

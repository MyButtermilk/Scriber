from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ffmpeg" / "build_profile_b_msys2.ps1"


def test_profile_b_msys2_build_runner_contains_required_packages_and_gates() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "[switch]$InstallDependencies" in script
    assert "[switch]$PlanOnly" in script
    assert "[switch]$RunSidecarGate" in script
    assert "mingw-w64-ucrt-x86_64-gcc" in script
    assert "mingw-w64-ucrt-x86_64-opus" in script
    assert "mingw-w64-ucrt-x86_64-lame" in script
    assert "Copy-ProfileRuntimeDlls" in script
    assert "libmp3lame-0.dll" in script
    assert "libopus-0.dll" in script
    assert "pacman -S --needed --noconfirm" in script
    assert "create_profile_b_build_kit.py" in script
    assert "validate_ffmpeg_profile.py" in script
    assert "--require-lgpl" in script
    assert "smoke_profile_b_fixtures.py" in script
    assert "smoke_media_preparation.py" in script
    assert "build_tauri_backend_sidecar.ps1" in script
    assert "-ValidateSlimMediaTools" in script
    assert "profile-b-msys2-build-report.json" in script


def test_profile_b_msys2_build_runner_plan_only_outputs_json(tmp_path: Path) -> None:
    build_root = tmp_path / "profile-b-msys2"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_PATH),
            "-BuildRoot",
            str(build_root),
            "-PlanOnly",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["apiVersion"] == "1"
    assert payload["ok"] is True
    assert payload["mode"] == "plan"
    assert "mingw-w64-ucrt-x86_64-lame" in payload["requiredPackages"]
    assert "mingw-w64-ucrt-x86_64-opus" in payload["requiredPackages"]
    assert any("smoke_profile_b_fixtures.py" in command for command in payload["commands"])
    assert any("validate_ffmpeg_profile.py" in command for command in payload["commands"])

    report_path = build_root / "profile-b-msys2-build-report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "plan"


def test_profile_b_msys2_build_runner_powershell_parses() -> None:
    command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"(Resolve-Path '{SCRIPT_PATH}'), [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | Format-List *; exit 1 }; 'parser ok'"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "parser ok" in result.stdout

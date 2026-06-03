from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHENTICODE_SCRIPT = REPO_ROOT / "scripts" / "validate_windows_authenticode.ps1"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_windows.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"


def powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is required for Windows release signing gates.")
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


@pytest.mark.parametrize("script", [AUTHENTICODE_SCRIPT, BUILD_SCRIPT])
def test_release_powershell_scripts_parse(script: Path) -> None:
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
        env={"SCRIPT_TO_PARSE": str(script)},
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_authenticode_gate_rejects_unsigned_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "unsigned.exe"
    artifact.write_bytes(b"MZ")
    report = tmp_path / "authenticode.json"

    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(AUTHENTICODE_SCRIPT),
        "-Path",
        str(artifact),
        "-OutputPath",
        str(report),
    )

    assert result.returncode == 1
    assert "Authenticode signature" in result.stderr
    assert not report.exists()


def test_build_script_wires_authenticode_gate() -> None:
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$RequireAuthenticodeSignature" in build_script
    assert "[string]$ExpectedAuthenticodePublisher" in build_script
    assert "[switch]$RequireAuthenticodeTimestamp" in build_script
    assert '$releaseExe = Join-Path $targetRelease "scriber-desktop.exe"' in build_script
    assert "$authenticodeTargets" in build_script
    assert "scripts\\validate_windows_authenticode.ps1" in build_script
    assert '$authenticodeReportPath = Join-Path $metadataDir "authenticode.json"' in build_script
    assert '"-OutputPath", $authenticodeReportPath' in build_script
    assert "Authenticode signature validation" in build_script


def test_authenticode_gate_supports_json_output_path() -> None:
    script = AUTHENTICODE_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$OutputPath = ""' in script
    assert "function Write-Utf8NoBomJson" in script
    assert "Write-Utf8NoBomJson -Path $resolvedOutputPath -Json $json" in script
    assert "$json" in script


def test_release_workflow_exposes_authenticode_gate_switches() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE" in workflow
    assert "SCRIBER_AUTHENTICODE_PUBLISHER" in workflow
    assert "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP" in workflow
    assert "-RequireAuthenticodeSignature" in workflow
    assert "-RequireAuthenticodeTimestamp" in workflow

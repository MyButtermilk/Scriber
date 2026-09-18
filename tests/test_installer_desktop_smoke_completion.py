from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts" / "smoke_windows_installer.ps1").read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(rf"(?ms)^function {re.escape(name)} \{{.*?(?=^function |\Z)", SOURCE)
    assert match is not None
    return match.group(0)


def _quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_scripts(scripts: list[str], tmp_path: Path) -> list[dict]:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    path = tmp_path / "smoke-harness.ps1"
    # Exercise independent cases in child scopes of one shell, avoiding a new
    # Windows PowerShell startup for every negative fixture in the full suite.
    command = "$ErrorActionPreference = 'Stop'\n$cases = @(\n"
    command += "\n".join("& {\n" + script + "\n}" for script in scripts)
    command += "\n)\nConvertTo-Json -InputObject $cases -Compress\n"
    path.write_text(command, encoding="utf-8")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(case) for case in json.loads(result.stdout.strip())]


def _passing_report() -> dict:
    return {
        "ok": True,
        "ready": True,
        "cleanupVerified": True,
        "runtimeMode": "tauri-supervised",
        "launchKind": "sidecar",
        "processExitCode": 0,
    }


def test_desktop_smoke_requires_successful_process_and_complete_report(tmp_path: Path) -> None:
    cases = [
        ({}, 0, True),
        ({}, 1, False),
        ({"ok": False}, 0, False),
        ({"ok": None}, 0, False),
        ({"ok": "true"}, 0, False),
        ({"ready": False}, 0, False),
        ({"cleanupVerified": False}, 0, False),
    ]
    scripts = []
    diagnostics = {"marker": "retained-diagnostics"}
    for change, exit_code, _passes in cases:
        report = _passing_report() | change | {"failureDiagnostics": diagnostics}
        scripts.append(
            _function("Invoke-InstalledDesktopSmoke")
            + _function("Assert-InstalledDesktopSmokeSucceeded")
            + f"\n$RepoRoot = {_quote(tmp_path)}\n"
            + f"$rawReport = {_quote(json.dumps(report))}\n"
            + f"$mockExitCode = {exit_code}\n"
            + """
$script:failure = $null
function powershell {
    Set-Content -LiteralPath (Join-Path $RepoRoot 'installed-desktop-smoke.json') -Value $rawReport
    $global:LASTEXITCODE = $mockExitCode
}
function Write-SmokeJson { param($Payload, $Path, $Root); $script:failure = $Payload }
$smoke = Invoke-InstalledDesktopSmoke -AppExe 'unused.exe' -RuntimeDataDir $RepoRoot
$passed = $false
try {
    Assert-InstalledDesktopSmokeSucceeded -Smoke $smoke -Stage 'clean-install' -CleanInstall $smoke
    $passed = $true
} catch {}
@{passed=$passed; smoke=$smoke; failure=$script:failure} | ConvertTo-Json -Depth 10 -Compress
"""
        )
    for (_change, exit_code, passes), result in zip(cases, _run_scripts(scripts, tmp_path), strict=True):
        assert result["passed"] is passes
        assert result["smoke"]["processExitCode"] == exit_code
        if not passes:
            failure = result["failure"]
            assert failure["ok"] is False
            assert failure["failedStage"] == "clean-install"
            assert failure["desktopSmokeFailure"]["failureDiagnostics"] == diagnostics
            assert failure["cleanInstall"] == result["smoke"]


def test_missing_or_invalid_fresh_report_cannot_reuse_previous_launch(tmp_path: Path) -> None:
    scripts = []
    for content in [None, "invalid-json", "true", "[]"]:
        scripts.append(
            _function("Invoke-InstalledDesktopSmoke")
            + f"\n$RepoRoot = {_quote(tmp_path)}\n"
            + f"$mockContent = {'$null' if content is None else _quote(content)}\n"
            + f"$stale = {_quote(json.dumps(_passing_report()))}\n"
            + """
Set-Content -LiteralPath (Join-Path $RepoRoot 'installed-desktop-smoke.json') -Value $stale
function powershell {
    if ($null -ne $mockContent) {
        Set-Content -LiteralPath (Join-Path $RepoRoot 'installed-desktop-smoke.json') -Value $mockContent
    }
    $global:LASTEXITCODE = 0
}
Invoke-InstalledDesktopSmoke -AppExe 'unused.exe' -RuntimeDataDir $RepoRoot |
    ConvertTo-Json -Depth 10 -Compress
"""
        )
    for result in _run_scripts(scripts, tmp_path):
        assert result["ok"] is False
        assert result["failureDiagnostics"]["reason"] == "missing_or_invalid_desktop_smoke_report"


def test_installer_orchestration_retains_both_launches_and_stops_on_first_failure(tmp_path: Path) -> None:
    install = tmp_path / "install"
    data = tmp_path / "data"
    (install / "backend" / "app" / "src").mkdir(parents=True)
    data.mkdir()
    body = SOURCE[SOURCE.index("$smoke = $null\n"):]
    scripts = []
    fixtures = []
    for failed_stage in [None, "clean-install", "upgrade"]:
        first = _passing_report() | {"launchMarker": "clean-install"}
        second = _passing_report() | {"launchMarker": "upgrade"}
        if failed_stage == "clean-install":
            first["ok"] = False
            first["failureDiagnostics"] = {"marker": "first-failure"}
        elif failed_stage == "upgrade":
            # A green-looking report from a failed child process must not pass.
            second["processExitCode"] = 1
            second["failureDiagnostics"] = {"marker": "second-failure"}
        fixtures.append((failed_stage, first, second))
        scripts.append(
            _function("Assert-InstalledDesktopSmokeSucceeded")
            + f"\n$InstallDir = {_quote(install)}\n$DataDir = {_quote(data)}\n"
            + f"$RepoRoot = {_quote(tmp_path)}\n$tmpRoot = $RepoRoot\n"
            + f"$first = {_quote(json.dumps(first))} | ConvertFrom-Json\n"
            + f"$second = {_quote(json.dumps(second))} | ConvertFrom-Json\n"
            + """
$InstallerPath = 'fixture-installer.exe'
$OutputPath = 'fixture-report.json'
$SimulateUpgrade = $true
$KeepInstalled = $false
$VerifyUninstall = $true
$script:installCalls = 0
$script:smokeCalls = 0
$script:cleanupCalls = 0
function Invoke-ProcessChecked {
    param($FilePath, $ArgumentList, $Label)
    $script:installCalls++
    $retired = Join-Path $InstallDir 'backend/app/src/gemini_transcribe.py'
    if ($script:installCalls -eq 2 -and (Test-Path -LiteralPath $retired)) {
        Remove-Item -LiteralPath $retired
    }
}
function Resolve-InstalledAppExe { return 'fixture-app.exe' }
function Resolve-InstalledAudioSidecarExe { return 'fixture-audio.exe' }
function Test-InstalledFrontendAssetOwnership { return @{verified=$true} }
function Test-InstalledMeetingResources { return @{verified=$true} }
function Test-InstalledLocalPolishingRuntime { return @{verified=$true} }
function Get-DirectorySizeReport { return @{totalBytes=1} }
function Invoke-InstalledDesktopSmoke {
    $script:smokeCalls++
    if ($script:smokeCalls -eq 1) { return $first }
    return $second
}
function Invoke-InstalledUninstallCheck { return @{verified=$true} }
function Resolve-InstalledUninstaller { return $null }
function Remove-InstallerSmokeArtifacts { $script:cleanupCalls++ }
function Write-SmokeJson { param($Payload, $Path, $Root); $script:report = $Payload }
$passed = $false
try {
    $null = & {
"""
            + body
            + """
    }
    $passed = $true
} catch {}
@{
    passed=$passed; installCalls=$script:installCalls; smokeCalls=$script:smokeCalls
    cleanupCalls=$script:cleanupCalls; report=$script:report
} | ConvertTo-Json -Depth 10 -Compress
"""
        )
    for (failed_stage, first, second), result in zip(fixtures, _run_scripts(scripts, tmp_path), strict=True):
        assert result["passed"] is (failed_stage is None)
        assert result["cleanupCalls"] == 1
        assert result["report"]["cleanInstall"] == first
        if failed_stage == "clean-install":
            assert result["installCalls"] == result["smokeCalls"] == 1
            assert result["report"]["desktopSmokeFailure"] == first
        else:
            assert result["installCalls"] == result["smokeCalls"] == 2
            if failed_stage == "upgrade":
                assert result["report"]["desktopSmokeFailure"] == second
            else:
                assert result["report"]["ok"] is True
                assert result["report"]["desktopSmokeExitCode"] == 0
                assert result["report"]["upgrade"]["verified"] is True

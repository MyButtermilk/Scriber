from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/smoke_tauri_desktop.ps1").read_text(encoding="utf-8")


def test_startup_diagnostics_capture_exit_and_bounded_redacted_logs_without_backend(tmp_path: Path) -> None:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    secrets = ["sentinel-session-value", "sentinel-env-key", "unconfigured-password", "unconfigured-url-token"]
    (log_dir / "tauri-shell.log").write_text(
        "backend_layer_validation_failed\n"
        + " ".join(secrets[:2])
        + '\npassword="unconfigured-password"\nhttps://host/?token=unconfigured-url-token\n'
        + 'File "C:\\Users\\private-person\\runtime.py"\n'
        + 'File "/home/private-person/runtime.py"\n',
        encoding="utf-8",
    )
    (log_dir / "tauri-backend.log").write_text("private-prefix-" + "x" * 20000 + "\nexit_code=78\n")
    (log_dir / "smoke-shell.stderr.log").write_text("native panic: failed to initialize\n")
    # A single oversized line must not leak an unrecognized credential suffix.
    (log_dir / "smoke-shell.stdout.log").write_text("api_key=" + "s" * 20000)
    script = ROOT / "tests/powershell/test_tauri_startup_diagnostics.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script), "-RuntimeDataDir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for secret in [*secrets, "private-person", "private-prefix", "never serialize"]:
        assert secret not in result.stdout
    evidence = json.loads(result.stdout)
    assert evidence["running"]["app"] == {"started": True, "hasExited": False, "exitCode": None}
    assert evidence["running"]["managedBackendCount"] == 1
    assert evidence["exited"]["app"] == {"started": True, "hasExited": True, "exitCode": 7}
    assert evidence["missing"]["app"]["started"] is False
    assert evidence["missing"]["managedBackendCount"] == 0
    assert all(not log["present"] for log in evidence["missing"]["logs"])
    logs = {log["name"]: log for log in evidence["running"]["logs"]}
    assert len(logs) == 5
    assert logs["tauri-backend.log"]["truncated"] is True
    assert logs["tauri-backend.log"]["text"].splitlines() == ["exit_code=78"]
    assert logs["smoke-shell.stdout.log"]["text"] == ""
    assert "backend_layer_validation_failed" in logs["tauri-shell.log"]["text"]
    assert "native panic" in logs["smoke-shell.stderr.log"]["text"]
    assert all(len(log["text"]) <= 16384 for log in logs.values())
    unreadable = {log["name"]: log for log in evidence["unreadable"]["logs"]}
    assert evidence["unreadable"]["managedBackendCount"] is None
    assert unreadable["tauri-shell.log"]["error"] == "log_read_failed"
    assert "native panic" in unreadable["smoke-shell.stderr.log"]["text"]


def test_failure_evidence_is_collected_and_written_before_cleanup() -> None:
    catch = SOURCE[SOURCE.index("    $failure = $_\n") : SOURCE.index("} finally {\n    $cleanupFailure")]
    assert catch.index("Get-SmokeStartupFailureDiagnostics") < catch.index("if ($listener)")
    assert "-AppProcess $app -ManagedBackendCount $managedBackendCount -Token $SessionToken" in catch
    assert 'Import-Module (Join-Path $PSScriptRoot "SmokeStartupDiagnostics.psm1") -Force' in SOURCE
    assert "Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot | Out-Null" in catch
    assert "-RedirectStandardOutput (Join-Path $shellOutputDir 'smoke-shell.stdout.log')" in SOURCE
    assert "-RedirectStandardError (Join-Path $shellOutputDir 'smoke-shell.stderr.log')" in SOURCE
    cleanup_failure = SOURCE[SOURCE.index("    if ($cleanupFailure) {") :]
    assert cleanup_failure.index("Write-SmokeJson") < cleanup_failure.index("throw $cleanupFailure")

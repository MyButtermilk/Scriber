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
    secrets = [
        "sentinel-session-value",
        "sentinel-env-key",
        "sentinel-oth-key",
        "unconfigured-password",
        "unconfigured-url-token",
    ]
    assert len(secrets[1]) == len(secrets[2])
    (log_dir / "tauri-shell.log").write_text(
        "backend_layer_validation_failed\n"
        + " ".join(secrets[:3])
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


@pytest.mark.parametrize(
    ("stderr", "expected_error"),
    [
        ("", None),
        ("backend exited with code 1; restarting\ncontrolled shutdown completed\n", None),
        ("diagnostic: thread 'example' panicked at is a sample error message\n", None),
        (
            "thread 'reqwest-internal-sync-runtime' (21248) panicked at C:\\private\\sentinel-token.rs:12:7:\n"
            "sentinel-secret-payload\n",
            "Tauri shell reported an unexpected Rust panic.",
        ),
        (
            "thread 'shell-ipc-client' panicked at 'event loop thread panicked', sentinel-private.rs:1:2\n",
            "Tauri shell reported an unexpected Rust panic.",
        ),
        (
            "thread 'first' panicked at sentinel-secret.rs:1:2:\n" + "ordinary later output\n" * 2000,
            "Tauri shell reported an unexpected Rust panic.",
        ),
        ("thread caused non-unwinding panic. aborting.\n", "Tauri shell reported an unexpected Rust panic."),
        (None, "Tauri shell stderr could not be verified."),
        ("x" * (8 * 1024 * 1024 + 1), "Tauri shell stderr could not be verified."),
    ],
    ids=[
        "empty",
        "backend-recovery",
        "quoted-example",
        "rust-thread-id",
        "rust-legacy",
        "panic-before-tail",
        "abort",
        "missing",
        "oversized",
    ],
)
def test_shell_panic_gate_checks_real_headers_without_disclosing_stderr(
    tmp_path: Path, stderr: str | None, expected_error: str | None
) -> None:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    logs = tmp_path / "logs"
    logs.mkdir()
    if stderr is not None:
        (logs / "smoke-shell.stderr.log").write_text(stderr, encoding="utf-8")
    script = ROOT / "tests/powershell/test_tauri_startup_diagnostics.ps1"
    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        "-RuntimeDataDir",
        str(tmp_path),
        "-CheckShellPanic",
    ]
    if stderr is not None:
        command.append("-HoldShellLogOpen")
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    combined = result.stdout + result.stderr
    assert "sentinel-" not in combined
    if expected_error is None:
        assert result.returncode == 0, combined
        evidence = json.loads(result.stdout)
        assert evidence["verified"] is True
        assert evidence["panicDetected"] is False
    else:
        assert result.returncode != 0
        assert expected_error in combined


def test_shell_panic_gate_runs_before_success_and_after_cleanup() -> None:
    check = "Assert-SmokeShellNoPanic -RuntimeDataDir $DataDir"
    assert SOURCE.index(check) < SOURCE.index("        ok = $true\n", SOURCE.index(check))
    cleanup = SOURCE.index("} finally {\n    $cleanupFailure")
    final_check = SOURCE.index(check, cleanup)
    assert final_check < SOURCE.index(
        "Write-SmokeJson -Payload $result -Path $OutputPath -Root $RepoRoot\n", final_check
    )

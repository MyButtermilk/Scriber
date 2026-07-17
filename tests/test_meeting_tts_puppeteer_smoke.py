from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_meeting_speech_smoke_uses_puppeteer_against_real_webview2() -> None:
    powershell = _read("scripts/smoke_meeting_tts_puppeteer.ps1")
    driver = _read("scripts/smoke_meeting_tts_puppeteer.mjs")
    combined = f"{powershell}\n{driver}".lower()

    assert "puppeteer-core" in combined
    assert "playwright" not in combined
    assert "--remote-debugging-address=127.0.0.1" in powershell
    assert 'browserTransport: "webview2-remote-debugging"' in driver
    assert 'runtime?.runtimeMode === "tauri-supervised"' in driver
    assert 'page.$eval(selector, (button)' in driver
    assert 'page.on("pageerror", (error)' in driver
    assert "if (diagnostics.pageErrorCount > 0)" in driver
    assert 'activePhase = "validate-page-errors"' in driver
    for action in ("pause", "resume", "stop"):
        assert f'clickControl(\n      page,\n      meetingId,\n      "{action}"' in driver


def test_meeting_speech_smoke_keeps_pcm_in_explicit_synthetic_path() -> None:
    powershell = _read("scripts/smoke_meeting_tts_puppeteer.ps1")
    generator = _read("scripts/generate_meeting_tts_fixture.py")
    sidecar = _read("Frontend/src-tauri/src/audio_sidecar.rs")
    fixture_env = "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH"

    assert fixture_env in powershell
    assert fixture_env in sidecar
    assert '"SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE" = "1"' in powershell
    assert '"SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL" = "0"' in powershell
    assert '"SCRIBER_RUST_AUDIO_WASAPI_CAPTURE" = "0"' in powershell
    assert 'request.capture_kind.eq_ignore_ascii_case("microphone")' in sidecar
    assert "request.sample_rate != 48_000 || request.channels != 1" in sidecar
    assert "if !path.is_absolute()" in sidecar
    assert "const SYNTHETIC_PCM_MAX_BYTES: u64 = 64 * 1024 * 1024" in sidecar
    assert "MAX_FIXTURE_BYTES = 64 * 1024 * 1024" in generator
    assert '"--noise-scale",\n                "0"' in generator
    assert '"pcm_s16le_48000_mono"' in generator


def test_meeting_speech_smoke_artifacts_are_privacy_minimal() -> None:
    driver = _read("scripts/smoke_meeting_tts_puppeteer.mjs")
    success_start = driver.index("    return {\n      schemaVersion: 1,\n      ok: true,")
    success_end = driver.index("    };\n  } finally", success_start)
    success_result = driver[success_start:success_end]

    for expected in (
        "meetingIdHash",
        "observedStates",
        "segmentCount",
        "transcriptCharacterCount",
        "audioGapCount",
        "diagnostics",
        "meetingDebug",
    ):
        assert expected in success_result
    for forbidden in (
        "sessionToken",
        "baseUrl",
        "transcript:",
        "segments:",
        "audioGaps:",
        "screenshot",
    ):
        assert forbidden not in success_result
    assert "sanitizeMessage(error)" in driver
    assert "await fs.rename(temporary, resolved)" in driver


def test_driver_bootstrap_failure_emits_bounded_meeting_debug() -> None:
    completed = subprocess.run(
        ["node", str(REPO_ROOT / "scripts" / "smoke_meeting_tts_puppeteer.mjs")],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stderr.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["phase"] == "bootstrap"
    assert payload["diagnostics"] == {
        "consoleErrorCount": 0,
        "pageErrorCount": 0,
        "requestFailureCount": 0,
    }
    assert payload["meetingDebug"] == {
        "providerPhase": "not_started",
        "meetingIdHash": None,
        "captureIdHash": None,
        "meetingState": None,
        "finalProvider": None,
        "segmentCount": None,
        "errorCode": "harness_configuration_invalid",
    }
    assert set(payload["meetingDebug"]) == {
        "providerPhase",
        "meetingIdHash",
        "captureIdHash",
        "meetingState",
        "finalProvider",
        "segmentCount",
        "errorCode",
    }


def test_meeting_speech_smoke_owns_and_cleans_only_its_process_tree() -> None:
    powershell = _read("scripts/smoke_meeting_tts_puppeteer.ps1")

    assert "A Scriber desktop process is already running" in powershell
    assert "Get-ProcessDescendantHandles" in powershell
    assert "$null = $process.Handle" in powershell
    assert "CreationDate, ExecutablePath" in powershell
    assert "[System.StringComparison]::OrdinalIgnoreCase" in powershell
    assert "$Process.Kill()" in powershell
    assert "Stop-Process -Id" not in powershell
    assert "SCRIBER_TAURI_SMOKE_SHELL_MENU_TRIGGER_FILE" in powershell
    assert "Tauri shell did not exit through the bounded smoke Quit path" in powershell
    assert "$driverSucceeded = $false" in powershell
    assert "$driverSucceeded = $true" in powershell
    assert "if ($driverSucceeded -and -not $KeepRuntimeData)" in powershell
    assert '$FailureDebugPath = Join-Path $ArtifactDir "failure-debug.json"' in powershell
    assert "Write-AtomicJson -Path $FailureDebugPath -Value $failureReport" in powershell
    assert '$runPhase = "verify-owned-process-cleanup"' in powershell
    assert 'throw "Meeting E2E cleanup did not complete successfully."' in powershell
    assert "$redirectOwner.Dispose()" in powershell
    assert "if ($driverSucceeded)" in powershell
    assert "$viteDescendantProcesses = @()" in powershell
    assert "Get-ProcessDescendantHandles -RootProcess $viteProcess" in powershell
    assert "$rootSnapshotTicks - $rootStartTicks" in powershell
    assert "foreach ($owned in $viteDescendantProcesses)" in powershell
    assert "Remove-ContainedDirectory" in powershell
    assert "$driverStdout = @(& $NodePath @driverArguments)" in powershell
    assert "$driverExitCode = $LASTEXITCODE" in powershell
    success_output = powershell.rindex("[pscustomobject]@{")
    assert success_output > powershell.index("Write-AtomicJson -Path $FailureDebugPath")
    main_finally = powershell.index("} finally {", powershell.index('$runPhase = "prepare-harness"'))
    assert success_output > main_finally


def test_powershell_preparation_failure_retains_debug_data_but_removes_pcm(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is required for the Windows Meeting harness")

    artifact_dir = tmp_path / "meeting-failure"
    runtime_dir = artifact_dir / "runtime-data"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "probe.txt").write_text("synthetic-local-state", encoding="utf-8")
    (artifact_dir / "vite.stderr.log").write_text("synthetic-log", encoding="utf-8")
    (artifact_dir / "meeting-mic.pcm").write_bytes(b"synthetic-pcm")
    env = os.environ.copy()
    env["SCRIBER_MEETING_E2E_TEST_PREPARATION_FAILURE"] = "1"

    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "smoke_meeting_tts_puppeteer.ps1"),
            "-ArtifactDir",
            str(artifact_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert '"ok":true' not in completed.stdout.replace(" ", "").lower()
    report = json.loads((artifact_dir / "failure-debug.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["failureLayer"] == "orchestrator"
    assert report["failurePhase"] == "prepare-harness"
    assert report["errorCode"] == "meeting_e2e_prepare_harness_failed"
    assert report["meetingDebug"] == {
        "providerPhase": "not_started",
        "meetingIdHash": None,
        "captureIdHash": None,
        "meetingState": None,
        "finalProvider": None,
        "segmentCount": None,
        "errorCode": None,
    }
    assert report["observedStates"] == []
    assert report["diagnostics"] == {
        "consoleErrorCount": None,
        "pageErrorCount": None,
        "requestFailureCount": None,
    }
    assert isinstance(report["timings"]["elapsedMs"], int)
    assert report["timings"]["elapsedMs"] >= 0
    assert report["retention"] == {
        "runtimeDataRetained": True,
        "rawLogsRetained": True,
        "fixtureRetained": False,
        "containsSensitiveLocalData": True,
    }
    assert report["cleanupVerified"] is True
    assert (runtime_dir / "probe.txt").is_file()
    assert (artifact_dir / "vite.stderr.log").is_file()
    assert not (artifact_dir / "meeting-mic.pcm").exists()


def test_powershell_cleanup_failure_cannot_emit_success(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is required for the Windows Meeting harness")

    artifact_dir = tmp_path / "meeting-cleanup-failure"
    runtime_dir = artifact_dir / "runtime-data"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "probe.txt").write_text("synthetic-local-state", encoding="utf-8")
    (artifact_dir / "tauri.stderr.log").write_text("synthetic-log", encoding="utf-8")
    (artifact_dir / "meeting-mic.pcm").write_bytes(b"synthetic-pcm")
    env = os.environ.copy()
    env["SCRIBER_MEETING_E2E_TEST_CLEANUP_FAILURE"] = "1"

    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "smoke_meeting_tts_puppeteer.ps1"),
            "-ArtifactDir",
            str(artifact_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert '"ok":true' not in completed.stdout.replace(" ", "").lower()
    report = json.loads((artifact_dir / "failure-debug.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["failureLayer"] == "orchestrator"
    assert report["failurePhase"] == "verify-owned-process-cleanup"
    assert report["errorCode"] == "meeting_e2e_verify_owned_process_cleanup_failed"
    assert report["cleanupVerified"] is False
    assert report["retention"] == {
        "runtimeDataRetained": True,
        "rawLogsRetained": True,
        "fixtureRetained": False,
        "containsSensitiveLocalData": True,
    }
    assert (runtime_dir / "probe.txt").is_file()
    assert (artifact_dir / "tauri.stderr.log").is_file()
    assert not (artifact_dir / "meeting-mic.pcm").exists()


def test_failure_debug_report_has_only_bounded_diagnostic_fields() -> None:
    powershell = _read("scripts/smoke_meeting_tts_puppeteer.ps1")
    report_start = powershell.index("        $failureReport = [ordered]@{")
    report_end = powershell.index("        try {", report_start)
    report = powershell[report_start:report_end]

    for expected in (
        "failureLayer",
        "failurePhase",
        "providerPhase",
        "meetingIdHash",
        "captureIdHash",
        "meetingState",
        "finalProvider",
        "segmentCount",
        "errorCode",
        "observedStates",
        "diagnostics",
        "timings",
        "runtimeDataRetained",
        "rawLogsRetained",
        "fixtureRetained",
        "containsSensitiveLocalData",
        "cleanupVerified",
    ):
        assert expected in report
    for forbidden in (
        "errorMessage",
        "transcript",
        "captureMetadata",
        "sessionToken",
        "baseUrl",
        "audioGaps",
        "screenshot",
        "ArtifactDir",
        "DataDir",
    ):
        assert forbidden not in report

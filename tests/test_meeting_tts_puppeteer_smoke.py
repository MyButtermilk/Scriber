from __future__ import annotations

from pathlib import Path


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
    assert "$redirectOwner.Dispose()" in powershell
    assert "if ($driverSucceeded)" in powershell
    assert "$viteDescendantProcesses = @()" in powershell
    assert "Get-ProcessDescendantHandles -RootProcess $viteProcess" in powershell
    assert "$rootSnapshotTicks - $rootStartTicks" in powershell
    assert "foreach ($owned in $viteDescendantProcesses)" in powershell
    assert "Remove-ContainedDirectory" in powershell

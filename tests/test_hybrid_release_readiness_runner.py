from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_hybrid_release_readiness.ps1"


def powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is required for the hybrid release readiness runner.")
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


def test_hybrid_release_readiness_runner_powershell_parses() -> None:
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
        env={"SCRIPT_TO_PARSE": str(RUNNER_SCRIPT)},
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_hybrid_release_readiness_runner_plan_only_writes_operator_plan(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-AuthenticodePath",
        str(tmp_path / "scriber-desktop.exe"),
        "-ExpectedAuthenticodePublisher",
        "Scriber Publisher",
        "-RequireAuthenticodeTimestamp",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    written = json.loads((tmp_path / "hybrid-release-readiness-runner-plan.json").read_text(encoding="utf-8-sig"))
    command_names = [entry["name"] for entry in payload["commands"]]
    assert command_names == [
        "microphoneMatrixValidation",
        "updaterPublicationVerification",
        "mediaPreparationSmoke",
        "runtimeDependencyFootprint",
        "rustAudioSidecarSmoke",
        "rustAudioPrewarmSidecarSmoke",
        "rustAudioAppPrewarmSmoke",
        "recordingHotPathPythonRustComparison",
        "installedLiveRecordingSmoke",
        "tauriTextInjectionSmoke",
        "tauriTextInjectionMatrix",
        "authenticodeValidation",
        "hybridReleaseReadiness",
    ]
    assert payload["ok"] is True
    assert payload["planOnly"] is True
    evidence_names = [entry["name"] for entry in payload["requiredEvidence"]]
    assert evidence_names == [
        "physicalMicrophoneMatrix",
        "signedTauriUpdaterMetadata",
        "mediaPreparationSmoke",
        "runtimeDependencyFootprint",
        "rustAudioSidecarSmoke",
        "rustAudioPrewarmSidecarSmoke",
        "rustAudioAppPrewarmSmoke",
        "recordingHotPathPythonRustComparison",
        "installedLiveRecordingSmoke",
        "tauriTextInjectionSmoke",
        "tauriTextInjectionMatrix",
        "publishedUpdaterManifest",
        "authenticodeSignatures",
        "hybridReleaseReadinessAggregate",
    ]
    hardware_evidence = payload["requiredEvidence"][0]
    assert hardware_evidence["external"] is True
    assert len(hardware_evidence["expectedArtifacts"]) == 8
    assert any("favorite-fallback" in artifact for artifact in hardware_evidence["expectedArtifacts"])
    assert hardware_evidence["producer"] == "external artifacts or scripts\\run_microphone_hardware_matrix.ps1"
    assert hardware_evidence["waitSec"] == 60
    assert hardware_evidence["pollSec"] == 1
    assert hardware_evidence["forceRefreshEachPoll"] is False
    assert hardware_evidence["labelsConfigured"] == {
        "usb": False,
        "dock": False,
        "bluetooth": False,
        "favorite": False,
    }
    assert hardware_evidence["requireRustEndpointInventory"] is False
    assert hardware_evidence["requireDeviceRefreshEvidence"] is False
    updater_evidence = payload["requiredEvidence"][1]
    assert updater_evidence["metadata"].endswith("latest.json")
    assert "absolute HTTPS" in updater_evidence["notes"]
    media_evidence = payload["requiredEvidence"][2]
    assert media_evidence["external"] is False
    assert media_evidence["report"].endswith("media-preparation-smoke.json")
    assert "smoke_media_preparation.py" in media_evidence["producer"]
    assert media_evidence["mediaToolsDir"].endswith("backend\\tools\\ffmpeg")
    runtime_evidence = payload["requiredEvidence"][3]
    assert runtime_evidence["external"] is False
    assert runtime_evidence["report"].endswith("runtime-dependency-footprint.json")
    assert "analyze_backend_runtime_dependencies.py" in runtime_evidence["producer"]
    assert runtime_evidence["sidecarDir"].endswith("target\\release\\backend")
    rust_evidence = payload["requiredEvidence"][4]
    assert rust_evidence["required"] is False
    assert rust_evidence["report"].endswith("rust-audio-sidecar-smoke.json")
    assert rust_evidence["producer"] == "not requested"
    assert rust_evidence["durationSec"] == 600
    assert rust_evidence["selectedDurationSec"] == 10
    assert rust_evidence["prebufferMs"] == 400
    assert rust_evidence["prewarmBeforeCapture"] is False
    assert rust_evidence["requirePrewarmAdoption"] is False
    assert rust_evidence["prewarmDurationSec"] == 0.5
    assert "Optional for standard releases" in rust_evidence["notes"]
    prewarm_evidence = payload["requiredEvidence"][5]
    assert prewarm_evidence["required"] is False
    assert prewarm_evidence["report"].endswith("rust-audio-prewarm-sidecar-smoke.json")
    assert prewarm_evidence["producer"] == "not requested"
    assert prewarm_evidence["mode"] == "synthetic"
    assert prewarm_evidence["durationSec"] == 1
    assert prewarm_evidence["prebufferMs"] == 400
    assert "Optional lifecycle evidence only" in prewarm_evidence["notes"]
    app_prewarm_evidence = payload["requiredEvidence"][6]
    assert app_prewarm_evidence["required"] is False
    assert app_prewarm_evidence["report"].endswith("rust-audio-app-prewarm-smoke.json")
    assert app_prewarm_evidence["producer"] == "not requested"
    assert app_prewarm_evidence["mode"] == "wasapi"
    assert app_prewarm_evidence["minDurationSec"] == 0
    assert app_prewarm_evidence["minPrewarmDurationSec"] == 0
    assert "app-level RustAudioPrewarmManager" in app_prewarm_evidence["notes"]
    comparison_evidence = payload["requiredEvidence"][7]
    assert comparison_evidence["required"] is False
    assert comparison_evidence["report"].endswith("recording-hot-path-python-rust-comparison.json")
    assert comparison_evidence["producer"] == "not requested"
    assert "at least three samples per engine" in comparison_evidence["notes"]
    assert "local audio-owned hot-path segments" in comparison_evidence["notes"]
    live_recording_evidence = payload["requiredEvidence"][8]
    assert live_recording_evidence["required"] is False
    assert live_recording_evidence["external"] is True
    assert live_recording_evidence["report"].endswith("installed-live-recording-smoke.json")
    assert live_recording_evidence["requireRustAudio"] is False
    assert live_recording_evidence["producer"] == "not requested"
    assert "non-recording sample leakage" in live_recording_evidence["notes"]
    injection_evidence = payload["requiredEvidence"][9]
    assert injection_evidence["required"] is False
    assert injection_evidence["external"] is True
    assert injection_evidence["report"].endswith("tauri-text-injection-smoke.json")
    assert injection_evidence["producer"] == "not requested"
    assert injection_evidence["textChars"] > 0
    assert injection_evidence["targetTitle"] == "Scriber Tauri Injection Readiness Target"
    assert injection_evidence["timeoutSec"] == 5
    assert injection_evidence["skipTargetClick"] is False
    assert "clipboard_set and paste markers" in injection_evidence["notes"]
    injection_matrix_evidence = payload["requiredEvidence"][10]
    assert injection_matrix_evidence["required"] is False
    assert injection_matrix_evidence["external"] is True
    assert injection_matrix_evidence["report"].endswith("tauri-text-injection-matrix.json")
    assert injection_matrix_evidence["producer"] == "not requested"
    assert injection_matrix_evidence["inputDir"].endswith("tauri-text-injection")
    assert injection_matrix_evidence["scenarioOverrides"] == []
    assert injection_matrix_evidence["unsupportedOptional"] == []
    assert injection_matrix_evidence["inputReportsExternal"] is True
    assert "same-text restore" in injection_matrix_evidence["notes"]
    publication_evidence = payload["requiredEvidence"][11]
    assert "final redirect URL" in publication_evidence["notes"]
    authenticode_evidence = payload["requiredEvidence"][12]
    assert authenticode_evidence["expectedPublisher"] == "Scriber Publisher"
    assert authenticode_evidence["requireTimestamp"] is True
    assert "validate_microphone_hardware_matrix.py" in payload["commands"][0]["command"]
    assert "--require-device-refresh-evidence" not in payload["commands"][0]["command"]
    assert "verify_tauri_updater_publication.py" in payload["commands"][1]["command"]
    assert "smoke_media_preparation.py" in payload["commands"][2]["command"]
    assert "--media-tools-dir" in payload["commands"][2]["command"]
    assert "--require-ffprobe" in payload["commands"][2]["command"]
    assert "analyze_backend_runtime_dependencies.py" in payload["commands"][3]["command"]
    assert "--sidecar-dir" in payload["commands"][3]["command"]
    assert "runtime-dependency-footprint.json" in payload["commands"][3]["command"]
    assert payload["commands"][4]["command"] == "not requested"
    assert payload["commands"][5]["command"] == "not requested"
    assert payload["commands"][6]["command"] == "not requested"
    assert payload["commands"][7]["command"] == "not requested"
    assert payload["commands"][8]["command"] == "not requested"
    assert payload["commands"][9]["command"] == "not requested"
    assert payload["commands"][10]["command"] == "not requested"
    assert "validate_windows_authenticode.ps1" in payload["commands"][11]["command"]
    assert "validate_hybrid_release_readiness.py" in payload["commands"][12]["command"]
    assert "--media-preparation-report" in payload["commands"][12]["command"]
    assert "media-preparation-smoke.json" in payload["commands"][12]["command"]
    assert "--runtime-dependency-footprint-report" in payload["commands"][12]["command"]
    assert "runtime-dependency-footprint.json" in payload["commands"][12]["command"]
    assert "--require-authenticode-timestamp" in payload["commands"][12]["command"]
    assert written == payload


def test_hybrid_release_readiness_runner_can_plan_microphone_matrix_run(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunMicrophoneHardwareMatrix",
        "-MicrophoneMatrixBaseUrl",
        "http://127.0.0.1:9999",
        "-MicrophoneMatrixToken",
        "real-session-token",
        "-MicrophoneMatrixWaitSec",
        "75",
        "-MicrophoneMatrixPollSec",
        "0.5",
        "-MicrophoneMatrixUsbLabel",
        "USB Mic",
        "-MicrophoneMatrixDockLabel",
        "Dock Mic",
        "-MicrophoneMatrixBluetoothLabel",
        "Bluetooth Headset",
        "-MicrophoneMatrixFavoriteLabel",
        "Favorite Mic",
        "-MicrophoneMatrixAssumeCompleted",
        "-RequireRustEndpointInventory",
        "-RequireDeviceRefreshEvidence",
        "-UseExistingAuthenticodeReport",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runMicrophoneHardwareMatrix"] is True
    assert payload["microphoneMatrixBaseUrl"] == "http://127.0.0.1:9999"
    assert payload["microphoneMatrixWaitSec"] == 75
    assert payload["microphoneMatrixPollSec"] == 0.5
    assert payload["microphoneMatrixForceRefreshEachPoll"] is False

    hardware_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    assert hardware_evidence["external"] is False
    assert hardware_evidence["producer"] == "scripts\\run_microphone_hardware_matrix.ps1"
    assert hardware_evidence["waitSec"] == 75
    assert hardware_evidence["pollSec"] == 0.5
    assert hardware_evidence["forceRefreshEachPoll"] is False
    assert hardware_evidence["labelsConfigured"] == {
        "usb": True,
        "dock": True,
        "bluetooth": True,
        "favorite": True,
    }
    assert hardware_evidence["requireRustEndpointInventory"] is True
    assert hardware_evidence["requireDeviceRefreshEvidence"] is True

    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "microphoneMatrixValidation")
    assert "run_microphone_hardware_matrix.ps1" in matrix_command["command"]
    assert "-BaseUrl http://127.0.0.1:9999" in matrix_command["command"]
    assert "-OutputDir" in matrix_command["command"]
    assert "-WaitSec 75" in matrix_command["command"]
    assert "-PollSec 0.5" in matrix_command["command"]
    assert '-UsbLabel "USB Mic"' in matrix_command["command"]
    assert '-DockLabel "Dock Mic"' in matrix_command["command"]
    assert '-BluetoothLabel "Bluetooth Headset"' in matrix_command["command"]
    assert '-FavoriteLabel "Favorite Mic"' in matrix_command["command"]
    assert "-AssumeCompleted" in matrix_command["command"]
    assert "-RequireRustEndpointInventory" in matrix_command["command"]
    assert "-RequireDeviceRefreshEvidence" in matrix_command["command"]
    assert "; python" in matrix_command["command"]
    assert "validate_microphone_hardware_matrix.py" in matrix_command["command"]
    assert "real-session-token" not in matrix_command["command"]
    assert "<session token>" in matrix_command["command"]


def test_hybrid_release_readiness_runner_rejects_forced_refresh_for_device_refresh_evidence(
    tmp_path: Path,
) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunMicrophoneHardwareMatrix",
        "-MicrophoneMatrixForceRefreshEachPoll",
        "-RequireDeviceRefreshEvidence",
        "-UseExistingAuthenticodeReport",
    )

    assert result.returncode == 1
    assert "-MicrophoneMatrixForceRefreshEachPoll cannot be used" in result.stderr


def test_hybrid_release_readiness_runner_can_plan_release_build(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunReleaseBuild",
        "-ReleaseBuildBundles",
        "nsis",
        "-ReleaseBuildReleaseBaseUrl",
        "https://github.com/MyButtermilk/Scriber/releases/latest/download",
        "-ReleaseBuildEnableTauriUpdater",
        "-ReleaseBuildUpdaterEndpoint",
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        "-ReleaseBuildUpdaterPublicKey",
        "test-public-key",
        "-ReleaseBuildRequireUpdaterSignatures",
        "-ReleaseBuildRequireAuthenticodeSignature",
        "-ReleaseBuildUseProfileBFfmpeg",
        "-ReleaseBuildValidateSlimMediaTools",
        "-ReleaseBuildReuseSidecarIfUnchanged",
        "-ReleaseBuildRunMediaPreparationSmoke",
        "-ReleaseBuildRunRuntimeDependencyFootprint",
        "-ExpectedAuthenticodePublisher",
        "Scriber Publisher",
        "-RequireAuthenticodeTimestamp",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runReleaseBuild"] is True
    assert payload["releaseBuildEnableTauriUpdater"] is True
    assert payload["releaseBuildRequireUpdaterSignatures"] is True
    assert payload["releaseBuildRequireAuthenticodeSignature"] is True
    assert payload["releaseBuildGeneratedAuthenticodeReport"].endswith(
        "Frontend\\src-tauri\\target\\release\\release-metadata\\authenticode.json"
    )
    assert "build_windows.ps1" in payload["releaseBuildCommand"]
    assert "-Bundles nsis" in payload["releaseBuildCommand"]
    assert "-ReleaseBaseUrl https://github.com/MyButtermilk/Scriber/releases/latest/download" in payload[
        "releaseBuildCommand"
    ]
    assert "-EnableTauriUpdater" in payload["releaseBuildCommand"]
    assert "-UpdaterEndpoint https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json" in payload[
        "releaseBuildCommand"
    ]
    assert "-UpdaterPublicKey test-public-key" in payload["releaseBuildCommand"]
    assert "-RequireUpdaterSignatures" in payload["releaseBuildCommand"]
    assert "-RequireAuthenticodeSignature" in payload["releaseBuildCommand"]
    assert "-ExpectedAuthenticodePublisher" in payload["releaseBuildCommand"]
    assert "-RequireAuthenticodeTimestamp" in payload["releaseBuildCommand"]
    assert "-UseProfileBFfmpeg" in payload["releaseBuildCommand"]
    assert "-ValidateSlimMediaTools" in payload["releaseBuildCommand"]
    assert "-ReuseSidecarIfUnchanged" in payload["releaseBuildCommand"]
    assert "-RunMediaPreparationSmoke" in payload["releaseBuildCommand"]
    assert "-RunRuntimeDependencyFootprint" in payload["releaseBuildCommand"]

    signed_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "signedTauriUpdaterMetadata")
    assert signed_evidence["producer"] == "scripts\\build_windows.ps1"
    assert signed_evidence["releaseBuild"]["run"] is True
    assert signed_evidence["releaseBuild"]["enableTauriUpdater"] is True
    assert signed_evidence["releaseBuild"]["requireUpdaterSignatures"] is True
    assert signed_evidence["releaseBuild"]["releaseBaseUrlConfigured"] is True
    authenticode_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "authenticodeSignatures")
    assert authenticode_evidence["generatedByReleaseBuild"] is True
    assert authenticode_evidence["releaseBuildReport"].endswith("release-metadata\\authenticode.json")


def test_hybrid_release_readiness_runner_plans_required_rust_audio_sidecar_smoke(tmp_path: Path) -> None:
    sidecar_exe = tmp_path / "scriber-audio-sidecar.exe"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunRustAudioSidecarSmoke",
        "-RequireRustAudioSidecarSmoke",
        "-RustAudioSidecarDurationSec",
        "600",
        "-RustAudioSidecarSelectedDurationSec",
        "12",
        "-RustAudioSidecarPrewarmBeforeCapture",
        "-RustAudioSidecarPrewarmDurationSec",
        "0.75",
        "-RustAudioSidecarExe",
        str(sidecar_exe),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    rust_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioSidecarSmoke")
    assert rust_evidence["required"] is True
    assert "smoke_rust_audio_sidecar.py" in rust_evidence["producer"]
    assert rust_evidence["durationSec"] == 600
    assert rust_evidence["selectedDurationSec"] == 12
    assert rust_evidence["prebufferMs"] == 400
    assert rust_evidence["prewarmBeforeCapture"] is True
    assert rust_evidence["requirePrewarmAdoption"] is True
    assert rust_evidence["prewarmDurationSec"] == 0.75
    rust_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioSidecarSmoke")
    assert "smoke_rust_audio_sidecar.py" in rust_command["command"]
    assert "--mode wasapi" in rust_command["command"]
    assert "--duration-sec 600" in rust_command["command"]
    assert "--selected-duration-sec 12" in rust_command["command"]
    assert "--prebuffer-ms 400" in rust_command["command"]
    assert "--prewarm-before-capture" in rust_command["command"]
    assert "--prewarm-duration-sec 0.75" in rust_command["command"]
    assert "--sidecar-exe" in rust_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "microphoneMatrixValidation")
    assert matrix_evidence["requireRustEndpointInventory"] is True
    assert matrix_evidence["requireDeviceRefreshEvidence"] is True
    assert "--require-rust-endpoint-inventory" in matrix_command["command"]
    assert "--require-device-refresh-evidence" in matrix_command["command"]
    assert "--rust-audio-sidecar-report" in readiness_command["command"]
    assert "--require-rust-audio-sidecar-smoke" in readiness_command["command"]
    assert "--require-rust-endpoint-inventory" in readiness_command["command"]
    assert "--require-device-refresh-evidence" in readiness_command["command"]
    assert "--min-rust-audio-duration-sec 600" in readiness_command["command"]
    assert "--require-rust-audio-sidecar-prewarm-adoption" in readiness_command["command"]


def test_hybrid_release_readiness_runner_treats_sidecar_prewarm_adoption_as_required(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RustAudioSidecarPrewarmBeforeCapture",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    rust_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioSidecarSmoke")
    assert rust_evidence["required"] is True
    assert rust_evidence["requirePrewarmAdoption"] is True
    assert rust_evidence["producer"] == "required external report"
    rust_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioSidecarSmoke")
    assert "required external report" in rust_command["command"]
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    assert matrix_evidence["requireRustEndpointInventory"] is True
    assert matrix_evidence["requireDeviceRefreshEvidence"] is True
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--rust-audio-sidecar-report" in readiness_command["command"]
    assert "--require-rust-audio-sidecar-smoke" not in readiness_command["command"]
    assert "--require-rust-audio-sidecar-prewarm-adoption" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_rust_audio_prewarm_sidecar_smoke(tmp_path: Path) -> None:
    sidecar_exe = tmp_path / "scriber-audio-sidecar.exe"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunRustAudioPrewarmSidecarSmoke",
        "-RequireRustAudioPrewarmSidecarSmoke",
        "-RustAudioPrewarmSidecarMode",
        "wasapi",
        "-RustAudioPrewarmSidecarDurationSec",
        "2",
        "-RustAudioPrewarmSidecarPrebufferMs",
        "500",
        "-RustAudioSidecarExe",
        str(sidecar_exe),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    prewarm_evidence = next(
        entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioPrewarmSidecarSmoke"
    )
    assert prewarm_evidence["required"] is True
    assert "smoke_rust_audio_prewarm_sidecar.py" in prewarm_evidence["producer"]
    assert prewarm_evidence["mode"] == "wasapi"
    assert prewarm_evidence["durationSec"] == 2
    assert prewarm_evidence["prebufferMs"] == 500
    prewarm_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioPrewarmSidecarSmoke")
    assert "smoke_rust_audio_prewarm_sidecar.py" in prewarm_command["command"]
    assert "--mode wasapi" in prewarm_command["command"]
    assert "--duration-sec 2" in prewarm_command["command"]
    assert "--prebuffer-ms 500" in prewarm_command["command"]
    assert "--sidecar-exe" in prewarm_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "microphoneMatrixValidation")
    assert matrix_evidence["requireRustEndpointInventory"] is False
    assert matrix_evidence["requireDeviceRefreshEvidence"] is False
    assert "--require-rust-endpoint-inventory" not in matrix_command["command"]
    assert "--require-device-refresh-evidence" not in matrix_command["command"]
    assert "--rust-audio-prewarm-sidecar-report" in readiness_command["command"]
    assert "--require-rust-audio-prewarm-sidecar-smoke" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_long_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    sidecar_exe = tmp_path / "scriber-audio-sidecar.exe"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunRustAudioAppPrewarmSmoke",
        "-RequireRustAudioAppPrewarmSmoke",
        "-RustAudioAppPrewarmDurationSec",
        "600",
        "-RustAudioAppPrewarmPrewarmDurationSec",
        "1800",
        "-MinRustAudioAppPrewarmDurationSec",
        "600",
        "-MinRustAudioAppPrewarmPrewarmDurationSec",
        "1800",
        "-RustAudioSidecarExe",
        str(sidecar_exe),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    app_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert app_evidence["required"] is True
    assert "smoke_rust_audio_app_prewarm.py" in app_evidence["producer"]
    assert app_evidence["durationSec"] == 600
    assert app_evidence["prewarmDurationSec"] == 1800
    assert app_evidence["captureCycles"] == 1
    assert app_evidence["minDurationSec"] == 600
    assert app_evidence["minPrewarmDurationSec"] == 1800
    assert app_evidence["minCaptureCycles"] == 0
    app_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert "smoke_rust_audio_app_prewarm.py" in app_command["command"]
    assert "--duration-sec 600" in app_command["command"]
    assert "--prewarm-duration-sec 1800" in app_command["command"]
    assert "--capture-cycles 1" in app_command["command"]
    assert "--sidecar-exe" in app_command["command"]
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "microphoneMatrixValidation")
    assert matrix_evidence["requireRustEndpointInventory"] is True
    assert matrix_evidence["requireDeviceRefreshEvidence"] is True
    assert "--require-rust-endpoint-inventory" in matrix_command["command"]
    assert "--require-device-refresh-evidence" in matrix_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--rust-audio-app-prewarm-report" in readiness_command["command"]
    assert "--require-rust-audio-app-prewarm-smoke" in readiness_command["command"]
    assert "--min-rust-audio-app-prewarm-duration-sec 600" in readiness_command["command"]
    assert "--min-rust-audio-app-prewarm-prewarm-duration-sec 1800" in readiness_command["command"]
    assert "--require-rust-endpoint-inventory" in readiness_command["command"]
    assert "--require-device-refresh-evidence" in readiness_command["command"]


def test_hybrid_release_readiness_runner_treats_app_prewarm_min_duration_as_required(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-MinRustAudioAppPrewarmDurationSec",
        "600",
        "-MinRustAudioAppPrewarmPrewarmDurationSec",
        "1800",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    app_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert app_evidence["required"] is True
    assert app_evidence["producer"] == "required external report"
    assert app_evidence["minDurationSec"] == 600
    assert app_evidence["minPrewarmDurationSec"] == 1800
    assert app_evidence["minCaptureCycles"] == 0
    app_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert "required external report" in app_command["command"]
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "physicalMicrophoneMatrix")
    assert matrix_evidence["requireRustEndpointInventory"] is True
    assert matrix_evidence["requireDeviceRefreshEvidence"] is True
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--rust-audio-app-prewarm-report" in readiness_command["command"]
    assert "--require-rust-audio-app-prewarm-smoke" not in readiness_command["command"]
    assert "--min-rust-audio-app-prewarm-duration-sec 600" in readiness_command["command"]
    assert "--min-rust-audio-app-prewarm-prewarm-duration-sec 1800" in readiness_command["command"]


def test_hybrid_release_readiness_runner_treats_app_prewarm_min_cycles_as_required(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-MinRustAudioAppPrewarmCaptureCycles",
        "2",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    app_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert app_evidence["required"] is True
    assert app_evidence["producer"] == "required external report"
    assert app_evidence["minCaptureCycles"] == 2
    app_command = next(entry for entry in payload["commands"] if entry["name"] == "rustAudioAppPrewarmSmoke")
    assert "required external report" in app_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--rust-audio-app-prewarm-report" in readiness_command["command"]
    assert "--min-rust-audio-app-prewarm-cycles 2" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_recording_hot_path_comparison(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireRecordingHotPathComparison",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    comparison_evidence = next(
        entry for entry in payload["requiredEvidence"] if entry["name"] == "recordingHotPathPythonRustComparison"
    )
    assert comparison_evidence["required"] is True
    assert comparison_evidence["external"] is True
    assert comparison_evidence["report"].endswith("recording-hot-path-python-rust-comparison.json")
    assert "run_recording_hot_path_comparison.ps1" in comparison_evidence["producer"]
    assert "validate_recording_hot_path_comparison.py" in comparison_evidence["producer"]
    assert "same STT provider" in comparison_evidence["notes"]
    assert "inputReportRedaction" in comparison_evidence["notes"]
    assert "sameRecordingConfig" in comparison_evidence["notes"]
    assert "rustMidSessionClean" in comparison_evidence["notes"]
    assert "rustFramePipeFlow" in comparison_evidence["notes"]
    assert "rustNoDroppedFrames" in comparison_evidence["notes"]
    assert "rustActiveCaptureStable" in comparison_evidence["notes"]
    assert comparison_evidence["rustPrewarmAdoptionRequired"] is True
    assert "rustPrewarmAdoption" in comparison_evidence["notes"]
    assert "adopted Rust prewarm evidence" in comparison_evidence["notes"]
    assert "active rust-frame-pipe capture" in comparison_evidence["notes"]
    assert "fallback-circuit" in comparison_evidence["notes"]
    comparison_command = next(
        entry for entry in payload["commands"] if entry["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "required external report" in comparison_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--recording-hot-path-comparison-report" in readiness_command["command"]
    assert "--require-recording-hot-path-comparison" in readiness_command["command"]


def test_hybrid_release_readiness_runner_can_plan_recording_hot_path_comparison_run(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunRecordingHotPathComparison",
        "-RequireRecordingHotPathComparison",
        "-RecordingHotPathIterations",
        "4",
        "-RecordingHotPathSeconds",
        "3",
        "-RecordingHotPathTimeoutSec",
        "90",
        "-RecordingHotPathRustCaptureMode",
        "wasapi",
        "-RecordingHotPathHidden",
        "-RecordingHotPathDisableDevFallback",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runRecordingHotPathComparison"] is True
    comparison_evidence = next(
        entry for entry in payload["requiredEvidence"] if entry["name"] == "recordingHotPathPythonRustComparison"
    )
    assert comparison_evidence["required"] is True
    assert comparison_evidence["external"] is False
    assert comparison_evidence["producer"] == "scripts\\run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic"
    assert comparison_evidence["rustPrewarmAdoptionRequired"] is True
    assert comparison_evidence["iterations"] == 4
    assert comparison_evidence["recordSeconds"] == 3
    assert comparison_evidence["timeoutSec"] == 90
    assert comparison_evidence["rustCaptureMode"] == "wasapi"
    comparison_command = next(
        entry for entry in payload["commands"] if entry["name"] == "recordingHotPathPythonRustComparison"
    )
    command = comparison_command["command"]
    assert "run_recording_hot_path_comparison.ps1" in command
    assert "-RustAlwaysOnMic" in command
    assert "-RecordingHotPathIterations 4" in command
    assert "-RecordingHotPathSeconds 3" in command
    assert "-RecordingHotPathTimeoutSec 90" in command
    assert "-RustCaptureMode wasapi" in command
    assert "-Hidden" in command
    assert "-DisableDevFallback" in command
    assert "recording-hot-path-python-rust-comparison.json" in command
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--recording-hot-path-comparison-report" in readiness_command["command"]
    assert "--require-recording-hot-path-comparison" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_installed_live_recording_smoke(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireInstalledLiveRecordingSmoke",
        "-MinInstalledLiveRecordingDurationSec",
        "600",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    live_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "installedLiveRecordingSmoke")
    assert live_evidence["required"] is True
    assert live_evidence["external"] is True
    assert live_evidence["report"].endswith("installed-live-recording-smoke.json")
    assert live_evidence["minDurationSec"] == 600
    assert live_evidence["producer"] == "required external report"
    assert "provider-backed transcription quality" in live_evidence["notes"]
    live_command = next(entry for entry in payload["commands"] if entry["name"] == "installedLiveRecordingSmoke")
    assert "required external report" in live_command["command"]
    assert "RunInstallerLiveRecordingSmoke" in live_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--installed-live-recording-smoke-report" in readiness_command["command"]
    assert "--require-installed-live-recording-smoke" in readiness_command["command"]
    assert "--min-installed-live-recording-duration-sec 600" in readiness_command["command"]


def test_hybrid_release_readiness_runner_treats_installed_live_recording_min_duration_as_required(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-MinInstalledLiveRecordingDurationSec",
        "600",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["requireInstalledLiveRecordingSmoke"] is False
    live_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "installedLiveRecordingSmoke")
    assert live_evidence["required"] is True
    assert live_evidence["minDurationSec"] == 600
    live_command = next(entry for entry in payload["commands"] if entry["name"] == "installedLiveRecordingSmoke")
    assert "required external report" in live_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--installed-live-recording-smoke-report" in readiness_command["command"]
    assert "--require-installed-live-recording-smoke" not in readiness_command["command"]
    assert "--min-installed-live-recording-duration-sec 600" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_installed_live_recording_rust_audio(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireInstalledLiveRecordingRustAudio",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["requireInstalledLiveRecordingSmoke"] is False
    assert payload["requireInstalledLiveRecordingRustAudio"] is True
    live_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "installedLiveRecordingSmoke")
    assert live_evidence["required"] is True
    assert live_evidence["requireRustAudio"] is True
    assert live_evidence["rustPrewarmAdoptionRequired"] is True
    assert "adopted Rust prewarm evidence" in live_evidence["notes"]
    assert "-InstallerLiveRecordingAudioEngine rust-prototype" in live_evidence["producer"]
    assert "-LiveRecordingRustAudioCaptureMode wasapi" in live_evidence["producer"]
    live_command = next(entry for entry in payload["commands"] if entry["name"] == "installedLiveRecordingSmoke")
    assert "required external report" in live_command["command"]
    assert "-InstallerLiveRecordingAudioEngine rust-prototype" in live_command["command"]
    assert "-LiveRecordingRustAudioCaptureMode wasapi" in live_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--installed-live-recording-smoke-report" in readiness_command["command"]
    assert "--require-installed-live-recording-smoke" not in readiness_command["command"]
    assert "--require-installed-live-recording-rust-audio" in readiness_command["command"]


def test_hybrid_release_readiness_runner_can_run_installed_live_recording_smoke(tmp_path: Path) -> None:
    installer = tmp_path / "Scriber_0.1.0_x64-setup.exe"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunInstalledLiveRecordingSmoke",
        "-InstalledLiveRecordingInstallerPath",
        str(installer),
        "-RequireInstalledLiveRecordingRustAudio",
        "-InstalledLiveRecordingDurationSec",
        "600",
        "-InstalledLiveRecordingEnvFile",
        str(tmp_path / ".env"),
        "-InstalledLiveRecordingDefaultStt",
        "soniox",
        "-InstalledLiveRecordingSonioxMode",
        "realtime",
        "-InstalledLiveRecordingDisableTextInjection",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runInstalledLiveRecordingSmoke"] is True
    assert payload["installedLiveRecordingInstallerPath"].endswith("Scriber_0.1.0_x64-setup.exe")
    assert payload["installedLiveRecordingDurationSec"] == 600
    assert payload["installedLiveRecordingEnvFile"].endswith(".env")
    assert payload["installedLiveRecordingDefaultStt"] == "soniox"
    assert payload["installedLiveRecordingSonioxMode"] == "realtime"
    assert payload["installedLiveRecordingAudioEngine"] == "rust-prototype"
    assert payload["installedLiveRecordingRustAudioCaptureMode"] == "wasapi"
    assert payload["installedLiveRecordingMicAlwaysOn"] is True

    live_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "installedLiveRecordingSmoke")
    assert live_evidence["required"] is True
    assert live_evidence["external"] is False
    assert live_evidence["producer"] == "scripts\\smoke_windows_installer.ps1"
    assert live_evidence["installerPath"].endswith("Scriber_0.1.0_x64-setup.exe")
    assert live_evidence["envFile"].endswith(".env")
    assert live_evidence["defaultStt"] == "soniox"
    assert live_evidence["sonioxMode"] == "realtime"
    assert live_evidence["audioEngine"] == "rust-prototype"
    assert live_evidence["rustAudioCaptureMode"] == "wasapi"
    assert live_evidence["rustPrewarmAdoptionRequired"] is True
    assert live_evidence["micAlwaysOn"] is True
    assert live_evidence["disableTextInjection"] is True

    live_command = next(entry for entry in payload["commands"] if entry["name"] == "installedLiveRecordingSmoke")
    assert "powershell" in live_command["command"]
    assert "smoke_windows_installer.ps1" in live_command["command"]
    assert "-InstallerPath" in live_command["command"]
    assert "-OutputPath" in live_command["command"]
    assert "-LiveRecordingDurationSec 600" in live_command["command"]
    assert "-LiveRecordingEnvFile" in live_command["command"]
    assert "-LiveRecordingDefaultStt soniox" in live_command["command"]
    assert "-LiveRecordingSonioxMode realtime" in live_command["command"]
    assert "-LiveRecordingAudioEngine rust-prototype" in live_command["command"]
    assert "-LiveRecordingRustAudioCaptureMode wasapi" in live_command["command"]
    assert "-LiveRecordingMicAlwaysOn" in live_command["command"]
    assert "-DisableLiveTextInjection" in live_command["command"]


def test_hybrid_release_readiness_runner_plans_full_rust_audio_promotion_gate(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireRustAudioPromotionReadiness",
        "-RustAudioSidecarDurationSec",
        "30",
        "-RustAudioAppPrewarmDurationSec",
        "30",
        "-RustAudioAppPrewarmPrewarmDurationSec",
        "30",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["requireRustAudioPromotionReadiness"] is True
    assert payload["requireRustAudioSidecarSmoke"] is True
    assert payload["requireRustAudioAppPrewarmSmoke"] is True
    assert payload["requireInstalledLiveRecordingSmoke"] is True
    assert payload["requireInstalledLiveRecordingRustAudio"] is True
    assert payload["requireRecordingHotPathComparison"] is True
    assert payload["requireRustEndpointInventory"] is True
    assert payload["requireDeviceRefreshEvidence"] is True
    assert payload["rustAudioSidecarDurationSec"] == 600
    assert payload["rustAudioSidecarPrewarmBeforeCapture"] is True
    assert payload["rustAudioAppPrewarmDurationSec"] == 600
    assert payload["rustAudioAppPrewarmPrewarmDurationSec"] == 1800
    assert payload["rustAudioAppPrewarmCaptureCycles"] == 2
    assert payload["minRustAudioAppPrewarmDurationSec"] == 600
    assert payload["minRustAudioAppPrewarmPrewarmDurationSec"] == 1800
    assert payload["minRustAudioAppPrewarmCaptureCycles"] == 2
    assert payload["minInstalledLiveRecordingDurationSec"] == 600

    by_name = {entry["name"]: entry for entry in payload["requiredEvidence"]}
    assert by_name["rustAudioSidecarSmoke"]["required"] is True
    assert by_name["rustAudioSidecarSmoke"]["producer"] == "required external report"
    assert by_name["rustAudioSidecarSmoke"]["durationSec"] == 600
    assert by_name["rustAudioSidecarSmoke"]["prewarmBeforeCapture"] is True
    assert by_name["rustAudioSidecarSmoke"]["requirePrewarmAdoption"] is True
    assert by_name["rustAudioAppPrewarmSmoke"]["required"] is True
    assert by_name["rustAudioAppPrewarmSmoke"]["producer"] == "required external report"
    assert by_name["rustAudioAppPrewarmSmoke"]["durationSec"] == 600
    assert by_name["rustAudioAppPrewarmSmoke"]["prewarmDurationSec"] == 1800
    assert by_name["rustAudioAppPrewarmSmoke"]["captureCycles"] == 2
    assert by_name["rustAudioAppPrewarmSmoke"]["minCaptureCycles"] == 2
    assert by_name["recordingHotPathPythonRustComparison"]["required"] is True
    assert by_name["recordingHotPathPythonRustComparison"]["rustAlwaysOnMicRequired"] is True
    assert by_name["recordingHotPathPythonRustComparison"]["rustPrewarmAdoptionRequired"] is True
    assert "inputReportRedaction" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "sameRecordingConfig" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustAlwaysOnMic" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustMidSessionClean" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustFramePipeFlow" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustNoDroppedFrames" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustActiveCaptureStable" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert "rustPrewarmAdoption" in by_name["recordingHotPathPythonRustComparison"]["notes"]
    assert by_name["installedLiveRecordingSmoke"]["required"] is True
    assert by_name["installedLiveRecordingSmoke"]["minDurationSec"] == 600
    assert by_name["installedLiveRecordingSmoke"]["requireRustAudio"] is True
    assert by_name["installedLiveRecordingSmoke"]["rustPrewarmAdoptionRequired"] is True
    assert by_name["physicalMicrophoneMatrix"]["requireRustEndpointInventory"] is True
    assert by_name["physicalMicrophoneMatrix"]["requireDeviceRefreshEvidence"] is True

    command_by_name = {entry["name"]: entry["command"] for entry in payload["commands"]}
    assert "--require-rust-endpoint-inventory" in command_by_name["microphoneMatrixValidation"]
    assert "--require-device-refresh-evidence" in command_by_name["microphoneMatrixValidation"]
    assert "required external report" in command_by_name["rustAudioSidecarSmoke"]
    assert "RunRustAudioSidecarSmoke" in command_by_name["rustAudioSidecarSmoke"]
    assert "required external report" in command_by_name["rustAudioAppPrewarmSmoke"]
    assert "RunRustAudioAppPrewarmSmoke" in command_by_name["rustAudioAppPrewarmSmoke"]
    assert "required external report" in command_by_name["recordingHotPathPythonRustComparison"]
    assert "run_recording_hot_path_comparison.ps1 -RustAlwaysOnMic" in command_by_name["recordingHotPathPythonRustComparison"]
    assert "required external report" in command_by_name["installedLiveRecordingSmoke"]
    readiness_command = command_by_name["hybridReleaseReadiness"]
    assert "--require-rust-audio-sidecar-smoke" in readiness_command
    assert "--min-rust-audio-duration-sec 600" in readiness_command
    assert "--require-rust-audio-sidecar-prewarm-adoption" in readiness_command
    assert "--require-rust-audio-app-prewarm-smoke" in readiness_command
    assert "--min-rust-audio-app-prewarm-duration-sec 600" in readiness_command
    assert "--min-rust-audio-app-prewarm-prewarm-duration-sec 1800" in readiness_command
    assert "--min-rust-audio-app-prewarm-cycles 2" in readiness_command
    assert "--require-installed-live-recording-smoke" in readiness_command
    assert "--require-installed-live-recording-rust-audio" in readiness_command
    assert "--min-installed-live-recording-duration-sec 600" in readiness_command
    assert "--require-recording-hot-path-comparison" in readiness_command
    assert "--require-rust-endpoint-inventory" in readiness_command
    assert "--require-device-refresh-evidence" in readiness_command


def test_hybrid_release_readiness_runner_plans_required_tauri_text_injection_smoke(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireTauriTextInjectionSmoke",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    injection_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "tauriTextInjectionSmoke")
    assert injection_evidence["required"] is True
    assert injection_evidence["external"] is True
    assert injection_evidence["report"].endswith("tauri-text-injection-smoke.json")
    assert "smoke_text_injection_target.py --method tauri" in injection_evidence["producer"]
    assert "strict SCRIBER_INJECT_METHOD=tauri" in injection_evidence["notes"]
    injection_command = next(entry for entry in payload["commands"] if entry["name"] == "tauriTextInjectionSmoke")
    assert "required external report" in injection_command["command"]
    assert "smoke_text_injection_target.py --method tauri" in injection_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--tauri-text-injection-smoke-report" in readiness_command["command"]
    assert "--require-tauri-text-injection-smoke" in readiness_command["command"]


def test_hybrid_release_readiness_runner_can_plan_tauri_text_injection_smoke_run(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunTauriTextInjectionSmoke",
        "-RequireTauriTextInjectionSmoke",
        "-TauriTextInjectionSmokeText",
        "Scriber Tauri smoke text",
        "-TauriTextInjectionTargetTitle",
        "Scriber Target",
        "-TauriTextInjectionSettleSec",
        "1.5",
        "-TauriTextInjectionTimeoutSec",
        "8",
        "-TauriTextInjectionSkipTargetClick",
        "-TauriTextInjectionPasteRestoreDelayMs",
        "1200",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runTauriTextInjectionSmoke"] is True
    smoke_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "tauriTextInjectionSmoke")
    assert smoke_evidence["required"] is True
    assert smoke_evidence["external"] is False
    assert smoke_evidence["producer"] == "scripts\\smoke_text_injection_target.py --method tauri"
    assert smoke_evidence["textChars"] == len("Scriber Tauri smoke text")
    assert smoke_evidence["targetTitle"] == "Scriber Target"
    assert smoke_evidence["timeoutSec"] == 8
    assert smoke_evidence["skipTargetClick"] is True
    smoke_command = next(entry for entry in payload["commands"] if entry["name"] == "tauriTextInjectionSmoke")
    assert "smoke_text_injection_target.py" in smoke_command["command"]
    assert "--method tauri" in smoke_command["command"]
    assert '--text "Scriber Tauri smoke text"' in smoke_command["command"]
    assert '--target-title "Scriber Target"' in smoke_command["command"]
    assert "--settle-sec 1.5" in smoke_command["command"]
    assert "--timeout-sec 8" in smoke_command["command"]
    assert "--paste-restore-delay-ms 1200" in smoke_command["command"]
    assert "--skip-target-click" in smoke_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--tauri-text-injection-smoke-report" in readiness_command["command"]
    assert "--require-tauri-text-injection-smoke" in readiness_command["command"]


def test_hybrid_release_readiness_runner_plans_required_tauri_text_injection_matrix(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RequireTauriTextInjectionMatrix",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "tauriTextInjectionMatrix")
    assert matrix_evidence["required"] is True
    assert matrix_evidence["external"] is True
    assert matrix_evidence["report"].endswith("tauri-text-injection-matrix.json")
    assert "build_tauri_text_injection_matrix.py" in matrix_evidence["producer"]
    assert "manual target-app runs" in matrix_evidence["producer"]
    assert "Notepad, Word, Outlook" in matrix_evidence["notes"]
    assert "preDelayMode=auto" in matrix_evidence["notes"]
    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "tauriTextInjectionMatrix")
    assert "required external report" in matrix_command["command"]
    assert "build_tauri_text_injection_matrix.py" in matrix_command["command"]
    assert "target-app matrix evidence" in matrix_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--tauri-text-injection-matrix-report" in readiness_command["command"]
    assert "--require-tauri-text-injection-matrix" in readiness_command["command"]


def test_hybrid_release_readiness_runner_can_plan_tauri_text_injection_matrix_builder(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "text-injection-reports"
    word_report = input_dir / "word.json"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-RunTauriTextInjectionMatrixBuilder",
        "-RequireTauriTextInjectionMatrix",
        "-TauriTextInjectionMatrixInputDir",
        str(input_dir),
        "-TauriTextInjectionMatrixScenario",
        f"word={word_report}",
        "-TauriTextInjectionMatrixUnsupportedOptional",
        "remote-desktop=Remote Desktop is not available on this test machine",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runTauriTextInjectionMatrixBuilder"] is True
    assert payload["tauriTextInjectionMatrixInputDir"] == str(input_dir)
    matrix_evidence = next(entry for entry in payload["requiredEvidence"] if entry["name"] == "tauriTextInjectionMatrix")
    assert matrix_evidence["required"] is True
    assert matrix_evidence["external"] is False
    assert matrix_evidence["producer"] == "scripts\\build_tauri_text_injection_matrix.py"
    assert matrix_evidence["inputDir"] == str(input_dir)
    assert matrix_evidence["scenarioOverrides"] == [f"word={word_report}"]
    assert matrix_evidence["unsupportedOptional"] == [
        "remote-desktop=Remote Desktop is not available on this test machine"
    ]
    assert matrix_evidence["inputReportsExternal"] is True
    matrix_command = next(entry for entry in payload["commands"] if entry["name"] == "tauriTextInjectionMatrix")
    assert "build_tauri_text_injection_matrix.py" in matrix_command["command"]
    assert "--input-dir" in matrix_command["command"]
    assert str(input_dir) in matrix_command["command"]
    assert "--scenario" in matrix_command["command"]
    assert str(word_report) in matrix_command["command"]
    assert "--unsupported-optional" in matrix_command["command"]
    assert "remote-desktop=Remote Desktop is not available on this test machine" in matrix_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--tauri-text-injection-matrix-report" in readiness_command["command"]
    assert "--require-tauri-text-injection-matrix" in readiness_command["command"]


def test_hybrid_release_readiness_runner_requires_authenticode_paths_before_backend_work(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-HardwareInputDir",
        str(tmp_path),
    )

    assert result.returncode == 1
    assert "-AuthenticodePath is required" in result.stderr


def test_hybrid_release_readiness_runner_can_reuse_existing_external_reports(tmp_path: Path) -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-HardwareInputDir",
        str(tmp_path),
        "-UseExistingMediaPreparationReport",
        "-UseExistingRuntimeDependencyFootprintReport",
        "-UseExistingAuthenticodeReport",
        "-UseExistingUpdaterPublicationReport",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "reuse" in payload["commands"][1]["command"]
    assert "updater-publication.json" in payload["commands"][1]["command"]
    assert "reuse" in payload["commands"][2]["command"]
    assert "media-preparation-smoke.json" in payload["commands"][2]["command"]
    assert "reuse" in payload["commands"][3]["command"]
    assert "runtime-dependency-footprint.json" in payload["commands"][3]["command"]
    assert payload["commands"][4]["command"] == "not requested"
    assert payload["commands"][5]["command"] == "not requested"
    assert payload["commands"][6]["command"] == "not requested"
    assert payload["commands"][7]["command"] == "not requested"
    assert payload["commands"][8]["command"] == "not requested"
    assert payload["commands"][9]["command"] == "not requested"
    assert payload["commands"][10]["command"] == "not requested"
    assert "reuse" in payload["commands"][11]["command"]
    assert "authenticode.json" in payload["commands"][11]["command"]

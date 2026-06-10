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
        "publishedUpdaterManifest",
        "authenticodeSignatures",
        "hybridReleaseReadinessAggregate",
    ]
    hardware_evidence = payload["requiredEvidence"][0]
    assert hardware_evidence["external"] is True
    assert len(hardware_evidence["expectedArtifacts"]) == 8
    assert any("favorite-fallback" in artifact for artifact in hardware_evidence["expectedArtifacts"])
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
    assert "app-level RustAudioPrewarmManager" in app_prewarm_evidence["notes"]
    comparison_evidence = payload["requiredEvidence"][7]
    assert comparison_evidence["required"] is False
    assert comparison_evidence["report"].endswith("recording-hot-path-python-rust-comparison.json")
    assert "run_recording_hot_path_comparison.ps1" in comparison_evidence["producer"]
    assert "validate_recording_hot_path_comparison.py" in comparison_evidence["producer"]
    assert "provider-backed Python and Rust reports" in comparison_evidence["producer"]
    publication_evidence = payload["requiredEvidence"][8]
    assert "final redirect URL" in publication_evidence["notes"]
    authenticode_evidence = payload["requiredEvidence"][9]
    assert authenticode_evidence["expectedPublisher"] == "Scriber Publisher"
    assert authenticode_evidence["requireTimestamp"] is True
    assert "validate_microphone_hardware_matrix.py" in payload["commands"][0]["command"]
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
    assert "validate_windows_authenticode.ps1" in payload["commands"][8]["command"]
    assert "validate_hybrid_release_readiness.py" in payload["commands"][9]["command"]
    assert "--media-preparation-report" in payload["commands"][9]["command"]
    assert "media-preparation-smoke.json" in payload["commands"][9]["command"]
    assert "--runtime-dependency-footprint-report" in payload["commands"][9]["command"]
    assert "runtime-dependency-footprint.json" in payload["commands"][9]["command"]
    assert "--require-authenticode-timestamp" in payload["commands"][9]["command"]
    assert written == payload


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
    assert "--require-rust-endpoint-inventory" in matrix_command["command"]
    assert "--rust-audio-sidecar-report" in readiness_command["command"]
    assert "--require-rust-audio-sidecar-smoke" in readiness_command["command"]
    assert "--min-rust-audio-duration-sec 600" in readiness_command["command"]


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
    assert "--require-rust-endpoint-inventory" not in matrix_command["command"]
    assert "--rust-audio-prewarm-sidecar-report" in readiness_command["command"]
    assert "--require-rust-audio-prewarm-sidecar-smoke" in readiness_command["command"]


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
    assert "active rust-frame-pipe capture" in comparison_evidence["notes"]
    comparison_command = next(
        entry for entry in payload["commands"] if entry["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "required external report" in comparison_command["command"]
    readiness_command = next(entry for entry in payload["commands"] if entry["name"] == "hybridReleaseReadiness")
    assert "--recording-hot-path-comparison-report" in readiness_command["command"]
    assert "--require-recording-hot-path-comparison" in readiness_command["command"]


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
    assert "reuse" in payload["commands"][8]["command"]
    assert "authenticode.json" in payload["commands"][8]["command"]

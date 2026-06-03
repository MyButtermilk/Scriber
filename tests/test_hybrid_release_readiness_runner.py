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
    publication_evidence = payload["requiredEvidence"][4]
    assert "final redirect URL" in publication_evidence["notes"]
    authenticode_evidence = payload["requiredEvidence"][5]
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
    assert "validate_windows_authenticode.ps1" in payload["commands"][4]["command"]
    assert "validate_hybrid_release_readiness.py" in payload["commands"][5]["command"]
    assert "--media-preparation-report" in payload["commands"][5]["command"]
    assert "media-preparation-smoke.json" in payload["commands"][5]["command"]
    assert "--runtime-dependency-footprint-report" in payload["commands"][5]["command"]
    assert "runtime-dependency-footprint.json" in payload["commands"][5]["command"]
    assert "--require-authenticode-timestamp" in payload["commands"][5]["command"]
    assert written == payload


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
    assert "reuse" in payload["commands"][4]["command"]
    assert "authenticode.json" in payload["commands"][4]["command"]

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "run_recording_hot_path_comparison.ps1"


def powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is required for the recording hot-path comparison runner.")
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


def test_recording_hot_path_comparison_runner_powershell_parses() -> None:
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


def test_recording_hot_path_comparison_runner_plan_only_wires_python_and_rust_runs(tmp_path: Path) -> None:
    output_dir = REPO_ROOT / "tmp" / f"pytest-{tmp_path.name}"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(output_dir),
        "-RecordingHotPathIterations",
        "2",
        "-RecordingHotPathSeconds",
        "3",
        "-RecordingHotPathSpeechPrompt",
        "Scriber comparison evidence",
        "-RustAlwaysOnMic",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["planOnly"] is True
    assert payload["rustCaptureMode"] == "wasapi"
    assert payload["rustAlwaysOnMic"] is True
    assert payload["pythonHotPathReport"].endswith("python-recording-hot-path-baseline-recording-hot-path-1.json")
    assert payload["rustHotPathReport"].endswith("rust-recording-hot-path-baseline-recording-hot-path-1.json")
    assert payload["comparisonOutput"].endswith("recording-hot-path-python-rust-comparison.json")

    commands = {entry["name"]: entry for entry in payload["commands"]}
    assert set(commands) == {
        "pythonRecordingHotPath",
        "rustRecordingHotPath",
        "comparisonValidation",
    }

    python_command = commands["pythonRecordingHotPath"]["command"]
    rust_command = commands["rustRecordingHotPath"]["command"]
    comparison_command = commands["comparisonValidation"]["command"]

    assert commands["pythonRecordingHotPath"]["environment"]["SCRIBER_AUDIO_ENGINE"] == "python"
    assert commands["rustRecordingHotPath"]["environment"]["SCRIBER_AUDIO_ENGINE"] == "rust-prototype"
    assert commands["rustRecordingHotPath"]["environment"]["SCRIBER_RUST_AUDIO_WASAPI_CAPTURE"] == "1"
    assert commands["rustRecordingHotPath"]["environment"]["SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE"] == ""
    assert commands["rustRecordingHotPath"]["environment"]["SCRIBER_MIC_ALWAYS_ON"] == "1"

    assert "measure_hybrid_baseline.ps1" in python_command
    assert "measure_hybrid_baseline.ps1" in rust_command
    assert "-RecordHotPathSamples" in python_command
    assert "-RecordHotPathSamples" in rust_command
    assert "-RecordingHotPathIterations 2" in python_command
    assert "-RecordingHotPathSeconds 3" in python_command
    assert "-RequireRecordingHotPathProviderTranscript" in python_command
    assert "-RequireRecordingHotPathProviderTranscript" in rust_command
    assert "-RequireRecordingHotPathRustAudio" not in python_command
    assert "-RequireRecordingHotPathRustAudio" in rust_command
    assert "-SkipUploadExportBenchmark" in python_command
    assert "-SkipWsBenchmark" in python_command
    assert "-SkipHistoryScrollBenchmark" in python_command
    assert "validate_recording_hot_path_comparison.py" in comparison_command
    assert "--python-report" in comparison_command
    assert "--rust-report" in comparison_command
    assert "--min-samples-per-report 2" in comparison_command
    assert "--output" in comparison_command


def test_recording_hot_path_comparison_runner_plan_only_can_select_synthetic_capture(tmp_path: Path) -> None:
    output_dir = REPO_ROOT / "tmp" / f"pytest-{tmp_path.name}"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER_SCRIPT),
        "-PlanOnly",
        "-OutputDir",
        str(output_dir),
        "-RustCaptureMode",
        "synthetic",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    commands = {entry["name"]: entry for entry in payload["commands"]}
    assert "-RecordingHotPathIterations 3" in commands["pythonRecordingHotPath"]["command"]
    assert "--min-samples-per-report 3" in commands["comparisonValidation"]["command"]
    rust_env = next(entry for entry in payload["commands"] if entry["name"] == "rustRecordingHotPath")[
        "environment"
    ]
    assert rust_env["SCRIBER_RUST_AUDIO_WASAPI_CAPTURE"] == ""
    assert rust_env["SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE"] == "1"

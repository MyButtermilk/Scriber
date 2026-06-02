from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.measure_recording_hot_path_baseline import (
    build_summary,
    iteration_text_target_path,
)


def test_recording_hot_path_baseline_script_validate_only_writes_artifact(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "recording-hot-path-baseline.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_recording_hot_path_baseline.py",
            "--validate-only",
            "--text-target-file",
            str(tmp_path / "target.txt"),
            "--speech-prompt-text",
            "Scriber validation prompt",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["requirements"]["hotkey_to_recording_state"]["status"] == "measured"
    assert payload["summary"]["requirements"]["hotkey_to_first_audio_frame"]["status"] == "measured"
    stop_requirement = payload["summary"]["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "measured"
    assert stop_requirement["durations"]["p95Ms"] == 0.0
    assert stop_requirement["alreadyInjectedBeforeStopSamples"] == 1


def test_recording_hot_path_text_target_path_is_unique_per_iteration(tmp_path: Path):
    target = tmp_path / "capture.txt"

    assert iteration_text_target_path(str(target), 1, 1) == target
    assert iteration_text_target_path(str(target), 1, 2) == tmp_path / "capture.iteration-1.txt"
    assert iteration_text_target_path(str(target), 2, 2) == tmp_path / "capture.iteration-2.txt"


def test_recording_hot_path_text_target_keeps_focus_during_measurement():
    repo_root = Path(__file__).resolve().parents[2]
    script = (
        repo_root / "scripts" / "measure_recording_hot_path_baseline.py"
    ).read_text(encoding="utf-8")

    assert 'root.attributes("-topmost", True)' in script
    assert "root.after(500, focus_window)" in script


def test_recording_hot_path_summary_measures_text_injection_after_stop():
    summary = build_summary(
        [
            {
                "segments": {
                    "stop_requested_to_first_paste_ms": 82.5,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "measured"
    assert stop_requirement["durations"]["p95Ms"] == 82.5
    assert stop_requirement["afterStopInjectionSamples"] == 1
    assert stop_requirement["alreadyInjectedBeforeStopSamples"] == 0


def test_recording_hot_path_summary_treats_text_before_stop_as_zero_wait():
    summary = build_summary(
        [
            {
                "segments": {
                    "first_paste_to_stop_requested_ms": 220.0,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "measured"
    assert stop_requirement["durations"]["p95Ms"] == 0.0
    assert stop_requirement["afterStopInjectionSamples"] == 0
    assert stop_requirement["alreadyInjectedBeforeStopSamples"] == 1


def test_hybrid_baseline_runner_wires_recording_hot_path_benchmark():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    assert "measure_recording_hot_path_baseline.py" in script
    assert "Invoke-RecordingHotPathBenchmark" in script
    assert "RecordHotPathSamples" in script
    assert "hotkey_to_recording_state" in script
    assert "hotkey_to_first_audio_frame" in script
    assert "stop_to_text_injection" in script
    assert "RecordingHotPathTextTargetFile" in script
    assert "RecordingHotPathSpeechPrompt" in script
    assert "--text-target-file" in script
    assert "--speech-prompt-text" in script
    assert "Convert-ToProcessArgument" in script


def test_hybrid_baseline_recording_artifact_is_persistent_sibling():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    assert "[string]$BaselineOutputPath" in script
    assert '$baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaselineOutputPath)' in script
    assert '$baseName-recording-hot-path-$Iteration.json' in script
    assert "-BaselineOutputPath $OutputPath" in script
    assert "-BaselineOutputPath $BaselineOutputPath" in script


def test_hybrid_baseline_recording_samples_do_not_fall_back_to_old_metric_rows():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    recording_branch = script.split('if ($RecordHotPathSamples) {', 1)[1].split(
        'if ($segmentNames -contains $SegmentName)', 1
    )[0]
    assert 'return "missing_samples"' in recording_branch

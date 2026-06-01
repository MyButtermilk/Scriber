from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_recording_hot_path_baseline_script_validate_only_writes_artifact(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "recording-hot-path-baseline.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_recording_hot_path_baseline.py",
            "--validate-only",
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
    assert payload["summary"]["requirements"]["stop_to_text_injection"]["status"] == "missing_text_injection"


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

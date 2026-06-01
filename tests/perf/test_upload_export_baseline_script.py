from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_upload_export_baseline_script_writes_benchmark_artifact(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "upload-export-baseline.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_upload_export_baseline.py",
            "--upload-files",
            "2",
            "--upload-size-mb",
            "0.05",
            "--upload-chunk-mb",
            "0.02",
            "--export-iterations",
            "1",
            "--export-concurrency",
            "1",
            "--export-paragraphs",
            "5",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["upload"]["fileCount"] == 2
    assert payload["summary"]["upload"]["ok"] is True
    assert payload["summary"]["export"]["totalExports"] == 2
    assert payload["summary"]["export"]["byFormat"]["pdf"]["ok"] is True
    assert payload["summary"]["export"]["byFormat"]["docx"]["ok"] is True


def test_hybrid_baseline_runner_wires_upload_export_benchmark():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    assert "measure_upload_export_baseline.py" in script
    assert "Invoke-UploadExportBenchmark" in script
    assert "upload_export_under_load" in script
    assert "not_automated_yet" not in script.split(
        "upload_export_under_load", maxsplit=1
    )[1].split("websocket_events_and_json_serialize_cost", maxsplit=1)[0]

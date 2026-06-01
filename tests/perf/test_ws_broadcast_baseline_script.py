from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_ws_broadcast_baseline_script_writes_benchmark_artifact(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "ws-baseline.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_ws_broadcast_baseline.py",
            "--iterations",
            "2",
            "--warmup",
            "1",
            "--clients",
            "1,2",
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
    assert payload["summary"]["jsonSerialize"]["iterations"] == 2
    assert payload["summary"]["broadcastNoClients"]["clientCount"] == 0
    assert [
        item["clientCount"] for item in payload["summary"]["broadcastWithClients"]
    ] == [1, 2]
    assert payload["summary"]["broadcastWithClients"][0]["sendCalls"] == 3
    assert payload["summary"]["broadcastWithClients"][1]["sendCalls"] == 6


def test_hybrid_baseline_runner_wires_websocket_benchmark():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    assert "measure_ws_broadcast_baseline.py" in script
    assert "Invoke-WebSocketBroadcastBenchmark" in script
    assert "websocket_events_and_json_serialize_cost" in script
    assert "not_automated_yet" not in script.split(
        "websocket_events_and_json_serialize_cost", maxsplit=1
    )[1].split("history_scroll_many_transcripts", maxsplit=1)[0]

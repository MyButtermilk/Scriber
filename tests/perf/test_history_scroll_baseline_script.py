from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_browser_startup_can_write_more_than_a_pipe_buffer(monkeypatch, tmp_path):
    from scripts import measure_history_scroll_baseline as browser_tools

    popen = subprocess.Popen

    def noisy_browser(_args, **kwargs):
        return popen([sys.executable, "-c", "import sys; sys.stdout.write('x' * 262144); sys.stdout.flush()"], **kwargs)

    monkeypatch.setattr(browser_tools.subprocess, "Popen", noisy_browser)
    process = browser_tools.start_browser("inert", 12345, tmp_path, headed=False)
    try:
        assert process.wait(timeout=10) == 0
        assert (tmp_path / "browser-process.log").stat().st_size == 262144
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_history_scroll_baseline_script_validate_only_writes_artifact(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "history-scroll-baseline.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_history_scroll_baseline.py",
            "--validate-only",
            "--items",
            "20",
            "--routes",
            "/",
            "--views",
            "list",
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
    assert payload["summary"]["scenarioCount"] == 1
    assert payload["summary"]["itemCount"] == 20
    assert payload["summary"]["virtualized"] is True
    assert payload["scenarios"][0]["validateOnly"] is True


def test_hybrid_baseline_runner_wires_history_scroll_benchmark():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(encoding="utf-8")

    assert "measure_history_scroll_baseline.py" in script
    assert "Invoke-HistoryScrollBenchmark" in script
    assert "history_scroll_many_transcripts" in script
    assert (
        "not_automated_yet"
        not in script.split("history_scroll_many_transcripts", maxsplit=1)[1].split(")", maxsplit=1)[0]
    )

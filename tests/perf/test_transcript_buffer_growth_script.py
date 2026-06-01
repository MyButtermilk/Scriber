from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_transcript_buffer_growth_script_guards_long_session_shape(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "transcript-buffer-growth.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_transcript_buffer_growth.py",
            "--segments",
            "120",
            "--segment-chars",
            "64",
            "--metadata-read-interval",
            "7",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["segments"] == 120
    assert payload["preMaterializeContentChars"] == 64
    assert payload["pendingBeforeMaterialize"] == 119
    assert payload["materializedContentChars"] == payload["expectedContentChars"]
    assert payload["metadataContentLeaked"] is False
    assert payload["checks"]["appendDidNotMaterializePendingSegments"] is True
    assert payload["checks"]["pendingSegmentsClearedAfterMaterialize"] is True


def test_transcript_buffer_growth_defaults_model_30_minute_session() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "check_transcript_buffer_growth.py").read_text(
        encoding="utf-8"
    )

    assert 'parser.add_argument("--segments", type=int, default=1800)' in script
    assert "one final segment per second over 30 minutes" in script

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_rust_audio_prewarm_sidecar import validate_prewarm_metrics


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rust_audio_prewarm_sidecar_smoke_script_documents_prewarm_contract() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_rust_audio_prewarm_sidecar.py").read_text(
        encoding="utf-8"
    )

    assert "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE" in script
    assert "SCRIBER_RUST_AUDIO_DISABLE_WASAPI_CAPTURE" in script
    assert "normal WASAPI capture is enabled by default" in script
    assert "--mode" in script
    assert "prewarmStart" in script
    assert "prewarmStop" in script
    assert "prewarmId" in script
    assert "totalBlocksObserved" in script
    assert "bufferedAudioFrames" in script
    assert "--prebuffer-ms" in script


def test_rust_audio_prewarm_sidecar_smoke_plan_only_writes_artifact(tmp_path: Path) -> None:
    output = tmp_path / "rust-audio-prewarm-sidecar-plan.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_rust_audio_prewarm_sidecar.py",
            "--plan-only",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout_payload = json.loads(result.stdout)
    written_payload = json.loads(output.read_text(encoding="utf-8"))

    assert stdout_payload == written_payload
    assert stdout_payload["apiVersion"] == "1"
    assert stdout_payload["ok"] is True
    assert stdout_payload["planOnly"] is True
    assert stdout_payload["mode"] == "synthetic"
    assert stdout_payload["requested"]["prebufferMs"] == 400
    assert "--mode wasapi" in stdout_payload["exampleCommand"]
    assert any("SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1" in item for item in stdout_payload["requirements"])
    assert any("normal WASAPI capture is enabled by default" in item for item in stdout_payload["requirements"])
    assert any("does not yet prove buffered WASAPI audio adoption" in item for item in stdout_payload["requirements"])


def test_rust_audio_prewarm_validation_accepts_consistent_stop_health() -> None:
    errors = validate_prewarm_metrics(
        {
            "start": {
                "prewarmId": "prewarm-1",
                "prebufferFrameTarget": 4,
            },
            "stop": {
                "stopped": True,
                "prewarmId": "prewarm-1",
                "prewarmError": None,
                "totalBlocksObserved": 8,
                "totalAudioFramesObserved": 1280,
                "bufferedBlocks": 4,
                "bufferedAudioFrames": 640,
            },
        },
        require_prebuffer=True,
    )

    assert errors == []


def test_rust_audio_prewarm_validation_rejects_missing_buffer_when_required() -> None:
    errors = validate_prewarm_metrics(
        {
            "start": {
                "prewarmId": "prewarm-1",
                "prebufferFrameTarget": 4,
            },
            "stop": {
                "stopped": True,
                "prewarmId": "prewarm-1",
                "prewarmError": None,
                "totalBlocksObserved": 8,
                "totalAudioFramesObserved": 1280,
                "bufferedBlocks": 0,
                "bufferedAudioFrames": 0,
            },
        },
        require_prebuffer=True,
    )

    assert "bufferedBlocks must be positive when prebufferMs is requested" in errors
    assert "bufferedAudioFrames must be positive when prebufferMs is requested" in errors

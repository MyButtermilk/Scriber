from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.smoke_rust_audio_app_prewarm import validate_smoke


REPO_ROOT = Path(__file__).resolve().parents[2]


def _manager_health_snapshot(*, health_restart_count: int = 0) -> dict[str, object]:
    return {
        "active": True,
        "prewarmIdHash": "abc",
        "lastHealthCheckActive": True,
        "lastHealthResponseMs": 3.0,
        "healthRestartCount": health_restart_count,
        "lastStatus": {
            "active": True,
            "prewarmIdHash": "abc",
        },
    }


def test_rust_audio_app_prewarm_smoke_script_documents_app_lifecycle_contract() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_rust_audio_app_prewarm.py").read_text(
        encoding="utf-8"
    )

    assert "RustAudioPrewarmManager" in script
    assert "RustPrototypeFrameSource" in script
    assert "audioPrewarmStart" in script
    assert "audioCaptureStart" in script
    assert "prewarmId" in script
    assert "managerResume" in script
    assert "adoptedPrewarm" in script
    assert "healthRestartCount" in script
    assert "framePipePrebufferFramesRead" in script
    assert "framePipePrebufferAfterLiveCount" in script
    assert "--mode wasapi" in script
    assert "--honor-favorite-mic" in script


def test_rust_audio_app_prewarm_smoke_plan_only_writes_artifact(tmp_path: Path) -> None:
    output = tmp_path / "rust-audio-app-prewarm-plan.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_rust_audio_app_prewarm.py",
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
    assert stdout_payload["requested"]["resumeAfterCapture"] is True
    assert stdout_payload["requested"]["honorFavoriteMic"] is False
    assert "RustAudioPrewarmManager" in " ".join(stdout_payload["requirements"])
    assert "--mode wasapi" in stdout_payload["exampleCommand"]


def test_rust_audio_app_prewarm_validation_accepts_app_adoption_metrics() -> None:
    errors = validate_smoke(
        {
            "ok": True,
            "managerStart": {"active": True},
            "managerPreAdoptionHealth": _manager_health_snapshot(),
            "managerAdoption": {"prewarmIdHash": "abc"},
            "managerResume": {"active": True},
            "managerPostResumeHealth": _manager_health_snapshot(),
            "sourceFinal": {
                "callbackCount": 12,
                "adoptedPrewarm": {"adopted": True, "blocks": 4},
                "framePipePrebufferFramesRead": 4,
                "framePipeLiveFramesRead": 8,
                "framePipePrebufferAfterLiveCount": 0,
                "framePipeSequenceErrorCount": 0,
                "framePipeProtocolErrorCount": 0,
            },
        }
    )

    assert errors == []


def test_rust_audio_app_prewarm_validation_rejects_health_restart() -> None:
    errors = validate_smoke(
        {
            "ok": True,
            "managerStart": {"active": True},
            "managerPreAdoptionHealth": _manager_health_snapshot(health_restart_count=1),
            "managerAdoption": {"prewarmIdHash": "abc"},
            "managerResume": {"active": True},
            "managerPostResumeHealth": _manager_health_snapshot(),
            "sourceFinal": {
                "callbackCount": 12,
                "adoptedPrewarm": {"adopted": True, "blocks": 4},
                "framePipePrebufferFramesRead": 4,
                "framePipeLiveFramesRead": 8,
                "framePipePrebufferAfterLiveCount": 0,
                "framePipeSequenceErrorCount": 0,
                "framePipeProtocolErrorCount": 0,
            },
        }
    )

    assert "managerPreAdoptionHealth must prove active audioPrewarmStatus" in errors


def test_rust_audio_app_prewarm_validation_rejects_missing_adoption() -> None:
    errors = validate_smoke(
        {
            "ok": True,
            "managerStart": {"active": True},
            "managerPreAdoptionHealth": _manager_health_snapshot(),
            "managerAdoption": {},
            "managerPostResumeHealth": _manager_health_snapshot(),
            "sourceFinal": {
                "callbackCount": 2,
                "adoptedPrewarm": {"adopted": False, "blocks": 0},
                "framePipePrebufferFramesRead": 0,
                "framePipeLiveFramesRead": 2,
                "framePipePrebufferAfterLiveCount": 0,
                "framePipeSequenceErrorCount": 0,
                "framePipeProtocolErrorCount": 0,
            },
        }
    )

    assert "managerAdoption.prewarmIdHash must be present" in errors
    assert "sourceFinal.adoptedPrewarm.adopted must be true" in errors
    assert "sourceFinal.framePipePrebufferFramesRead must be positive" in errors

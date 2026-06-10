from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_recording_hot_path_comparison import build_comparison


def recording_report(*, engine: str, provider: bool = True, rust_capture: bool = False) -> dict:
    requirements = {
        "hotkey_to_recording_state": {"status": "measured"},
        "hotkey_to_first_audio_frame": {"status": "measured"},
        "stop_to_text_injection": {"status": "measured"},
    }
    if provider:
        requirements["provider_transcript"] = {
            "status": "measured",
            "providerTranscriptSamples": 1,
        }
    if rust_capture:
        requirements["rust_audio_engine"] = {
            "status": "measured",
            "matchingSamples": 1,
        }
    sample = {
        "ok": True,
        "segments": {
            "hotkey_received_to_mic_ready_ms": 100.0 if engine == "python" else 80.0,
            "hotkey_received_to_first_audio_frame_ms": 180.0 if engine == "python" else 140.0,
            "hotkey_received_to_first_audible_audio_frame_ms": 210.0 if engine == "python" else 160.0,
            "hotkey_received_to_first_final_token_ms": 900.0 if engine == "python" else 820.0,
            "stop_requested_to_last_chunk_sent_ms": 24.0 if engine == "python" else 12.0,
            "stop_requested_to_provider_final_received_ms": 1300.0 if engine == "python" else 1260.0,
            "last_chunk_sent_to_provider_final_received_ms": 1276.0 if engine == "python" else 1248.0,
            "provider_final_received_to_clipboard_set_ms": 20.0,
            "clipboard_set_to_paste_ms": 12.0,
            "paste_to_first_paste_ms": 4.0,
            "stop_requested_to_first_paste_ms": 1336.0 if engine == "python" else 1296.0,
        },
        "audioDiagnosticsDuringRecording": {
            "microphone": {
                "activeCapture": {
                    "engine": "rust-prototype" if rust_capture else "python",
                    "frameSource": "rust-frame-pipe" if rust_capture else "sounddevice",
                    "callbackCount": 12,
                    "nativeEndpointIdHash": "hash" if rust_capture else None,
                }
            }
        },
    }
    return {
        "schemaVersion": 1,
        "ok": True,
        "audioDiagnostics": {
            "featureFlags": {
                "audioEngine": engine,
                "requestedAudioEngine": engine,
                "rustAudioRequested": engine == "rust-prototype",
                "rustAudioAvailable": engine == "rust-prototype",
            },
            "provider": {
                "configured": "azure_mai",
                "active": "azure_mai",
            },
        },
        "summary": {
            "complete": True,
            "requirements": requirements,
        },
        "samples": [sample],
    }


def test_recording_hot_path_comparison_accepts_provider_backed_rust_evidence() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(engine="rust-prototype", rust_capture=True),
    )

    assert result["ok"] is True
    assert result["reports"]["python"]["audioEngine"] == "python"
    assert result["reports"]["rust"]["audioEngine"] == "rust-prototype"
    assert result["summary"]["completeSegmentCount"] > 0
    first_audio = result["segments"]["hotkey_received_to_first_audio_frame_ms"]
    assert first_audio["rustMinusPythonP95Ms"] == -40.0
    assert first_audio["rustSpeedupP95Pct"] > 0


def test_recording_hot_path_comparison_rejects_missing_provider_transcript() -> None:
    result = build_comparison(
        recording_report(engine="python", provider=False),
        recording_report(engine="rust-prototype", rust_capture=True),
    )

    assert result["ok"] is False
    assert "Both Python and Rust reports must measure provider_transcript" in result["failures"]


def test_recording_hot_path_comparison_rejects_rust_fallback_to_python_capture() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(engine="rust-prototype", rust_capture=False),
    )

    assert result["ok"] is False
    assert "Rust report must prove active rust-prototype rust-frame-pipe capture" in result["failures"]


def test_recording_hot_path_comparison_cli_writes_artifact(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    python_report = tmp_path / "python.json"
    rust_report = tmp_path / "rust.json"
    output = tmp_path / "comparison.json"
    python_report.write_text(json.dumps(recording_report(engine="python")), encoding="utf-8")
    rust_report.write_text(
        json.dumps(recording_report(engine="rust-prototype", rust_capture=True)),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_recording_hot_path_comparison.py",
            "--python-report",
            str(python_report),
            "--rust-report",
            str(rust_report),
            "--output",
            str(output),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["providerTranscript"]["complete"] is True

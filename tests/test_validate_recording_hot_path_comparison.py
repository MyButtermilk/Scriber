from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from scripts.validate_recording_hot_path_comparison import build_comparison


def recording_report(
    *,
    engine: str,
    provider_label: str = "azure_mai",
    provider: bool = True,
    rust_capture: bool = False,
    fallback_circuit_open: bool = False,
    sample_count: int = 3,
    record_seconds: float = 2.0,
    mic_always_on: bool | None = None,
    rust_prewarm_adopted: bool = True,
    rust_prewarm_raw_id: bool = False,
    rust_mid_session_failure_reason: str = "",
    rust_frame_pipe_reader_end_reason: str = "running",
) -> dict:
    if mic_always_on is None:
        mic_always_on = bool(rust_capture)
    requirements = {
        "hotkey_to_recording_state": {"status": "measured"},
        "hotkey_to_first_audio_frame": {"status": "measured"},
        "stop_to_text_injection": {"status": "measured"},
    }
    if provider:
        requirements["provider_transcript"] = {
            "status": "measured",
            "providerTranscriptSamples": sample_count,
        }
    if rust_capture:
        requirements["rust_audio_engine"] = {
            "status": "measured",
            "matchingSamples": sample_count,
        }
    active_capture = {
        "engine": "rust-prototype" if rust_capture else "python",
        "frameSource": "rust-frame-pipe" if rust_capture else "sounddevice",
        "callbackCount": 12,
        "nativeEndpointIdHash": "hash" if rust_capture else None,
    }
    if rust_capture:
        rust_adoption = {
            "adopted": rust_prewarm_adopted,
            "prewarmIdHash": "prewarm-hash" if rust_prewarm_adopted else "",
            "signature": {
                "device_preference": "default",
                "sample_rate": 16000,
                "target_channels": 1,
                "block_size": 160,
            },
        }
        if rust_prewarm_raw_id:
            rust_adoption["prewarmId"] = "raw-prewarm-id"
        active_capture["rustPrewarmAdoption"] = rust_adoption
        active_capture["midSessionFailureReason"] = rust_mid_session_failure_reason
        active_capture["sourceMidSessionFailureReason"] = rust_mid_session_failure_reason
        active_capture["lastRustAudioMidSessionFailureReason"] = rust_mid_session_failure_reason
        active_capture["framePipeReaderEndReason"] = rust_frame_pipe_reader_end_reason
        active_capture["sourceFramePipeReaderEndReason"] = rust_frame_pipe_reader_end_reason

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
                "micAlwaysOn": mic_always_on,
                "idlePrewarmActive": mic_always_on,
                "prewarmEngine": "rust" if mic_always_on and rust_capture else "",
                "rustAudioFallbackCircuit": {
                    "available": True,
                    "open": fallback_circuit_open,
                    "reason": "pipeClosed" if fallback_circuit_open else "",
                    "remainingSeconds": 12.5 if fallback_circuit_open else None,
                },
                "activeCapture": active_capture,
            }
        },
    }
    samples = []
    for index in range(sample_count):
        item = copy.deepcopy(sample)
        item["iteration"] = index + 1
        samples.append(item)

    return {
        "schemaVersion": 1,
        "ok": True,
        "requested": {
            "iterations": sample_count,
            "recordSeconds": record_seconds,
            "speechPromptText": "Scriber provider-backed Rust audio validation",
            "speechPromptDelaySec": 0.5,
            "requireTextTarget": False,
            "requireProviderTranscript": True,
            "textTargetTitle": "Scriber Hot Path Text Target",
            "textTargetSettleSec": 1.0,
            "textTargetTimeoutSec": 5.0,
        },
        "audioDiagnostics": {
            "featureFlags": {
                "audioEngine": engine,
                "requestedAudioEngine": engine,
                "rustAudioRequested": engine == "rust-prototype",
                "rustAudioAvailable": engine == "rust-prototype",
            },
            "provider": {
                "configured": provider_label,
                "active": provider_label,
            },
            "microphone": {
                "micAlwaysOn": mic_always_on,
                "idlePrewarmActive": mic_always_on,
                "prewarmEngine": "rust" if mic_always_on and rust_capture else "",
                "rustAudioFallbackCircuit": {
                    "available": True,
                    "open": fallback_circuit_open,
                    "reason": "pipeClosed" if fallback_circuit_open else "",
                    "remainingSeconds": 12.5 if fallback_circuit_open else None,
                },
            },
        },
        "summary": {
            "complete": True,
            "requirements": requirements,
        },
        "samples": samples,
    }


def test_recording_hot_path_comparison_accepts_provider_backed_rust_evidence() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(engine="rust-prototype", rust_capture=True),
    )

    assert result["ok"] is True
    assert result["reports"]["python"]["audioEngine"] == "python"
    assert result["reports"]["rust"]["audioEngine"] == "rust-prototype"
    assert result["reports"]["rust"]["micAlwaysOn"] is True
    assert result["reports"]["rust"]["rustPrewarmAdopted"] is True
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


def test_recording_hot_path_comparison_rejects_mismatched_provider() -> None:
    result = build_comparison(
        recording_report(engine="python", provider_label="azure_mai"),
        recording_report(
            engine="rust-prototype",
            provider_label="deepgram",
            rust_capture=True,
        ),
    )

    assert result["ok"] is False
    assert "Python and Rust reports must use the same STT provider" in result["failures"]
    check = next(item for item in result["checks"] if item["name"] == "sameProvider")
    assert check["ok"] is False
    assert check["details"]["pythonProvider"] == "azure_mai"
    assert check["details"]["rustProvider"] == "deepgram"


def test_recording_hot_path_comparison_rejects_mismatched_recording_config() -> None:
    result = build_comparison(
        recording_report(engine="python", record_seconds=2.0),
        recording_report(engine="rust-prototype", rust_capture=True, record_seconds=3.0),
    )

    assert result["ok"] is False
    assert (
        "Python and Rust reports must use the same recording benchmark configuration"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "sameRecordingConfig")
    assert check["ok"] is False
    assert check["details"]["mismatchedFields"] == ["recordSeconds"]


def test_recording_hot_path_comparison_rejects_too_few_samples() -> None:
    result = build_comparison(
        recording_report(engine="python", sample_count=1),
        recording_report(engine="rust-prototype", rust_capture=True, sample_count=1),
        min_samples_per_report=3,
    )

    assert result["ok"] is False
    assert "Python and Rust reports must each include at least 3 sample(s)" in result["failures"]
    check = next(item for item in result["checks"] if item["name"] == "sampleCount")
    assert check["ok"] is False
    assert check["details"]["pythonSamples"] == 1
    assert check["details"]["rustSamples"] == 1
    assert check["details"]["minSamplesPerReport"] == 3


def test_recording_hot_path_comparison_rejects_rust_fallback_to_python_capture() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(engine="rust-prototype", rust_capture=False),
    )

    assert result["ok"] is False
    assert "Rust report must prove active rust-prototype rust-frame-pipe capture" in result["failures"]


def test_recording_hot_path_comparison_rejects_audio_owned_latency_regression() -> None:
    rust_report = recording_report(engine="rust-prototype", rust_capture=True)
    for sample in rust_report["samples"]:
        sample["segments"]["hotkey_received_to_first_audio_frame_ms"] = 260.0

    result = build_comparison(
        recording_report(engine="python"),
        rust_report,
        max_audio_owned_p95_regression_ms=50.0,
    )

    assert result["ok"] is False
    assert (
        "Rust audio-owned hot-path P95 latency must not regress by more than 50.0 ms on any gated segment"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "audioOwnedLatencyNoRegression")
    assert check["ok"] is False
    segment = check["details"]["gatedSegments"]["hotkey_received_to_first_audio_frame_ms"]
    assert segment["rustMinusPythonP95Ms"] == 80.0
    assert segment["ok"] is False


def test_recording_hot_path_comparison_rejects_open_rust_fallback_circuit() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(
            engine="rust-prototype",
            rust_capture=True,
            fallback_circuit_open=True,
        ),
    )

    assert result["ok"] is False
    assert "Rust report must not have an open Rust audio fallback circuit" in result["failures"]
    check = next(item for item in result["checks"] if item["name"] == "rustFallbackCircuitClosed")
    assert check["ok"] is False
    assert check["details"]["open"] is True


def test_recording_hot_path_comparison_rejects_rust_mid_session_failure() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(
            engine="rust-prototype",
            rust_capture=True,
            rust_mid_session_failure_reason="pipeClosed",
            rust_frame_pipe_reader_end_reason="pipeClosed",
        ),
    )

    assert result["ok"] is False
    assert "Rust report must not contain mid-session frame-pipe failures" in result["failures"]
    check = next(item for item in result["checks"] if item["name"] == "rustMidSessionClean")
    assert check["ok"] is False
    assert check["details"]["failureSampleCount"] == 3
    assert check["details"]["failures"][0]["midSessionFailureReason"] == "pipeClosed"


def test_recording_hot_path_comparison_rejects_rust_without_always_on_mic() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(
            engine="rust-prototype",
            rust_capture=True,
            mic_always_on=False,
        ),
    )

    assert result["ok"] is False
    assert (
        "Rust report must prove MIC always-on was enabled during provider-backed comparison"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "rustAlwaysOnMic")
    assert check["ok"] is False
    assert check["details"]["micAlwaysOnSnapshotCount"] == 0
    assert result["reports"]["rust"]["micAlwaysOn"] is False


def test_recording_hot_path_comparison_rejects_rust_without_prewarm_adoption() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(
            engine="rust-prototype",
            rust_capture=True,
            rust_prewarm_adopted=False,
        ),
    )

    assert result["ok"] is False
    assert (
        "Rust report must prove adopted Rust prewarm evidence during provider-backed comparison"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "rustPrewarmAdoption")
    assert check["ok"] is False
    assert check["details"]["framePipeSampleCount"] == 3
    assert check["details"]["adoptedSampleCount"] == 0
    assert check["details"]["missingOrWeakSampleCount"] == 3
    assert result["reports"]["rust"]["rustPrewarmAdopted"] is False


def test_recording_hot_path_comparison_rejects_raw_prewarm_id() -> None:
    result = build_comparison(
        recording_report(engine="python"),
        recording_report(
            engine="rust-prototype",
            rust_capture=True,
            rust_prewarm_raw_id=True,
        ),
    )

    assert result["ok"] is False
    assert (
        "Rust report must prove adopted Rust prewarm evidence during provider-backed comparison"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "rustPrewarmAdoption")
    assert check["ok"] is False
    assert check["details"]["rawPrewarmIdSampleCount"] == 3


def test_recording_hot_path_comparison_rejects_unredacted_input_reports() -> None:
    python_report = recording_report(engine="python")
    rust_report = recording_report(engine="rust-prototype", rust_capture=True)
    python_report["audioDiagnostics"]["sessionToken"] = "raw-python-token"
    active_capture = rust_report["samples"][0]["audioDiagnosticsDuringRecording"]["microphone"]["activeCapture"]
    active_capture["endpointId"] = r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-hot-path-device}"
    active_capture["framePipe"] = r"\\.\pipe\scriber-audio-hot-path-secret"

    result = build_comparison(python_report, rust_report)

    assert result["ok"] is False
    assert "Recording hot-path comparison input reports must be redacted" in result["failures"]
    assert (
        "Python recording hot-path report contains unredacted token-like value at "
        "audioDiagnostics.sessionToken"
        in result["failures"]
    )
    assert (
        "Rust recording hot-path report contains raw native endpoint ID at "
        "samples[0].audioDiagnosticsDuringRecording.microphone.activeCapture.endpointId"
        in result["failures"]
    )
    assert (
        "Rust recording hot-path report contains raw Scriber pipe name at "
        "samples[0].audioDiagnosticsDuringRecording.microphone.activeCapture.framePipe"
        in result["failures"]
    )
    check = next(item for item in result["checks"] if item["name"] == "inputReportRedaction")
    assert check["ok"] is False
    assert check["details"]["failureCount"] >= 3


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

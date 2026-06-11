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
    assert payload["audioDiagnostics"]["provider"]["configured"] == "validate"
    assert payload["audioDiagnostics"]["runtimeImports"]["onnxruntime"]["importable"] is True
    assert payload["audioDiagnostics"]["runtimeImports"]["pipecat.audio.vad.silero"]["importable"] is True
    assert payload["summary"]["requirements"]["hotkey_to_recording_state"]["status"] == "measured"
    assert payload["summary"]["requirements"]["hotkey_to_first_audio_frame"]["status"] == "measured"
    stop_requirement = payload["summary"]["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "measured"
    assert stop_requirement["durations"]["p95Ms"] == 0.0
    assert stop_requirement["alreadyInjectedBeforeStopSamples"] == 1


def test_recording_hot_path_baseline_validate_only_can_require_text_target(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "recording-hot-path-baseline-strict-target.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_recording_hot_path_baseline.py",
            "--validate-only",
            "--require-text-target",
            "--text-target-file",
            str(tmp_path / "target.txt"),
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
    target_requirement = payload["summary"]["requirements"]["text_target_persistence"]
    assert payload["ok"] is True
    assert payload["summary"]["complete"] is True
    assert target_requirement["status"] == "measured"
    assert target_requirement["capturedSamples"] == 1


def test_recording_hot_path_validate_only_can_require_provider_and_rust_audio(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "recording-hot-path-baseline-strict-rust.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_recording_hot_path_baseline.py",
            "--validate-only",
            "--require-provider-transcript",
            "--require-rust-audio-engine",
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
    provider_requirement = payload["summary"]["requirements"]["provider_transcript"]
    rust_requirement = payload["summary"]["requirements"]["rust_audio_engine"]

    assert payload["ok"] is True
    assert payload["summary"]["complete"] is True
    assert payload["audioDiagnostics"]["featureFlags"]["audioEngine"] == "rust-prototype"
    assert provider_requirement["status"] == "measured"
    assert provider_requirement["providerTranscriptSamples"] == 1
    assert rust_requirement["status"] == "measured"
    assert rust_requirement["matchingSamples"] == 1
    assert rust_requirement["activeCaptures"][0]["frameSource"] == "rust-frame-pipe"


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


def test_recording_hot_path_summary_reports_stop_to_injection_breakdown():
    summary = build_summary(
        [
            {
                "segments": {
                    "stop_requested_to_first_paste_ms": 1387.0,
                    "stop_requested_to_last_chunk_sent_ms": 16.0,
                    "stop_requested_to_provider_final_received_ms": 1320.0,
                    "last_chunk_sent_to_provider_final_received_ms": 1304.0,
                    "provider_final_received_to_clipboard_set_ms": 20.0,
                    "clipboard_set_to_paste_ms": 11.0,
                    "paste_to_first_paste_ms": 3.0,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "measured"
    assert stop_requirement["durations"]["p95Ms"] == 1387.0
    assert stop_requirement["lastChunkSentDurations"]["p95Ms"] == 16.0
    assert stop_requirement["stopToProviderFinalDurations"]["p95Ms"] == 1320.0
    assert stop_requirement["providerFinalizeDurations"]["p95Ms"] == 1304.0
    assert stop_requirement["providerToClipboardDurations"]["p95Ms"] == 20.0
    assert stop_requirement["clipboardToPasteDurations"]["p95Ms"] == 11.0
    assert stop_requirement["pasteCallbackDurations"]["p95Ms"] == 3.0


def test_recording_hot_path_summary_reports_text_target_capture():
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "textTarget": {
                    "capturedChars": 27,
                    "captureElapsedMs": 310.0,
                },
            }
        ]
    )

    assert summary["textTarget"]["configuredSamples"] == 1
    assert summary["textTarget"]["capturedSamples"] == 1
    assert summary["textTarget"]["maxCapturedChars"] == 27
    assert summary["textTarget"]["captureElapsedDurations"]["p95Ms"] == 310.0
    assert "text_target_persistence" not in summary["requirements"]


def test_recording_hot_path_summary_strict_text_target_requires_persisted_text():
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "textTarget": {
                    "capturedChars": 0,
                    "captureElapsedMs": None,
                },
            }
        ],
        require_text_target=True,
    )

    target_requirement = summary["requirements"]["text_target_persistence"]
    assert summary["complete"] is False
    assert target_requirement["status"] == "missing_target_text"
    assert target_requirement["configuredSamples"] == 1
    assert target_requirement["capturedSamples"] == 0


def test_recording_hot_path_summary_strict_text_target_passes_when_text_is_persisted():
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "textTarget": {
                    "capturedChars": 27,
                    "captureElapsedMs": 310.0,
                },
            }
        ],
        require_text_target=True,
    )

    target_requirement = summary["requirements"]["text_target_persistence"]
    assert summary["complete"] is True
    assert target_requirement["status"] == "measured"
    assert target_requirement["durations"]["p95Ms"] == 310.0


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


def test_recording_hot_path_summary_reports_missing_audible_audio():
    summary = build_summary(
        [
            {
                "segments": {
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_session_finished_ms": 90.0,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "missing_audible_audio"
    assert stop_requirement["audibleAudioSamples"] == 0
    assert stop_requirement["audibleAudioDurations"]["count"] == 0
    assert stop_requirement["providerTranscriptSamples"] == 0
    assert stop_requirement["providerTranscriptDurations"]["count"] == 0


def test_recording_hot_path_summary_reports_missing_provider_transcript_after_audible_audio():
    summary = build_summary(
        [
            {
                "segments": {
                    "hotkey_received_to_first_audible_audio_frame_ms": 210.0,
                    "stop_requested_to_session_finished_ms": 90.0,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "missing_provider_transcript"
    assert stop_requirement["audibleAudioSamples"] == 1
    assert stop_requirement["audibleAudioDurations"]["p95Ms"] == 210.0
    assert stop_requirement["providerTranscriptSamples"] == 0


def test_recording_hot_path_provider_requirement_rejects_audible_audio_without_provider_final():
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "hotkey_received_to_first_audible_audio_frame_ms": 210.0,
                    "stop_requested_to_session_finished_ms": 90.0,
                },
            }
        ],
        require_provider_transcript=True,
    )

    provider_requirement = summary["requirements"]["provider_transcript"]
    assert summary["complete"] is False
    assert provider_requirement["status"] == "missing_provider_transcript"
    assert provider_requirement["audibleAudioSamples"] == 1
    assert provider_requirement["providerTranscriptSamples"] == 0


def test_recording_hot_path_rust_audio_requirement_checks_active_capture_diagnostics():
    audio_diagnostics = {
        "featureFlags": {
            "requestedAudioEngine": "rust-prototype",
            "audioEngine": "rust-prototype",
            "rustAudioRequested": True,
            "rustAudioAvailable": True,
        }
    }
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "audioDiagnosticsDuringRecording": {
                    "microphone": {
                        "activeCapture": {
                            "engine": "rust-prototype",
                            "frameSource": "rust-frame-pipe",
                            "callbackCount": 9,
                            "nativeEndpointIdHash": "redacted",
                        }
                    }
                },
            }
        ],
        require_rust_audio_engine=True,
        audio_diagnostics=audio_diagnostics,
    )

    rust_requirement = summary["requirements"]["rust_audio_engine"]
    assert summary["complete"] is True
    assert rust_requirement["status"] == "measured"
    assert rust_requirement["matchingSamples"] == 1
    assert rust_requirement["activeCaptures"][0]["nativeEndpointIdHash"] == "redacted"


def test_recording_hot_path_rust_audio_requirement_rejects_python_capture_fallback():
    audio_diagnostics = {
        "featureFlags": {
            "requestedAudioEngine": "rust-prototype",
            "audioEngine": "rust-prototype",
            "rustAudioRequested": True,
            "rustAudioAvailable": True,
        }
    }
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "audioDiagnosticsDuringRecording": {
                    "microphone": {
                        "activeCapture": {
                            "engine": "python",
                            "frameSource": "sounddevice",
                        }
                    }
                },
            }
        ],
        require_rust_audio_engine=True,
        audio_diagnostics=audio_diagnostics,
    )

    rust_requirement = summary["requirements"]["rust_audio_engine"]
    assert summary["complete"] is False
    assert rust_requirement["status"] == "missing_active_rust_capture"
    assert rust_requirement["matchingSamples"] == 0


def test_recording_hot_path_rust_audio_requirement_rejects_open_fallback_circuit():
    audio_diagnostics = {
        "featureFlags": {
            "requestedAudioEngine": "rust-prototype",
            "audioEngine": "rust-prototype",
            "rustAudioRequested": True,
            "rustAudioAvailable": True,
        },
        "microphone": {
            "rustAudioFallbackCircuit": {
                "available": True,
                "open": True,
                "reason": "pipeClosed",
                "remainingSeconds": 12.5,
            }
        },
    }
    summary = build_summary(
        [
            {
                "ok": True,
                "segments": {
                    "hotkey_received_to_mic_ready_ms": 120.0,
                    "hotkey_received_to_first_audio_frame_ms": 180.0,
                    "stop_requested_to_first_paste_ms": 82.5,
                },
                "audioDiagnosticsDuringRecording": {
                    "microphone": {
                        "rustAudioFallbackCircuit": {
                            "available": True,
                            "open": True,
                            "reason": "pipeClosed",
                            "remainingSeconds": 12.5,
                        },
                        "activeCapture": {
                            "engine": "rust-prototype",
                            "frameSource": "rust-frame-pipe",
                            "callbackCount": 9,
                            "nativeEndpointIdHash": "redacted",
                        },
                    }
                },
            }
        ],
        require_rust_audio_engine=True,
        audio_diagnostics=audio_diagnostics,
    )

    rust_requirement = summary["requirements"]["rust_audio_engine"]
    assert summary["complete"] is False
    assert rust_requirement["status"] == "fallback_circuit_open"
    assert rust_requirement["matchingSamples"] == 1
    assert rust_requirement["fallbackCircuitOpen"] is True
    assert rust_requirement["fallbackCircuits"][0]["reason"] == "pipeClosed"


def test_recording_hot_path_summary_reports_missing_injection_after_transcript():
    summary = build_summary(
        [
            {
                "segments": {
                    "hotkey_received_to_first_final_token_ms": 240.0,
                    "stop_requested_to_session_finished_ms": 90.0,
                }
            }
        ]
    )

    stop_requirement = summary["requirements"]["stop_to_text_injection"]
    assert stop_requirement["status"] == "missing_injection_after_transcript"
    assert stop_requirement["providerTranscriptSamples"] == 1
    assert stop_requirement["providerTranscriptDurations"]["p95Ms"] == 240.0


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
    assert "/api/runtime/audio-diagnostics" in (
        repo_root / "scripts" / "measure_recording_hot_path_baseline.py"
    ).read_text(encoding="utf-8")
    assert "--text-target-file" in script
    assert "--text-target-timeout-sec" in script
    assert "--require-text-target" in script
    assert "--require-provider-transcript" in script
    assert "--require-rust-audio-engine" in script
    assert "RequireRecordingHotPathTextTarget requires RecordingHotPathTextTargetFile" in script
    assert "RequireRecordingHotPathProviderTranscript" in script
    assert "RequireRecordingHotPathRustAudio" in script
    assert "provider_transcript" in script
    assert "rust_audio_engine" in script
    assert "--speech-prompt-text" in script
    assert "Convert-ToProcessArgument" in script
    assert "[string]$LegacyDataDir" in script
    assert "SCRIBER_LEGACY_DATA_DIR" in script
    assert "legacyDataDir = $LegacyDataDir" in script


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

    status_function = script.split("function Get-RecordingHotPathRequirementStatus", 1)[1].split(
        "function Get-RecordingHotPathRequirementNotes", 1
    )[0]
    assert 'return "not_requested"' in status_function
    assert 'return "missing_samples"' in status_function
    assert "$segmentNames -contains" not in status_function
    assert "$hasHotPathSamples" not in status_function


def test_hybrid_baseline_keeps_recording_hot_path_ok_false_as_evidence():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    recording_function = script.split("function Invoke-RecordingHotPathBenchmark", 1)[1].split(
        "function Invoke-BaselineIteration", 1
    )[0]
    assert 'throw "Recording hot-path baseline benchmark wrote ok=false' not in recording_function
    assert "Recording hot-path benchmark wrote ok=false; see sample errors" in recording_function
    assert "return Add-ArtifactPath -Benchmark $benchmark -Path $benchmarkOutputPath" in recording_function


def test_hybrid_baseline_restores_legacy_data_environment():
    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "measure_hybrid_baseline.ps1").read_text(
        encoding="utf-8"
    )

    baseline_function = script.split("function Invoke-BaselineIteration", 1)[1].split(
        "$RepoRoot = (Resolve-Path $RepoRoot).Path", 1
    )[0]
    assert "$oldLegacyDataDir = $env:SCRIBER_LEGACY_DATA_DIR" in baseline_function
    assert "$env:SCRIBER_LEGACY_DATA_DIR = $LegacyDataDir" in baseline_function
    assert "$env:SCRIBER_LEGACY_DATA_DIR = $oldLegacyDataDir" in baseline_function

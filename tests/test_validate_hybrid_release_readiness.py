from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from scripts.validate_hybrid_release_readiness import validate_release_readiness
from scripts.validate_tauri_updater_metadata import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_hybrid_release_readiness.py"


SCENARIO_FIXTURES = {
    "usb-add": (
        {"expectAdded": "usb", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "USB Mic", "label": "USB Mic"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "usb-remove": (
        {"expectAdded": "", "expectRemoved": "usb", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "USB Mic", "label": "USB Mic"}], "defaultChanged": False},
        {},
    ),
    "dock-disconnect": (
        {"expectAdded": "", "expectRemoved": "dock", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "defaultChanged": False},
        {},
    ),
    "dock-connect": (
        {"expectAdded": "dock", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "Dock Mic", "label": "Dock Mic"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "bluetooth-add": (
        {"expectAdded": "bluetooth", "expectRemoved": "", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "removed": [], "defaultChanged": False},
        {},
    ),
    "bluetooth-remove": (
        {"expectAdded": "", "expectRemoved": "bluetooth", "expectDefaultChanged": False, "expectFavoriteFallback": False},
        {"added": [], "removed": [{"deviceId": "Bluetooth Headset", "label": "Bluetooth Headset"}], "defaultChanged": False},
        {},
    ),
    "default-mic-change": (
        {"expectAdded": "", "expectRemoved": "", "expectDefaultChanged": True, "expectFavoriteFallback": False},
        {"added": [], "removed": [], "defaultChanged": True},
        {},
    ),
    "favorite-fallback": (
        {"expectAdded": "", "expectRemoved": "favorite", "expectDefaultChanged": False, "expectFavoriteFallback": True},
        {"added": [], "removed": [{"deviceId": "Favorite Mic", "label": "Favorite Mic"}], "defaultChanged": False},
        {"micDevice": "Built-in Mic", "favoriteMic": "Favorite Mic", "favoriteMicAvailable": False},
    ),
}


def write_full_hardware_matrix(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for scenario, (expectations, change, settings_after) in SCENARIO_FIXTURES.items():
        payload = {
            "ok": True,
            "scenarios": [scenario],
            "planOnly": False,
            "assumeCompleted": True,
            "result": {
                "before": [],
                "after": [],
                "settingsBefore": {},
                "settingsAfter": settings_after,
                "change": change,
                "expectations": expectations,
                "failures": [],
            },
        }
        (directory / f"microphone-hardware-{scenario}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )


def write_signed_release_fixture(tmp_path: Path, *, signature: str = "signed-update") -> tuple[Path, Path, Path]:
    artifact_dir = tmp_path / "release-artifacts"
    artifact_dir.mkdir()
    artifact = artifact_dir / "Scriber_0.1.0_x64-setup.exe"
    artifact.write_bytes(b"signed Scriber setup")
    checksum = sha256(artifact.read_bytes()).hexdigest()
    metadata = tmp_path / "latest.json"
    metadata.write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "notes": "Release notes",
                "pub_date": "2026-06-02T12:00:00Z",
                "platforms": {
                    "windows-x86_64": {
                        "signature": signature,
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                    }
                },
                "artifacts": [
                    {
                        "name": artifact.name,
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                        "sha256": checksum,
                        "sizeBytes": artifact.stat().st_size,
                        "signature": signature,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{checksum}  {artifact.name}\n", encoding="utf-8")
    return metadata, artifact_dir, sums


def write_authenticode_report(path: Path) -> None:
    write_authenticode_report_for(path, artifact_path="Scriber_0.1.0_x64-setup.exe")


def write_authenticode_report_for(path: Path, *, artifact_path: str) -> None:
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "count": 1,
                "artifacts": [
                    {
                        "path": artifact_path,
                        "status": "Valid",
                        "signerSubject": "CN=Scriber Release Publisher",
                        "timestampSubject": "CN=Timestamp Authority",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_publication_report(path: Path, metadata: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "url": "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
                "finalUrl": "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
                "statusCode": 200,
                "requireSignatures": True,
                "metadataSha256": sha256_file(metadata),
            }
        ),
        encoding="utf-8",
    )


def write_media_preparation_report(path: Path, *, ok: bool = True, require_ffprobe: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "ok": ok,
                "durationMs": 700.0,
                "mediaTools": {
                    "ffmpeg": "C:/Program Files/FFmpeg/bin/ffmpeg.exe",
                    "ffprobe": "C:/Program Files/FFmpeg/bin/ffprobe.exe" if require_ffprobe else "",
                    "requireFfprobe": require_ffprobe,
                },
                "summary": {"totalChecks": 5, "passedChecks": 5 if ok else 4, "failedChecks": 0 if ok else 1},
                "checks": [
                    {
                        "name": "file_upload_compression",
                        "ok": True,
                        "output": {"suffix": ".webm", "sizeBytes": 1024},
                    },
                    {
                        "name": "video_upload_audio_extraction",
                        "ok": True,
                        "output": {"suffix": ".webm", "sizeBytes": 1024},
                    },
                    {
                        "name": "youtube_post_download_normalization",
                        "ok": True,
                        "output": {"suffix": ".webm", "sizeBytes": 1024},
                    },
                    {
                        "name": "azure_mai_audio_preparation",
                        "ok": True,
                        "prepared": {
                            "suffix": ".mp3",
                            "sizeBytes": 1024,
                            "contentType": "audio/mpeg",
                        },
                    },
                    {
                        "name": "ffprobe_duration_probe",
                        "ok": ok,
                        "ffprobeAvailable": require_ffprobe,
                        "durationSeconds": 4.0 if require_ffprobe else None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def write_runtime_dependency_footprint_report(path: Path, *, ok: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "ok": ok,
                "summary": {
                    "totalMb": 33.75,
                    "missingRequiredPaths": [],
                    "disallowedPaths": [],
                    "unexpectedPresentDependencies": [],
                    "budgetFailures": [] if ok else ["scipy"],
                },
                "budgets": {
                    "scipy": {"maxMb": None, "withinBudget": None},
                    "onnxruntime": {"maxMb": None, "withinBudget": None},
                    "total": {"maxMb": None, "withinBudget": None},
                },
                "dependencies": {
                    "scipy": {
                        "name": "scipy",
                        "expectedPresent": False,
                        "unexpectedPresent": False,
                        "totalMb": 0,
                        "missingRequiredPaths": [],
                        "disallowedPaths": [],
                        "paths": [{"path": "scipy", "exists": False}],
                    },
                    "onnxruntime": {
                        "name": "onnxruntime",
                        "expectedPresent": True,
                        "unexpectedPresent": False,
                        "totalMb": 33.75,
                        "missingRequiredPaths": [],
                        "disallowedPaths": [],
                        "paths": [{"path": "onnxruntime", "exists": True}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def write_rust_audio_sidecar_report(
    path: Path,
    *,
    ok: bool = True,
    duration_sec: float = 600,
    selected_hash_verified: bool = True,
    sequence_gap_count: int = 0,
    prebuffer_ms: int = 400,
    prebuffer_frames_read: int = 4,
    prebuffer_after_live_count: int = 0,
    observed_duration_sec: float | None = None,
    plan_only: bool = False,
    prewarm_before_capture: bool = False,
    adopted_prewarm_blocks: int = 4,
) -> None:
    failed_capture_count = 0 if ok else 1
    if observed_duration_sec is None:
        observed_duration_sec = duration_sec
    default_prebuffer_frames = prebuffer_frames_read if prebuffer_ms > 0 else 0
    selected_prebuffer_frames = (
        max(1, prebuffer_frames_read // 2)
        if prebuffer_ms > 0 and prebuffer_frames_read > 0
        else 0
    )
    path.write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "ok": ok,
                "planOnly": plan_only,
                "mode": "wasapi",
                "requested": {
                    "durationSec": duration_sec,
                    "selectedDurationSec": 10,
                    "prebufferMs": prebuffer_ms,
                    "prewarmBeforeCapture": prewarm_before_capture,
                },
                "summary": {
                    "captureCount": 2,
                    "failedCaptureCount": failed_capture_count,
                    "totalFramesRead": 24,
                    "totalPrebufferFramesRead": default_prebuffer_frames + selected_prebuffer_frames,
                    "totalLiveFramesRead": 18,
                    "totalPrebufferAfterLiveCount": prebuffer_after_live_count,
                    "totalFramesWritten": 24,
                    "totalPrebufferFramesWritten": default_prebuffer_frames + selected_prebuffer_frames,
                    "totalLiveFramesWritten": 18,
                    "totalAdoptedPrewarmBlocks": adopted_prewarm_blocks if prewarm_before_capture else 0,
                    "selectedHashVerified": selected_hash_verified,
                },
                "captures": [
                    {
                        "name": "default",
                        "ok": True,
                        "start": {
                            "nativeEndpointIdHash": "abc123",
                            "endpointSelection": {"mode": "default"},
                            "adoptedPrewarm": {
                                "adopted": prewarm_before_capture and adopted_prewarm_blocks > 0,
                                "blocks": adopted_prewarm_blocks if prewarm_before_capture else 0,
                                "audioFrames": (
                                    adopted_prewarm_blocks * 160 if prewarm_before_capture else 0
                                ),
                                "stop": {
                                    "reason": (
                                        "adoptedIntoCapture"
                                        if prewarm_before_capture and adopted_prewarm_blocks > 0
                                        else "notAdopted"
                                    )
                                },
                            },
                        },
                        "frames": {
                            "framesRead": 17,
                            "prebufferFramesRead": default_prebuffer_frames,
                            "liveFramesRead": 13,
                            "prebufferAfterLiveCount": prebuffer_after_live_count,
                            "firstFrameReadMs": 10.0,
                            "firstTimestampMicros": 1000,
                            "firstLiveFrameReadMs": 42.0,
                            "firstLiveSequence": default_prebuffer_frames,
                            "lastTimestampMicros": int(float(observed_duration_sec) * 1_000_000),
                            "observedDurationSec": observed_duration_sec,
                            "sequenceGapCount": sequence_gap_count,
                        },
                        "stop": {
                            "stopped": True,
                            "connected": True,
                            "framesWritten": 17,
                            "prebufferFramesWritten": default_prebuffer_frames,
                            "liveFramesWritten": 13,
                            "writerError": None,
                        },
                    },
                    {
                        "name": "selected-native-endpoint-hash",
                        "ok": True,
                        "start": {
                            "nativeEndpointIdHash": "abc123",
                            "endpointSelection": {"mode": "nativeEndpointHash"},
                        },
                        "frames": {
                            "framesRead": 7,
                            "prebufferFramesRead": selected_prebuffer_frames,
                            "liveFramesRead": 5,
                            "prebufferAfterLiveCount": 0,
                            "firstFrameReadMs": 9.0,
                            "firstTimestampMicros": 1000,
                            "firstLiveFrameReadMs": 30.0,
                            "firstLiveSequence": selected_prebuffer_frames,
                            "lastTimestampMicros": 10_000_000,
                            "observedDurationSec": 10.0,
                            "sequenceGapCount": 0,
                        },
                        "stop": {
                            "stopped": True,
                            "connected": True,
                            "framesWritten": 7,
                            "prebufferFramesWritten": selected_prebuffer_frames,
                            "liveFramesWritten": 5,
                            "writerError": None,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def write_rust_audio_prewarm_sidecar_report(
    path: Path,
    *,
    ok: bool = True,
    mode: str = "synthetic",
    plan_only: bool = False,
    prebuffer_ms: int = 400,
    prewarm_id: str = "prewarm-1",
    stopped: bool = True,
    prewarm_error: str | None = None,
    total_blocks_observed: int = 18,
    total_audio_frames_observed: int = 2880,
    buffered_blocks: int = 4,
    buffered_audio_frames: int = 640,
    prebuffer_frame_target: int = 40,
) -> None:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "ok": ok,
                "planOnly": plan_only,
                "mode": mode,
                "sidecar": {
                    "exe": "C:/Scriber/scriber-audio-sidecar.exe",
                    "exists": True,
                    "sha256": "a" * 64,
                },
                "requested": {
                    "durationSec": 1.0,
                    "sampleRate": 16000,
                    "channels": 1,
                    "blockSize": 160,
                    "prebufferMs": prebuffer_ms,
                },
                "prewarm": {
                    "ok": ok,
                    "prewarmStartResponseMs": 12.0,
                    "prewarmStopResponseMs": 4.0,
                    "start": {
                        "prewarmId": prewarm_id,
                        "prebufferFrameTarget": prebuffer_frame_target,
                        "syntheticPrewarm": mode == "synthetic",
                        "wasapiPrewarm": mode == "wasapi",
                        "nativeEndpointIdHash": "abc123" if mode == "wasapi" else None,
                        "endpointSelection": {
                            "mode": "default" if mode == "wasapi" else "synthetic",
                            "selectedNativeEndpointIdHash": "abc123" if mode == "wasapi" else None,
                        },
                    },
                    "stop": {
                        "prewarmId": prewarm_id,
                        "stopped": stopped,
                        "prewarmError": prewarm_error,
                        "totalBlocksObserved": total_blocks_observed,
                        "totalAudioFramesObserved": total_audio_frames_observed,
                        "bufferedBlocks": buffered_blocks,
                        "bufferedAudioFrames": buffered_audio_frames,
                    },
                },
                "summary": {
                    "prewarmOk": ok,
                    "totalBlocksObserved": total_blocks_observed,
                    "bufferedAudioFrames": buffered_audio_frames,
                },
            }
        ),
        encoding="utf-8",
    )


def write_rust_audio_app_prewarm_report(
    path: Path,
    *,
    ok: bool = True,
    mode: str = "wasapi",
    plan_only: bool = False,
    honor_favorite_mic: bool = False,
    duration_sec: float = 1.0,
    prewarm_duration_sec: float = 1.0,
    adopted_blocks: int = 40,
    prebuffer_frames: int = 40,
    live_frames: int = 42,
    callback_count: int = 82,
    prebuffer_after_live: int = 0,
    sequence_errors: int = 0,
    protocol_errors: int = 0,
    native_endpoint_hash: str = "abc123",
    manager_resume_active: bool = True,
    last_error: str = "",
) -> None:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "ok": ok,
                "planOnly": plan_only,
                "mode": mode,
                "requested": {
                    "durationSec": duration_sec,
                    "prewarmDurationSec": prewarm_duration_sec,
                    "sampleRate": 16000,
                    "channels": 1,
                    "blockSize": 160,
                    "prebufferMs": 400,
                    "honorFavoriteMic": honor_favorite_mic,
                },
                "managerStart": {
                    "active": True,
                    "prewarmIdHash": "prewarm-start-hash",
                },
                "managerAdoption": {
                    "prewarmIdHash": "prewarm-start-hash",
                    "signature": {
                        "device_preference": "default",
                        "sample_rate": 16000,
                        "target_channels": 1,
                        "block_size": 160,
                    },
                },
                "managerResume": {
                    "active": manager_resume_active,
                    "prewarmIdHash": "prewarm-resume-hash",
                },
                "sourceFinal": {
                    "callbackCount": callback_count,
                    "nativeEndpointIdHash": native_endpoint_hash,
                    "lastError": last_error,
                    "adoptedPrewarm": {
                        "adopted": adopted_blocks > 0,
                        "blocks": adopted_blocks,
                        "audioFrames": adopted_blocks * 160,
                    },
                    "framePipePrebufferFramesRead": prebuffer_frames,
                    "framePipeLiveFramesRead": live_frames,
                    "framePipePrebufferAfterLiveCount": prebuffer_after_live,
                    "framePipeSequenceErrorCount": sequence_errors,
                    "framePipeProtocolErrorCount": protocol_errors,
                },
                "summary": {
                    "callbackCount": callback_count,
                    "adoptedPrewarmBlocks": adopted_blocks,
                    "prebufferFramesRead": prebuffer_frames,
                    "liveFramesRead": live_frames,
                    "prebufferAfterLiveCount": prebuffer_after_live,
                    "sequenceErrorCount": sequence_errors,
                    "protocolErrorCount": protocol_errors,
                },
            }
        ),
        encoding="utf-8",
    )


def write_recording_hot_path_comparison_report(
    path: Path,
    *,
    ok: bool = True,
    rust_ok: bool = True,
    fallback_circuit_ok: bool = True,
    same_provider_ok: bool = True,
    latency_ok: bool = True,
    samples: int = 3,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "ok": ok,
                "failures": [] if ok else ["comparison failed"],
                "reports": {
                    "python": {"provider": "azure_mai", "audioEngine": "python", "samples": samples},
                    "rust": {"provider": "azure_mai", "audioEngine": "rust-prototype", "samples": samples},
                },
                "checks": [
                    {"name": "pythonReportOk", "ok": True, "details": {}},
                    {"name": "rustReportOk", "ok": True, "details": {}},
                    {"name": "physicalReports", "ok": True, "details": {}},
                    {
                        "name": "sampleCount",
                        "ok": samples >= 3,
                        "details": {
                            "pythonSamples": samples,
                            "rustSamples": samples,
                            "minSamplesPerReport": 3,
                        },
                    },
                    {"name": "providerTranscript", "ok": True, "details": {}},
                    {
                        "name": "sameProvider",
                        "ok": same_provider_ok,
                        "details": {
                            "pythonProvider": "azure_mai",
                            "rustProvider": "azure_mai" if same_provider_ok else "deepgram",
                        },
                    },
                    {"name": "rustAudioEngine", "ok": rust_ok, "details": {}},
                    {
                        "name": "rustFallbackCircuitClosed",
                        "ok": fallback_circuit_ok,
                        "details": {"open": not fallback_circuit_ok},
                    },
                    {
                        "name": "audioOwnedLatencyNoRegression",
                        "ok": latency_ok,
                        "details": {
                            "maxAllowedRustP95RegressionMs": 50.0,
                            "gatedSegments": {},
                        },
                    },
                    {"name": "pythonAudioEngine", "ok": True, "details": {}},
                ],
                "summary": {
                    "completeSegmentCount": 3,
                    "comparedSegmentCount": 11,
                    "providerTranscript": {"complete": True},
                    "hotkeyToFirstAudioFrame": {"complete": True},
                    "stopToTextInjection": {"complete": True},
                },
            }
        ),
        encoding="utf-8",
    )


def write_installed_live_recording_smoke_report(
    path: Path,
    *,
    ok: bool = True,
    cleanup_verified: bool = True,
    live_verified: bool = True,
    duration_sec: float = 600,
    stability_duration_sec: float | None = None,
    non_recording_sample_count: int = 0,
    sample_count: int = 3,
    stopped_listening: bool = False,
    started_recording_state: str = "recording",
    stopped_recording_state: str = "idle",
) -> None:
    if stability_duration_sec is None:
        stability_duration_sec = duration_sec
    samples = []
    for index in range(max(0, sample_count)):
        if non_recording_sample_count > 0 and index == 0:
            samples.append(
                {
                    "index": index + 1,
                    "recordingState": "idle",
                    "listening": False,
                    "healthReady": True,
                }
            )
        else:
            samples.append(
                {
                    "index": index + 1,
                    "recordingState": "recording",
                    "listening": True,
                    "healthReady": True,
                }
            )
    path.write_text(
        json.dumps(
            {
                "ok": ok,
                "runtimeMode": "tauri-supervised",
                "launchKind": "managed",
                "cleanupVerified": cleanup_verified,
                "liveRecording": {
                    "verified": live_verified,
                    "durationSec": duration_sec,
                    "probeIntervalSec": 5,
                    "startResponseOk": True,
                    "startedRecordingState": started_recording_state,
                    "startedListening": True,
                    "stopResponseOk": True,
                    "stoppedRecordingState": stopped_recording_state,
                    "stoppedListening": stopped_listening,
                    "nonRecordingSampleCount": non_recording_sample_count,
                    "textInjectionDisabled": True,
                    "stability": {
                        "verified": True,
                        "durationSec": stability_duration_sec,
                        "probeIntervalSec": 5,
                        "sampleCount": sample_count,
                        "backendWorkingSetPeakGrowthMb": 1.0,
                        "combinedCpuAvgPercent": 1.0,
                        "samples": samples,
                    },
                },
                "stability": {
                    "verified": True,
                    "durationSec": 1,
                    "sampleCount": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def write_tauri_text_injection_smoke_report(
    path: Path,
    *,
    ok: bool = True,
    method: str = "tauri",
    validate_only: bool = False,
    shell_ipc_available: bool = True,
    markers: list[str] | None = None,
    target_text_verified: bool = True,
) -> None:
    if markers is None:
        markers = ["clipboard_set", "paste"]
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAtUtc": "2026-06-11T12:00:00Z",
                "method": method,
                "status": "passed" if ok else "failed",
                "ok": ok,
                "callbackVerified": ok,
                "targetTextVerified": target_text_verified,
                "callbackElapsedMs": 12.5,
                "targetTextElapsedMs": 35.0,
                "capturedChars": 42 if target_text_verified else 0,
                "targetError": "",
                "expectedChars": 42,
                "targetFile": "C:/tmp/scriber-target.txt",
                "targetTitle": "Scriber Injection Smoke Target",
                "targetFocus": {
                    "attempted": True,
                    "ok": True,
                    "coordinateMode": "window-relative",
                },
                "shellIpc": {
                    "available": shell_ipc_available,
                    "pipeConfigured": True,
                    "tokenConfigured": True,
                    "apiVersion": "1",
                    "pipeNameHash": "pipe-hash",
                    "lastCommand": "injectText",
                    "lastSuccess": ok,
                    "lastError": None,
                    "lastErrorCode": None,
                    "lastFallbackReason": None,
                    "lastCommandAgoSeconds": 0.1,
                    "lastResponse": {
                        "success": ok,
                        "errorCode": None,
                        "fallbackReason": None,
                        "timingsMs": {"total": 12.0},
                        "payload": {
                            "method": method,
                            "dispatch": "ctrlV",
                            "markers": markers,
                            "restoreScheduled": True,
                            "restore": {
                                "scheduled": True,
                                "attempted": False,
                                "succeeded": None,
                                "skippedReason": "scheduled",
                                "errorCode": None,
                            },
                            "foregroundBefore": {
                                "available": True,
                                "windowHash": "win-hash",
                                "titleHash": "title-hash",
                                "processIdHash": "pid-hash",
                            },
                            "foregroundAfter": {
                                "available": True,
                                "windowHash": "win-hash",
                                "titleHash": "title-hash",
                                "processIdHash": "pid-hash",
                            },
                            "foregroundChanged": False,
                            "timingsMs": {
                                "clipboardRead": 1.0,
                                "clipboardSet": 2.0,
                                "preDelay": 80.0,
                                "pasteDispatch": 3.0,
                                "total": 10.0,
                            },
                        },
                    },
                },
                "validateOnly": validate_only,
            }
        ),
        encoding="utf-8",
    )


def write_complete_evidence(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path]:
    hardware_dir = tmp_path / "hardware"
    write_full_hardware_matrix(hardware_dir)
    metadata, artifact_dir, sums = write_signed_release_fixture(tmp_path)
    media_preparation_report = tmp_path / "media-preparation-smoke.json"
    runtime_dependency_footprint_report = tmp_path / "runtime-dependency-footprint.json"
    authenticode_report = tmp_path / "authenticode.json"
    publication_report = tmp_path / "updater-publication.json"
    write_media_preparation_report(media_preparation_report)
    write_runtime_dependency_footprint_report(runtime_dependency_footprint_report)
    write_authenticode_report(authenticode_report)
    write_publication_report(publication_report, metadata)
    return hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report


def test_validate_release_readiness_accepts_complete_evidence(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        expected_authenticode_publisher="Scriber Release Publisher",
        require_authenticode_timestamp=True,
    )

    assert result["ok"] is True
    assert {check["name"] for check in result["checks"]} == {
        "physicalMicrophoneMatrix",
        "signedTauriUpdaterMetadata",
        "mediaPreparationSmoke",
        "runtimeDependencyFootprint",
        "publishedUpdaterManifest",
        "authenticodeSignatures",
    }


def test_validate_release_readiness_accepts_required_rust_audio_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(rust_audio_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is True
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert rust_audio_check["details"]["summary"]["selectedHashVerified"] is True


def test_validate_release_readiness_accepts_required_recording_hot_path_comparison(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is True
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert comparison_check["details"]["summary"]["completeSegmentCount"] == 3


def test_validate_release_readiness_accepts_required_installed_live_recording_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(live_recording_report, duration_sec=600)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        installed_live_recording_smoke_report=live_recording_report,
        require_installed_live_recording_smoke=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is True
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert live_check["details"]["liveRecording"]["durationSec"] == 600
    assert live_check["details"]["stability"]["sampleCount"] == 3


def test_validate_release_readiness_rejects_missing_required_installed_live_recording_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_installed_live_recording_smoke=True,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert "Installed live recording smoke report is required" in live_check["failures"]


def test_validate_release_readiness_rejects_weak_installed_live_recording_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=120,
        non_recording_sample_count=1,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        installed_live_recording_smoke_report=live_recording_report,
        require_installed_live_recording_smoke=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert (
        "installed live recording smoke liveRecording.durationSec must be at least 600"
        in live_check["failures"]
    )
    assert "installed live recording smoke nonRecordingSampleCount must be 0" in live_check["failures"]
    assert any("sample 1 must remain in recording or listening state" in failure for failure in live_check["failures"])


def test_validate_release_readiness_accepts_required_tauri_text_injection_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_smoke_report=injection_report,
        require_tauri_text_injection_smoke=True,
    )

    assert result["ok"] is True
    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert injection_check["details"]["method"] == "tauri"
    assert injection_check["details"]["shellIpc"]["lastCommand"] == "injectText"


def test_validate_release_readiness_rejects_missing_required_tauri_text_injection_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_tauri_text_injection_smoke=True,
    )

    assert result["ok"] is False
    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert "Tauri text injection smoke report is required" in injection_check["failures"]


def test_validate_release_readiness_rejects_weak_tauri_text_injection_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(
        injection_report,
        validate_only=True,
        shell_ipc_available=False,
        markers=["clipboard_set"],
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_smoke_report=injection_report,
        require_tauri_text_injection_smoke=True,
    )

    assert result["ok"] is False
    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert "Tauri text injection smoke report must not be validate-only evidence" in injection_check["failures"]
    assert "Tauri text injection smoke shellIpc.available must be true" in injection_check["failures"]
    assert (
        "Tauri text injection smoke response markers must include clipboard_set and paste"
        in injection_check["failures"]
    )


def test_validate_release_readiness_rejects_missing_required_recording_hot_path_comparison(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "Recording hot-path Python/Rust comparison report is required" in comparison_check["failures"]


def test_validate_release_readiness_rejects_weak_recording_hot_path_comparison(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, rust_ok=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "recording hot-path comparison check failed: rustAudioEngine" in comparison_check["failures"]


def test_validate_release_readiness_rejects_too_few_recording_hot_path_samples(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, samples=1)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "recording hot-path comparison check failed: sampleCount" in comparison_check["failures"]
    assert "recording hot-path comparison Python report must include at least 3 samples" in comparison_check["failures"]
    assert "recording hot-path comparison Rust report must include at least 3 samples" in comparison_check["failures"]


def test_validate_release_readiness_rejects_open_rust_fallback_circuit_comparison(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, fallback_circuit_ok=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "recording hot-path comparison check failed: rustFallbackCircuitClosed" in comparison_check["failures"]


def test_validate_release_readiness_rejects_audio_owned_latency_regression_comparison(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, latency_ok=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "recording hot-path comparison check failed: audioOwnedLatencyNoRegression" in comparison_check["failures"]


def test_validate_release_readiness_rejects_mismatched_recording_hot_path_provider(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, same_provider_ok=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        recording_hot_path_comparison_report=comparison_report,
        require_recording_hot_path_comparison=True,
    )

    assert result["ok"] is False
    comparison_check = next(
        check for check in result["checks"] if check["name"] == "recordingHotPathPythonRustComparison"
    )
    assert "recording hot-path comparison check failed: sameProvider" in comparison_check["failures"]


def test_validate_release_readiness_accepts_required_rust_audio_sidecar_prewarm_adoption(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        prewarm_before_capture=True,
        adopted_prewarm_blocks=4,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is True
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert rust_audio_check["details"]["summary"]["totalAdoptedPrewarmBlocks"] == 4


def test_validate_release_readiness_rejects_missing_rust_audio_sidecar_prewarm_adoption(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        prewarm_before_capture=True,
        adopted_prewarm_blocks=0,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    failures = "\n".join(rust_audio_check["failures"])
    assert "totalAdoptedPrewarmBlocks must be positive" in failures
    assert "adoptedPrewarm.adopted must be true" in failures


def test_validate_release_readiness_accepts_required_rust_audio_prewarm_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    prewarm_report = tmp_path / "rust-audio-prewarm-sidecar-smoke.json"
    write_rust_audio_prewarm_sidecar_report(prewarm_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_prewarm_sidecar_report=prewarm_report,
        require_rust_audio_prewarm_sidecar_smoke=True,
    )

    assert result["ok"] is True
    prewarm_check = next(check for check in result["checks"] if check["name"] == "rustAudioPrewarmSidecarSmoke")
    assert prewarm_check["details"]["summary"]["prewarmOk"] is True


def test_validate_release_readiness_accepts_required_wasapi_rust_audio_prewarm_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    prewarm_report = tmp_path / "rust-audio-prewarm-sidecar-smoke.json"
    write_rust_audio_prewarm_sidecar_report(prewarm_report, mode="wasapi")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_prewarm_sidecar_report=prewarm_report,
        require_rust_audio_prewarm_sidecar_smoke=True,
    )

    assert result["ok"] is True
    prewarm_check = next(check for check in result["checks"] if check["name"] == "rustAudioPrewarmSidecarSmoke")
    assert prewarm_check["details"]["mode"] == "wasapi"


def test_validate_release_readiness_rejects_missing_required_rust_audio_prewarm_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_rust_audio_prewarm_sidecar_smoke=True,
    )

    assert result["ok"] is False
    prewarm_check = next(check for check in result["checks"] if check["name"] == "rustAudioPrewarmSidecarSmoke")
    assert "Rust audio prewarm sidecar smoke report is required" in prewarm_check["failures"]


def test_validate_release_readiness_rejects_weak_rust_audio_prewarm_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    prewarm_report = tmp_path / "rust-audio-prewarm-sidecar-smoke.json"
    write_rust_audio_prewarm_sidecar_report(
        prewarm_report,
        buffered_blocks=0,
        buffered_audio_frames=0,
        total_blocks_observed=0,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_prewarm_sidecar_report=prewarm_report,
        require_rust_audio_prewarm_sidecar_smoke=True,
    )

    assert result["ok"] is False
    prewarm_check = next(check for check in result["checks"] if check["name"] == "rustAudioPrewarmSidecarSmoke")
    failures = "\n".join(prewarm_check["failures"])
    assert "totalBlocksObserved must be positive" in failures
    assert "bufferedBlocks must be positive" in failures
    assert "bufferedAudioFrames must be positive" in failures


def test_validate_release_readiness_accepts_required_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    app_prewarm_report = tmp_path / "rust-audio-app-prewarm-smoke.json"
    write_rust_audio_app_prewarm_report(app_prewarm_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_app_prewarm_report=app_prewarm_report,
        require_rust_audio_app_prewarm_smoke=True,
    )

    assert result["ok"] is True
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    assert app_check["details"]["summary"]["adoptedPrewarmBlocks"] == 40


def test_validate_release_readiness_accepts_required_long_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    app_prewarm_report = tmp_path / "rust-audio-app-prewarm-smoke.json"
    write_rust_audio_app_prewarm_report(
        app_prewarm_report,
        duration_sec=600,
        prewarm_duration_sec=1800,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_app_prewarm_report=app_prewarm_report,
        require_rust_audio_app_prewarm_smoke=True,
        min_rust_audio_app_prewarm_duration_sec=600,
        min_rust_audio_app_prewarm_prewarm_duration_sec=1800,
    )

    assert result["ok"] is True
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    assert app_check["details"]["minDurationSec"] == 600
    assert app_check["details"]["minPrewarmDurationSec"] == 1800


def test_validate_release_readiness_rejects_missing_required_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_rust_audio_app_prewarm_smoke=True,
    )

    assert result["ok"] is False
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    assert "Rust audio app prewarm smoke report is required" in app_check["failures"]


def test_validate_release_readiness_rejects_short_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    app_prewarm_report = tmp_path / "rust-audio-app-prewarm-smoke.json"
    write_rust_audio_app_prewarm_report(
        app_prewarm_report,
        duration_sec=60,
        prewarm_duration_sec=120,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_app_prewarm_report=app_prewarm_report,
        require_rust_audio_app_prewarm_smoke=True,
        min_rust_audio_app_prewarm_duration_sec=600,
        min_rust_audio_app_prewarm_prewarm_duration_sec=1800,
    )

    assert result["ok"] is False
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    failures = "\n".join(app_check["failures"])
    assert "durationSec must be at least 600" in failures
    assert "prewarmDurationSec must be at least 1800" in failures


def test_validate_release_readiness_rejects_weak_rust_audio_app_prewarm_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    app_prewarm_report = tmp_path / "rust-audio-app-prewarm-smoke.json"
    write_rust_audio_app_prewarm_report(
        app_prewarm_report,
        adopted_blocks=0,
        prebuffer_frames=0,
        live_frames=0,
        honor_favorite_mic=True,
        native_endpoint_hash="",
        manager_resume_active=False,
        sequence_errors=1,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_app_prewarm_report=app_prewarm_report,
        require_rust_audio_app_prewarm_smoke=True,
    )

    assert result["ok"] is False
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    failures = "\n".join(app_check["failures"])
    assert "must use the stable default endpoint path" in failures
    assert "adoptedPrewarm.blocks must be positive" in failures
    assert "nativeEndpointIdHash is required" in failures
    assert "framePipePrebufferFramesRead must be positive" in failures
    assert "framePipeLiveFramesRead must be positive" in failures
    assert "framePipeSequenceErrorCount must be 0" in failures


def test_validate_release_readiness_rejects_missing_rust_audio_prebuffer_when_requested(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        prebuffer_ms=400,
        prebuffer_frames_read=0,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    failures = "\n".join(rust_audio_check["failures"])
    assert "totalPrebufferFramesRead must be positive" in failures
    assert "default prebufferFramesRead must be positive" in failures
    assert "default stop.prebufferFramesWritten must be positive" in failures
    assert "selected-native-endpoint-hash prebufferFramesRead must be positive" in failures
    assert (
        "selected-native-endpoint-hash stop.prebufferFramesWritten must be positive" in failures
    )


def test_validate_release_readiness_rejects_inconsistent_rust_audio_writer_counts(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(rust_audio_report)
    payload = json.loads(rust_audio_report.read_text(encoding="utf-8"))
    payload["summary"]["totalPrebufferFramesWritten"] = 1
    payload["captures"][0]["stop"]["prebufferFramesWritten"] = 1
    rust_audio_report.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    failures = "\n".join(rust_audio_check["failures"])
    assert "totalPrebufferFramesWritten must be at least totalPrebufferFramesRead" in failures
    assert "default stop.prebufferFramesWritten must be at least prebufferFramesRead" in failures


def test_validate_release_readiness_rejects_short_rust_audio_observed_duration(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        duration_sec=600,
        observed_duration_sec=3.0,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    failures = "\n".join(rust_audio_check["failures"])
    assert "default observedDurationSec must be at least 600" in failures


def test_validate_release_readiness_rejects_missing_required_rust_audio_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert "Rust audio sidecar smoke report is required" in rust_audio_check["failures"]


def test_validate_release_readiness_rejects_weak_rust_audio_sidecar_smoke(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        duration_sec=1,
        selected_hash_verified=False,
        sequence_gap_count=1,
    )

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        rust_audio_sidecar_report=rust_audio_report,
        require_rust_audio_sidecar_smoke=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    failures = "\n".join(rust_audio_check["failures"])
    assert "durationSec must be at least 600" in failures
    assert "selected native endpoint hash capture" in failures
    assert "sequenceGapCount must be 0" in failures


def test_validate_release_readiness_rejects_missing_external_reports(tmp_path: Path) -> None:
    hardware_dir = tmp_path / "hardware"
    write_full_hardware_matrix(hardware_dir)
    metadata, artifact_dir, sums = write_signed_release_fixture(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
    )

    assert result["ok"] is False
    failures_by_name = {check["name"]: check["failures"] for check in result["checks"]}
    assert "media preparation smoke report is required" in failures_by_name["mediaPreparationSmoke"]
    assert "runtime dependency footprint report is required" in failures_by_name["runtimeDependencyFootprint"]
    assert "published updater evidence report is required" in failures_by_name["publishedUpdaterManifest"]
    assert "Authenticode validation report is required" in failures_by_name["authenticodeSignatures"]


def test_validate_release_readiness_rejects_unsigned_updater_metadata(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["signature"] = ""
    data["artifacts"][0]["signature"] = ""
    metadata.write_text(json.dumps(data), encoding="utf-8")
    write_publication_report(publication_report, metadata)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    updater_check = next(check for check in result["checks"] if check["name"] == "signedTauriUpdaterMetadata")
    assert any("signature is required" in failure for failure in updater_check["failures"])


def test_validate_release_readiness_rejects_missing_media_preparation_report(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, _media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    media_check = next(check for check in result["checks"] if check["name"] == "mediaPreparationSmoke")
    assert "media preparation smoke report is required" in media_check["failures"]


def test_validate_release_readiness_rejects_media_preparation_without_ffprobe(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    write_media_preparation_report(media_preparation_report, require_ffprobe=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    media_check = next(check for check in result["checks"] if check["name"] == "mediaPreparationSmoke")
    assert any("ffprobe path" in failure for failure in media_check["failures"])
    assert any("requireFfprobe=true" in failure for failure in media_check["failures"])


def test_validate_release_readiness_rejects_runtime_dependency_budget_failures(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    write_runtime_dependency_footprint_report(runtime_dependency_footprint_report, ok=False)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    footprint_check = next(check for check in result["checks"] if check["name"] == "runtimeDependencyFootprint")
    assert any("budget failures" in failure for failure in footprint_check["failures"])


def test_validate_release_readiness_rejects_publication_report_with_non_https_final_url(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    data = json.loads(publication_report.read_text(encoding="utf-8"))
    data["finalUrl"] = "http://example.test/latest.json"
    publication_report.write_text(json.dumps(data), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    publication_check = next(check for check in result["checks"] if check["name"] == "publishedUpdaterManifest")
    assert any("finalUrl must be absolute HTTPS" in failure for failure in publication_check["failures"])


def test_validate_release_readiness_rejects_authenticode_report_for_wrong_artifact(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    write_authenticode_report_for(authenticode_report, artifact_path="unrelated-signed-tool.exe")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    authenticode_check = next(check for check in result["checks"] if check["name"] == "authenticodeSignatures")
    assert authenticode_check["details"]["expectedArtifactNames"] == ["Scriber_0.1.0_x64-setup.exe"]
    assert any("Scriber_0.1.0_x64-setup.exe" in failure for failure in authenticode_check["failures"])


def test_validate_release_readiness_requires_latest_json_artifact_names(tmp_path: Path) -> None:
    hardware_dir, metadata, _artifact_dir, _sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["artifacts"] = []
    metadata.write_text(json.dumps(data), encoding="utf-8")
    write_publication_report(publication_report, metadata)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
    )

    assert result["ok"] is False
    updater_check = next(check for check in result["checks"] if check["name"] == "signedTauriUpdaterMetadata")
    authenticode_check = next(check for check in result["checks"] if check["name"] == "authenticodeSignatures")
    assert any("artifacts must list at least one release artifact" in failure for failure in updater_check["failures"])
    assert authenticode_check["details"]["expectedArtifactNames"] == []
    assert any("Authenticode linkage" in failure for failure in authenticode_check["failures"])


def test_validate_release_readiness_cli_writes_summary(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    output = tmp_path / "readiness.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--hardware-input-dir",
            str(hardware_dir),
            "--updater-metadata",
            str(metadata),
            "--updater-artifact-dir",
            str(artifact_dir),
            "--sha256sums",
            str(sums),
            "--media-preparation-report",
            str(media_preparation_report),
            "--runtime-dependency-footprint-report",
            str(runtime_dependency_footprint_report),
            "--updater-publication-report",
            str(publication_report),
            "--authenticode-report",
            str(authenticode_report),
            "--expected-authenticode-publisher",
            "Scriber Release Publisher",
            "--require-authenticode-timestamp",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout_payload = json.loads(completed.stdout)
    written_payload = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_payload["ok"] is True
    assert written_payload == stdout_payload

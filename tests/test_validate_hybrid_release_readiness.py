from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from scripts.validate_hybrid_release_readiness import (
    REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS,
    validate_release_readiness,
)
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
        added = [
            {
                "endpointIdHash": f"{str(item['label']).casefold().replace(' ', '-')}-hash",
                "friendlyName": item["label"],
                "flow": "capture",
                "isDefault": False,
                "defaultRoles": [],
            }
            for item in change.get("added", [])
        ]
        removed = [
            {
                "endpointIdHash": f"{str(item['label']).casefold().replace(' ', '-')}-hash",
                "friendlyName": item["label"],
                "flow": "capture",
                "isDefault": False,
                "defaultRoles": [],
            }
            for item in change.get("removed", [])
        ]
        after_endpoints = [
            {
                "endpointIdHash": "built-in-hash",
                "friendlyName": "Built-in Mic",
                "flow": "capture",
                "isDefault": not bool(change.get("defaultChanged")),
                "defaultRoles": ["console"] if not bool(change.get("defaultChanged")) else [],
            }
        ] + added
        if change.get("defaultChanged"):
            after_endpoints.append(
                {
                    "endpointIdHash": "default-target-hash",
                    "friendlyName": "Default Target Mic",
                    "flow": "capture",
                    "isDefault": True,
                    "defaultRoles": ["console"],
                }
            )
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
                "rustNativeEndpointInventoryChange": {
                    "availableAfter": True,
                    "sourceAfter": "rust-wasapi",
                    "after": after_endpoints,
                    "added": added,
                    "removed": removed,
                    "defaultChanged": bool(change.get("defaultChanged")),
                },
                "deviceMonitorRefresh": {
                    "availableAfter": True,
                    "strategy": {"mode": "monitor-events", "forcedRefreshRequests": 0},
                    "nativeEventsActiveAfter": True,
                    "pollModeAfter": "native-event-safety",
                    "pollIntervalSecondsAfter": 900,
                    "pollRefreshDelta": 0,
                    "eventRefreshDelta": 1,
                    "portAudioRefreshDelta": 1,
                    "nativeHintDelta": 1,
                    "nativeHintPortAudioDelta": 1,
                    "after": {
                        "nativeEventsActive": True,
                        "pollMode": "native-event-safety",
                        "lastNativeHint": {"kind": "endpoint", "eventKind": "stateChanged"},
                    },
                },
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
    input_redaction_ok: bool = True,
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
                        "name": "inputReportRedaction",
                        "ok": input_redaction_ok,
                        "details": {"failureCount": 0 if input_redaction_ok else 1},
                    },
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
    runtime_mode: str = "tauri-supervised",
    launch_kind: str = "sidecar",
    api_version: str = "1",
    ready: bool = True,
    external_attach: bool = False,
    app_pid: int = 1111,
    backend_pid: int = 2222,
    backend_port: int = 8765,
    cleanup_verified: bool = True,
    live_verified: bool = True,
    duration_sec: float = 600,
    stability_duration_sec: float | None = None,
    non_recording_sample_count: int = 0,
    sample_count: int | None = None,
    probe_interval_sec: int = 5,
    health_ready: bool = True,
    stopped_listening: bool = False,
    started_recording_state: str = "recording",
    stopped_recording_state: str = "idle",
    include_audio_diagnostics: bool = True,
    audio_engine: str = "python",
    rust_audio_requested: bool = False,
    rust_audio_available: bool = False,
    frame_source: str = "sounddevice",
    fallback_circuit_open: bool = False,
    frame_pipe_sequence_errors: int = 0,
    frame_pipe_protocol_errors: int = 0,
    frame_pipe_prebuffer_after_live: int = 0,
) -> None:
    if stability_duration_sec is None:
        stability_duration_sec = duration_sec
    if sample_count is None:
        sample_count = max(1, int(stability_duration_sec / max(1, probe_interval_sec)))
    samples = []
    for index in range(max(0, sample_count)):
        elapsed_sec = (
            0.0
            if sample_count <= 1
            else round((stability_duration_sec / (sample_count - 1)) * index, 2)
        )
        sample = {
            "index": index + 1,
            "elapsedSec": elapsed_sec,
            "backendPid": backend_pid,
            "recordingState": "recording",
            "listening": True,
            "healthReady": health_ready,
        }
        if non_recording_sample_count > 0 and index == 0:
            sample["recordingState"] = "idle"
            sample["listening"] = False
        if include_audio_diagnostics:
            sample["audioDiagnostics"] = {
                "featureFlags": {
                    "audioEngine": audio_engine,
                    "requestedAudioEngine": audio_engine,
                    "rustAudioRequested": rust_audio_requested,
                    "rustAudioAvailable": rust_audio_available,
                },
                "activeCapture": {
                    "running": True,
                    "engine": audio_engine,
                    "requestedEngine": audio_engine,
                    "frameSource": frame_source,
                    "streamActive": True,
                    "callbackCount": 10 + index,
                    "nativeEndpointIdHash": "abc123" if frame_source == "rust-frame-pipe" else "",
                    "sourceNativeEndpointIdHash": "abc123" if frame_source == "rust-frame-pipe" else "",
                    "framePipeFramesRead": 10 + index if frame_source == "rust-frame-pipe" else None,
                    "framePipeAudioFramesRead": (10 + index) * 160 if frame_source == "rust-frame-pipe" else None,
                    "framePipeSequenceErrorCount": frame_pipe_sequence_errors,
                    "framePipeProtocolErrorCount": frame_pipe_protocol_errors,
                    "framePipePrebufferAfterLiveCount": frame_pipe_prebuffer_after_live,
                },
                "rustAudioFallbackCircuit": {
                    "available": True,
                    "open": fallback_circuit_open,
                    "reason": "pipeClosed" if fallback_circuit_open else "",
                    "remainingSeconds": 30 if fallback_circuit_open else None,
                    "cooldownSeconds": 60,
                },
            }
        samples.append(sample)
    path.write_text(
        json.dumps(
            {
                "ok": ok,
                "appPid": app_pid,
                "backendPid": backend_pid,
                "backendPort": backend_port,
                "runtimeMode": runtime_mode,
                "apiVersion": api_version,
                "ready": ready,
                "launchKind": launch_kind,
                "externalAttach": external_attach,
                "cleanupVerified": cleanup_verified,
                "liveRecording": {
                    "verified": live_verified,
                    "durationSec": duration_sec,
                    "probeIntervalSec": probe_interval_sec,
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
                        "probeIntervalSec": probe_interval_sec,
                        "sampleCount": sample_count,
                        "backendPid": backend_pid,
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
    pre_delay_mode: str = "auto",
    requested_pre_delay_ms: float | None = 80.0,
    actual_pre_delay_ms: float | None = 80.0,
    deadline_ms: float | None = 2000.0,
    restore_scheduled: bool | None = True,
    restore: dict[str, object] | None = None,
) -> None:
    if markers is None:
        markers = ["clipboard_set", "paste"]
    if restore is None:
        restore = {
            "scheduled": True,
            "attempted": False,
            "succeeded": None,
            "skippedReason": "scheduled",
            "errorCode": None,
        }
    payload: dict[str, object] = {
        "method": method,
        "dispatch": "ctrlV",
        "preDelayMode": pre_delay_mode,
        "requestedPreDelayMs": requested_pre_delay_ms,
        "deadlineMs": deadline_ms,
        "markers": markers,
        "restore": restore,
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
            "preDelay": actual_pre_delay_ms,
            "pasteDispatch": 3.0,
            "total": 10.0,
        },
    }
    if restore_scheduled is not None:
        payload["restoreScheduled"] = restore_scheduled
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
                            **payload,
                        },
                    },
                },
                "validateOnly": validate_only,
            }
        ),
        encoding="utf-8",
    )


def write_tauri_text_injection_matrix_report(
    path: Path,
    *,
    scenario_ids: list[str] | None = None,
    weak_scenario: str = "",
    validate_only: bool = False,
) -> None:
    scenario_ids = scenario_ids or list(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS)
    scenarios = []
    for scenario_id in scenario_ids:
        smoke_path = path.parent / f"tauri-text-injection-{scenario_id}.json"
        write_tauri_text_injection_smoke_report(
            smoke_path,
            ok=scenario_id != weak_scenario,
            shell_ipc_available=scenario_id != weak_scenario,
            markers=["clipboard_set"] if scenario_id == weak_scenario else None,
        )
        scenarios.append(
            {
                "id": scenario_id,
                "label": REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS.get(scenario_id, scenario_id),
                "report": json.loads(smoke_path.read_text(encoding="utf-8")),
            }
        )

    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generatedAtUtc": "2026-06-11T12:00:00Z",
                "method": "tauri",
                "ok": True,
                "validateOnly": validate_only,
                "scenarios": scenarios,
                "summary": {
                    "scenarioCount": len(scenarios),
                    "requiredScenarioCount": len(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
                },
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


def test_validate_release_readiness_rejects_missing_required_device_refresh_evidence(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    usb_add = hardware_dir / "microphone-hardware-usb-add.json"
    payload = json.loads(usb_add.read_text(encoding="utf-8"))
    payload["result"].pop("deviceMonitorRefresh", None)
    usb_add.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        require_device_refresh_evidence=True,
    )

    assert result["ok"] is False
    hardware_check = next(check for check in result["checks"] if check["name"] == "physicalMicrophoneMatrix")
    assert "usb-add: result.deviceMonitorRefresh must be present" in hardware_check["failures"]


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


def test_validate_release_readiness_rejects_rust_audio_sidecar_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(rust_audio_report)
    payload = json.loads(rust_audio_report.read_text(encoding="utf-8"))
    payload["captures"][0]["start"]["endpointId"] = r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-sidecar-device}"
    payload["captures"][0]["start"]["framePipe"] = r"\\.\pipe\scriber-audio-frame-secret"
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

    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert result["ok"] is False
    assert (
        "Rust audio sidecar smoke contains raw native endpoint ID at "
        "captures[0].start.endpointId"
        in rust_audio_check["failures"]
    )
    assert (
        "Rust audio sidecar smoke contains unredacted endpointId value at "
        "captures[0].start.endpointId"
        in rust_audio_check["failures"]
    )
    assert (
        "Rust audio sidecar smoke contains raw Scriber pipe name at "
        "captures[0].start.framePipe"
        in rust_audio_check["failures"]
    )


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
    assert live_check["details"]["runtimeMode"] == "tauri-supervised"
    assert live_check["details"]["launchKind"] == "sidecar"
    assert live_check["details"]["ready"] is True
    assert live_check["details"]["stability"]["sampleCount"] >= 60


def test_validate_release_readiness_accepts_installed_live_recording_rust_audio_evidence(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        audio_engine="rust-prototype",
        rust_audio_requested=True,
        rust_audio_available=True,
        frame_source="rust-frame-pipe",
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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is True
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    rust_evidence = live_check["details"]["rustAudioEvidence"]
    assert rust_evidence["audioDiagnosticsSampleCount"] == rust_evidence["sampleCount"]
    assert rust_evidence["rustFramePipeSampleCount"] == rust_evidence["sampleCount"]
    assert rust_evidence["fallbackCircuitOpenCount"] == 0


def test_validate_release_readiness_rejects_stale_installed_live_recording_rust_audio_counters(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        audio_engine="rust-prototype",
        rust_audio_requested=True,
        rust_audio_available=True,
        frame_source="rust-frame-pipe",
    )
    payload = json.loads(live_recording_report.read_text(encoding="utf-8"))
    samples = payload["liveRecording"]["stability"]["samples"]
    for sample in samples:
        active_capture = sample["audioDiagnostics"]["activeCapture"]
        active_capture["callbackCount"] = 10
        active_capture["framePipeFramesRead"] = 10
        active_capture["framePipeAudioFramesRead"] = 1600
    live_recording_report.write_text(json.dumps(payload), encoding="utf-8")

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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert any(
        "activeCapture.callbackCount must increase between stability samples" in failure
        for failure in live_check["failures"]
    )


def test_validate_release_readiness_rejects_python_installed_live_recording_when_rust_audio_is_required(tmp_path: Path) -> None:
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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert any("audioEngine must be rust-prototype" in failure for failure in live_check["failures"])


def test_validate_release_readiness_rejects_installed_live_recording_without_rust_audio_diagnostics(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        include_audio_diagnostics=False,
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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert any("audioDiagnostics must be an object" in failure for failure in live_check["failures"])


def test_validate_release_readiness_rejects_installed_live_recording_with_open_rust_fallback_circuit(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        audio_engine="rust-prototype",
        rust_audio_requested=True,
        rust_audio_available=True,
        frame_source="rust-frame-pipe",
        fallback_circuit_open=True,
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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert any("rustAudioFallbackCircuit.open must be false" in failure for failure in live_check["failures"])


def test_validate_release_readiness_rejects_installed_live_recording_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        audio_engine="rust-prototype",
        rust_audio_requested=True,
        rust_audio_available=True,
        frame_source="rust-frame-pipe",
    )
    payload = json.loads(live_recording_report.read_text(encoding="utf-8"))
    active_capture = payload["liveRecording"]["stability"]["samples"][0]["audioDiagnostics"]["activeCapture"]
    active_capture["endpointId"] = r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-live-device}"
    active_capture["framePipe"] = r"\\.\pipe\scriber-audio-frame-secret"
    live_recording_report.write_text(json.dumps(payload), encoding="utf-8")

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
        require_installed_live_recording_rust_audio=True,
        min_installed_live_recording_duration_sec=600,
    )

    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert (
        "installed live recording smoke contains raw native endpoint ID at "
        "liveRecording.stability.samples[0].audioDiagnostics.activeCapture.endpointId"
        in live_check["failures"]
    )
    assert (
        "installed live recording smoke contains unredacted endpointId value at "
        "liveRecording.stability.samples[0].audioDiagnostics.activeCapture.endpointId"
        in live_check["failures"]
    )
    assert (
        "installed live recording smoke contains raw Scriber pipe name at "
        "liveRecording.stability.samples[0].audioDiagnostics.activeCapture.framePipe"
        in live_check["failures"]
    )


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


def test_validate_release_readiness_rejects_missing_installed_live_recording_report_when_rust_audio_is_required(tmp_path: Path) -> None:
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
        require_installed_live_recording_rust_audio=True,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert live_check["details"]["requireRustAudio"] is True
    assert "Installed live recording smoke report is required" in live_check["failures"]


def test_validate_release_readiness_rejects_missing_installed_live_recording_report_when_min_duration_is_set(tmp_path: Path) -> None:
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
        min_installed_live_recording_duration_sec=600,
    )

    assert result["ok"] is False
    live_check = next(check for check in result["checks"] if check["name"] == "installedLiveRecordingSmoke")
    assert live_check["details"]["required"] is True
    assert live_check["details"]["requireInstalledLiveRecordingSmoke"] is False
    assert live_check["details"]["minDurationSec"] == 600
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


def test_validate_release_readiness_rejects_non_installed_live_recording_metadata(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        runtime_mode="dev",
        launch_kind="external-python",
        external_attach=True,
        ready=False,
        api_version="0",
        app_pid=2222,
        backend_pid=2222,
        backend_port=0,
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
    failures = live_check["failures"]
    assert "installed live recording smoke runtimeMode must be tauri-supervised" in failures
    assert "installed live recording smoke launchKind must be sidecar" in failures
    assert "installed live recording smoke must not use an external backend" in failures
    assert "installed live recording smoke ready must be true" in failures
    assert "installed live recording smoke apiVersion must be 1" in failures
    assert "installed live recording smoke backendPort must be positive" in failures
    assert "installed live recording smoke appPid and backendPid must differ" in failures


def test_validate_release_readiness_rejects_sparse_installed_live_recording_samples(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    live_recording_report = tmp_path / "installed-live-recording-smoke.json"
    write_installed_live_recording_smoke_report(
        live_recording_report,
        duration_sec=600,
        stability_duration_sec=600,
        sample_count=3,
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
    assert any("stability.sampleCount must cover at least 50% of expected probes" in failure for failure in live_check["failures"])


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


def test_validate_release_readiness_rejects_tauri_text_injection_without_auto_pre_delay(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report, pre_delay_mode="")

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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert "Tauri text injection smoke response preDelayMode must be auto" in injection_check["failures"]


def test_validate_release_readiness_rejects_tauri_text_injection_without_deadline(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report, deadline_ms=None)

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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert "Tauri text injection smoke response deadlineMs must be positive" in injection_check["failures"]


def test_validate_release_readiness_rejects_tauri_text_injection_past_deadline(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report, deadline_ms=5.0)

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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert "Tauri text injection smoke timing total must not exceed response deadlineMs" in injection_check["failures"]


def test_validate_release_readiness_rejects_tauri_text_injection_without_restore_evidence(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(
        injection_report,
        restore_scheduled=None,
        restore={},
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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    failures = "\n".join(injection_check["failures"])
    assert "response restoreScheduled must be boolean" in failures
    assert "response restore.scheduled must be boolean" in failures
    assert "response restore.attempted must be boolean" in failures


def test_validate_release_readiness_rejects_tauri_text_injection_raw_foreground_diagnostics(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report)
    payload = json.loads(injection_report.read_text(encoding="utf-8"))
    foreground = payload["shellIpc"]["lastResponse"]["payload"]["foregroundBefore"]
    foreground["title"] = "Sensitive Word Document.docx"
    foreground["processId"] = 1234
    foreground["windowHash"] = ""
    injection_report.write_text(json.dumps(payload), encoding="utf-8")

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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    failures = "\n".join(injection_check["failures"])
    assert "response foregroundBefore.title must not expose raw foreground data" in failures
    assert "response foregroundBefore.processId must not expose raw foreground data" in failures
    assert "response foregroundBefore.windowHash must be present when available" in failures


def test_validate_release_readiness_rejects_tauri_text_injection_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    injection_report = tmp_path / "tauri-text-injection-smoke.json"
    write_tauri_text_injection_smoke_report(injection_report)
    payload = json.loads(injection_report.read_text(encoding="utf-8"))
    payload["shellIpc"]["lastFallbackReason"] = r"failed \\.\pipe\scriber-shell-secret"
    payload["shellIpc"]["sessionToken"] = "secret-token"
    injection_report.write_text(json.dumps(payload), encoding="utf-8")

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

    injection_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionSmoke")
    assert (
        "Tauri text injection smoke contains raw Shell IPC pipe name at shellIpc.lastFallbackReason"
        in injection_check["failures"]
    )
    assert (
        "Tauri text injection smoke contains unredacted token-like value at shellIpc.sessionToken"
        in injection_check["failures"]
    )


def test_validate_release_readiness_accepts_required_tauri_text_injection_matrix(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    matrix_report = tmp_path / "tauri-text-injection-matrix.json"
    write_tauri_text_injection_matrix_report(matrix_report)

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_matrix_report=matrix_report,
        require_tauri_text_injection_matrix=True,
    )

    assert result["ok"] is True
    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    assert matrix_check["details"]["validScenarioCount"] == len(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS)
    assert "notepad" in matrix_check["details"]["coveredScenarioIds"]
    assert "restore-same-text-copy" in matrix_check["details"]["coveredScenarioIds"]


def test_validate_release_readiness_rejects_tauri_text_injection_matrix_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    matrix_report = tmp_path / "tauri-text-injection-matrix.json"
    write_tauri_text_injection_matrix_report(matrix_report)
    payload = json.loads(matrix_report.read_text(encoding="utf-8"))
    first = payload["scenarios"][0]["report"]
    first["shellIpc"]["lastError"] = r"transport failed at \\\\.\\pipe\\scriber-shell-secret"
    first["shellIpc"]["requestToken"] = "secret-token"
    matrix_report.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_matrix_report=matrix_report,
        require_tauri_text_injection_matrix=True,
    )

    scenario_id = payload["scenarios"][0]["id"]
    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    assert (
        f"Tauri text injection matrix scenario {scenario_id} contains raw Shell IPC pipe name at shellIpc.lastError"
        in matrix_check["failures"]
    )
    assert (
        f"Tauri text injection matrix scenario {scenario_id} contains unredacted token-like value at shellIpc.requestToken"
        in matrix_check["failures"]
    )


def test_validate_release_readiness_rejects_missing_required_tauri_text_injection_matrix(tmp_path: Path) -> None:
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
        require_tauri_text_injection_matrix=True,
    )

    assert result["ok"] is False
    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    assert "Tauri text injection matrix report is required" in matrix_check["failures"]


def test_validate_release_readiness_rejects_weak_tauri_text_injection_matrix(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    matrix_report = tmp_path / "tauri-text-injection-matrix.json"
    scenario_ids = list(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS)
    scenario_ids.remove("outlook")
    write_tauri_text_injection_matrix_report(
        matrix_report,
        scenario_ids=scenario_ids,
        weak_scenario="word",
        validate_only=True,
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
        tauri_text_injection_matrix_report=matrix_report,
        require_tauri_text_injection_matrix=True,
    )

    assert result["ok"] is False
    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    assert "Tauri text injection matrix report must not be validate-only evidence" in matrix_check["failures"]
    assert (
        "Tauri text injection matrix scenario word shellIpc.available must be true"
        in matrix_check["failures"]
    )
    assert (
        "Tauri text injection matrix scenario word response markers must include clipboard_set and paste"
        in matrix_check["failures"]
    )
    assert any("missing required scenario(s): outlook" in failure for failure in matrix_check["failures"])


def test_validate_release_readiness_rejects_word_matrix_without_positive_pre_delay(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    matrix_report = tmp_path / "tauri-text-injection-matrix.json"
    write_tauri_text_injection_matrix_report(matrix_report)
    payload = json.loads(matrix_report.read_text(encoding="utf-8"))
    word = next(scenario for scenario in payload["scenarios"] if scenario["id"] == "word")
    word["report"]["shellIpc"]["lastResponse"]["payload"]["timingsMs"]["preDelay"] = 0.0
    matrix_report.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_matrix_report=matrix_report,
        require_tauri_text_injection_matrix=True,
    )

    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    assert (
        "Tauri text injection matrix scenario word timing preDelay must be positive for Word/Outlook scenario"
        in matrix_check["failures"]
    )


def test_validate_release_readiness_rejects_matrix_restore_failure(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    matrix_report = tmp_path / "tauri-text-injection-matrix.json"
    write_tauri_text_injection_matrix_report(matrix_report)
    payload = json.loads(matrix_report.read_text(encoding="utf-8"))
    clipboard_text = next(scenario for scenario in payload["scenarios"] if scenario["id"] == "clipboard-text")
    clipboard_text["report"]["shellIpc"]["lastResponse"]["payload"]["restoreScheduled"] = False
    clipboard_text["report"]["shellIpc"]["lastResponse"]["payload"]["restore"] = {
        "scheduled": False,
        "attempted": True,
        "succeeded": False,
        "skippedReason": "restoreFailed",
        "errorCode": "clipboardSetFailed",
    }
    matrix_report.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_release_readiness(
        hardware_input_dir=hardware_dir,
        updater_metadata=metadata,
        updater_artifact_dir=artifact_dir,
        sha256sums=sums,
        media_preparation_report=media_preparation_report,
        runtime_dependency_footprint_report=runtime_dependency_footprint_report,
        updater_publication_report=publication_report,
        authenticode_report=authenticode_report,
        tauri_text_injection_matrix_report=matrix_report,
        require_tauri_text_injection_matrix=True,
    )

    matrix_check = next(check for check in result["checks"] if check["name"] == "tauriTextInjectionMatrix")
    failures = "\n".join(matrix_check["failures"])
    assert "Tauri text injection matrix scenario clipboard-text response restore.errorCode must be empty" in failures
    assert "Tauri text injection matrix scenario clipboard-text response restore.succeeded must not be false" in failures
    assert (
        "Tauri text injection matrix scenario clipboard-text response restore.skippedReason must not be restoreFailed"
        in failures
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


def test_validate_release_readiness_rejects_unredacted_recording_hot_path_comparison_inputs(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report, input_redaction_ok=False)

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
    assert "recording hot-path comparison check failed: inputReportRedaction" in comparison_check["failures"]


def test_validate_release_readiness_rejects_stale_recording_hot_path_comparison_without_redaction_check(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    comparison_report = tmp_path / "recording-hot-path-python-rust-comparison.json"
    write_recording_hot_path_comparison_report(comparison_report)
    payload = json.loads(comparison_report.read_text(encoding="utf-8"))
    payload["checks"] = [
        check
        for check in payload["checks"]
        if check.get("name") != "inputReportRedaction"
    ]
    comparison_report.write_text(json.dumps(payload), encoding="utf-8")

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
    assert "recording hot-path comparison is missing check: inputReportRedaction" in comparison_check["failures"]


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


def test_validate_release_readiness_rejects_reused_sidecar_report_without_required_prewarm_adoption(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    rust_audio_report = tmp_path / "rust-audio-sidecar-smoke.json"
    write_rust_audio_sidecar_report(
        rust_audio_report,
        prewarm_before_capture=False,
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
        require_rust_audio_sidecar_prewarm_adoption=True,
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert rust_audio_check["details"]["requirePrewarmAdoption"] is True
    failures = "\n".join(rust_audio_check["failures"])
    assert "requested.prewarmBeforeCapture must be true" in failures
    assert "totalAdoptedPrewarmBlocks must be positive" in failures
    assert "adoptedPrewarm.adopted must be true" in failures


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


def test_validate_release_readiness_rejects_rust_audio_prewarm_sidecar_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    prewarm_report = tmp_path / "rust-audio-prewarm-sidecar-smoke.json"
    write_rust_audio_prewarm_sidecar_report(prewarm_report, mode="wasapi")
    payload = json.loads(prewarm_report.read_text(encoding="utf-8"))
    payload["prewarm"]["start"]["endpointId"] = r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-prewarm-device}"
    payload["prewarm"]["start"]["framePipe"] = r"\\.\pipe\scriber-audio-prewarm-secret"
    prewarm_report.write_text(json.dumps(payload), encoding="utf-8")

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

    prewarm_check = next(check for check in result["checks"] if check["name"] == "rustAudioPrewarmSidecarSmoke")
    assert result["ok"] is False
    assert (
        "Rust audio prewarm sidecar smoke contains raw native endpoint ID at "
        "prewarm.start.endpointId"
        in prewarm_check["failures"]
    )
    assert (
        "Rust audio prewarm sidecar smoke contains raw Scriber pipe name at "
        "prewarm.start.framePipe"
        in prewarm_check["failures"]
    )


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


def test_validate_release_readiness_rejects_rust_audio_app_prewarm_redaction_leaks(tmp_path: Path) -> None:
    hardware_dir, metadata, artifact_dir, sums, media_preparation_report, runtime_dependency_footprint_report, publication_report, authenticode_report = write_complete_evidence(tmp_path)
    app_prewarm_report = tmp_path / "rust-audio-app-prewarm-smoke.json"
    write_rust_audio_app_prewarm_report(app_prewarm_report)
    payload = json.loads(app_prewarm_report.read_text(encoding="utf-8"))
    payload["sourceFinal"]["endpointId"] = r"SWD\MMDEVAPI\{0.0.1.00000000}.{raw-app-device}"
    payload["sourceFinal"]["framePipe"] = r"\\.\pipe\scriber-audio-app-secret"
    app_prewarm_report.write_text(json.dumps(payload), encoding="utf-8")

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

    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    assert result["ok"] is False
    assert (
        "Rust audio app prewarm smoke contains raw native endpoint ID at "
        "sourceFinal.endpointId"
        in app_check["failures"]
    )
    assert (
        "Rust audio app prewarm smoke contains raw Scriber pipe name at "
        "sourceFinal.framePipe"
        in app_check["failures"]
    )


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


def test_validate_release_readiness_rejects_missing_rust_audio_app_prewarm_report_when_min_duration_is_set(tmp_path: Path) -> None:
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
        min_rust_audio_app_prewarm_duration_sec=600,
        min_rust_audio_app_prewarm_prewarm_duration_sec=1800,
    )

    assert result["ok"] is False
    app_check = next(check for check in result["checks"] if check["name"] == "rustAudioAppPrewarmSmoke")
    assert app_check["details"]["required"] is True
    assert app_check["details"]["requireRustAudioAppPrewarmSmoke"] is False
    assert app_check["details"]["minDurationSec"] == 600
    assert app_check["details"]["minPrewarmDurationSec"] == 1800
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


def test_validate_release_readiness_rejects_missing_rust_audio_sidecar_report_when_min_duration_is_set(tmp_path: Path) -> None:
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
        min_rust_audio_duration_sec=600,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert rust_audio_check["details"]["required"] is True
    assert rust_audio_check["details"]["requireRustAudioSidecarSmoke"] is False
    assert rust_audio_check["details"]["minDurationSec"] == 600
    assert "Rust audio sidecar smoke report is required" in rust_audio_check["failures"]


def test_validate_release_readiness_rejects_missing_sidecar_report_when_prewarm_adoption_is_required(tmp_path: Path) -> None:
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
        require_rust_audio_sidecar_prewarm_adoption=True,
    )

    assert result["ok"] is False
    rust_audio_check = next(check for check in result["checks"] if check["name"] == "rustAudioSidecarSmoke")
    assert rust_audio_check["details"]["requirePrewarmAdoption"] is True
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

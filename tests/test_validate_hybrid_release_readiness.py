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
                    "selectedHashVerified": selected_hash_verified,
                },
                "captures": [
                    {
                        "name": "default",
                        "ok": True,
                        "start": {
                            "nativeEndpointIdHash": "abc123",
                            "endpointSelection": {"mode": "default"},
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

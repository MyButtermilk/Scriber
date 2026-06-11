from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_microphone_hardware_matrix import validate_matrix as validate_microphone_matrix
from scripts.validate_tauri_updater_metadata import DEFAULT_METADATA, sha256_file, validate_local_artifacts, validate_metadata


SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
RUST_AUDIO_ENGINE = "rust-prototype"
RUST_AUDIO_FRAME_SOURCE = "rust-frame-pipe"

REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS: dict[str, str] = {
    "notepad": "Notepad plain text target",
    "word": "Microsoft Word document target",
    "outlook": "Microsoft Outlook compose target",
    "browser-input": "Browser text input target",
    "browser-contenteditable": "Browser contenteditable target",
    "electron": "Electron application target",
    "elevated-target": "Elevated target with normal Scriber",
    "elevated-scriber": "Elevated Scriber with normal target",
    "clipboard-text": "Existing text clipboard restore path",
    "clipboard-non-text": "Existing non-text clipboard handling",
    "clipboard-locked": "Clipboard locked by another process",
    "restore-user-copy": "User copy during restore delay",
    "restore-same-text-copy": "User copy of same text during restore delay",
}

OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS: dict[str, str] = {
    "remote-desktop": "Remote Desktop target, when available",
}


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    failures: list[str]
    details: dict[str, Any]

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "failures": self.failures,
            "details": self.details,
        }


def validate_release_readiness(
    *,
    hardware_input_dir: Path,
    updater_metadata: Path,
    updater_artifact_dir: Path | None = None,
    sha256sums: Path | None = None,
    media_preparation_report: Path | None = None,
    runtime_dependency_footprint_report: Path | None = None,
    updater_publication_report: Path | None = None,
    authenticode_report: Path | None = None,
    rust_audio_sidecar_report: Path | None = None,
    rust_audio_prewarm_sidecar_report: Path | None = None,
    rust_audio_app_prewarm_report: Path | None = None,
    installed_live_recording_smoke_report: Path | None = None,
    tauri_text_injection_smoke_report: Path | None = None,
    tauri_text_injection_matrix_report: Path | None = None,
    recording_hot_path_comparison_report: Path | None = None,
    require_rust_audio_sidecar_smoke: bool = False,
    require_rust_audio_prewarm_sidecar_smoke: bool = False,
    require_rust_audio_app_prewarm_smoke: bool = False,
    require_installed_live_recording_smoke: bool = False,
    require_tauri_text_injection_smoke: bool = False,
    require_tauri_text_injection_matrix: bool = False,
    require_recording_hot_path_comparison: bool = False,
    require_installed_live_recording_rust_audio: bool = False,
    require_rust_endpoint_inventory: bool = False,
    require_device_refresh_evidence: bool = False,
    require_rust_audio_sidecar_prewarm_adoption: bool = False,
    min_rust_audio_duration_sec: float = 0.0,
    min_rust_audio_app_prewarm_duration_sec: float = 0.0,
    min_rust_audio_app_prewarm_prewarm_duration_sec: float = 0.0,
    min_installed_live_recording_duration_sec: float = 0.0,
    expected_authenticode_publisher: str = "",
    require_authenticode_timestamp: bool = False,
    platform: str = "windows-x86_64",
) -> dict[str, Any]:
    expected_signed_artifact_names = read_updater_artifact_names(updater_metadata)
    effective_require_rust_endpoint_inventory = (
        require_rust_endpoint_inventory
        or require_rust_audio_sidecar_smoke
        or require_rust_audio_app_prewarm_smoke
    )
    effective_require_device_refresh_evidence = (
        require_device_refresh_evidence
        or require_rust_audio_sidecar_smoke
        or require_rust_audio_app_prewarm_smoke
    )
    checks = [
        validate_physical_microphone_matrix(
            hardware_input_dir,
            require_rust_endpoint_inventory=effective_require_rust_endpoint_inventory,
            require_device_refresh_evidence=effective_require_device_refresh_evidence,
        ),
        validate_signed_updater_metadata(
            updater_metadata,
            platform=platform,
            artifact_dir=updater_artifact_dir,
            sha256sums=sha256sums,
        ),
        validate_media_preparation_report(media_preparation_report),
        validate_runtime_dependency_footprint_report(runtime_dependency_footprint_report),
        validate_updater_publication_report(updater_publication_report, metadata_path=updater_metadata),
        validate_authenticode_report(
            authenticode_report,
            expected_publisher=expected_authenticode_publisher,
            require_timestamp=require_authenticode_timestamp,
            expected_artifact_names=expected_signed_artifact_names,
        ),
    ]
    if (
        require_rust_audio_sidecar_smoke
        or require_rust_audio_sidecar_prewarm_adoption
        or rust_audio_sidecar_report is not None
    ):
        checks.append(
            validate_rust_audio_sidecar_report(
                rust_audio_sidecar_report,
                required=require_rust_audio_sidecar_smoke,
                min_duration_sec=min_rust_audio_duration_sec,
                require_prewarm_adoption=require_rust_audio_sidecar_prewarm_adoption,
            )
        )
    if require_rust_audio_prewarm_sidecar_smoke or rust_audio_prewarm_sidecar_report is not None:
        checks.append(
            validate_rust_audio_prewarm_sidecar_report(
                rust_audio_prewarm_sidecar_report,
                required=require_rust_audio_prewarm_sidecar_smoke,
            )
        )
    if require_rust_audio_app_prewarm_smoke or rust_audio_app_prewarm_report is not None:
        checks.append(
            validate_rust_audio_app_prewarm_report(
                rust_audio_app_prewarm_report,
                required=require_rust_audio_app_prewarm_smoke,
                min_duration_sec=min_rust_audio_app_prewarm_duration_sec,
                min_prewarm_duration_sec=min_rust_audio_app_prewarm_prewarm_duration_sec,
            )
        )
    if (
        require_installed_live_recording_smoke
        or require_installed_live_recording_rust_audio
        or installed_live_recording_smoke_report is not None
    ):
        checks.append(
            validate_installed_live_recording_smoke_report(
                installed_live_recording_smoke_report,
                required=require_installed_live_recording_smoke,
                min_duration_sec=min_installed_live_recording_duration_sec,
                require_rust_audio=require_installed_live_recording_rust_audio,
            )
        )
    if require_tauri_text_injection_smoke or tauri_text_injection_smoke_report is not None:
        checks.append(
            validate_tauri_text_injection_smoke_report(
                tauri_text_injection_smoke_report,
                required=require_tauri_text_injection_smoke,
            )
        )
    if require_tauri_text_injection_matrix or tauri_text_injection_matrix_report is not None:
        checks.append(
            validate_tauri_text_injection_matrix_report(
                tauri_text_injection_matrix_report,
                required=require_tauri_text_injection_matrix,
            )
        )
    if require_recording_hot_path_comparison or recording_hot_path_comparison_report is not None:
        checks.append(
            validate_recording_hot_path_comparison_report(
                recording_hot_path_comparison_report,
                required=require_recording_hot_path_comparison,
            )
        )
    return {
        "ok": all(check.ok for check in checks),
        "checks": [check.to_public() for check in checks],
    }


def validate_physical_microphone_matrix(
    input_dir: Path,
    *,
    require_rust_endpoint_inventory: bool = False,
    require_device_refresh_evidence: bool = False,
) -> ReadinessCheck:
    payload = validate_microphone_matrix(
        input_dir=input_dir,
        require_rust_endpoint_inventory=require_rust_endpoint_inventory,
        require_device_refresh_evidence=require_device_refresh_evidence,
    )
    failures: list[str] = []
    if payload["failedCount"]:
        failures.append(f"{payload['failedCount']} physical microphone scenario(s) failed or are missing")
    for scenario in payload["scenarios"]:
        for failure in scenario["failures"]:
            failures.append(f"{scenario['scenario']}: {failure}")
    return ReadinessCheck(
        name="physicalMicrophoneMatrix",
        ok=payload["ok"],
        failures=failures,
        details={
            "inputDir": payload["inputDir"],
            "passedCount": payload["passedCount"],
            "failedCount": payload["failedCount"],
            "requiredScenarios": payload["requiredScenarios"],
            "requireRustEndpointInventory": payload.get("requireRustEndpointInventory", False),
            "requireDeviceRefreshEvidence": payload.get("requireDeviceRefreshEvidence", False),
        },
    )


def validate_signed_updater_metadata(
    metadata_path: Path,
    *,
    platform: str,
    artifact_dir: Path | None,
    sha256sums: Path | None,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "metadata": str(metadata_path),
        "platform": platform,
        "artifactDir": str(artifact_dir) if artifact_dir else "",
        "sha256Sums": str(sha256sums) if sha256sums else "",
        "localArtifactsVerified": 0,
    }
    try:
        if not metadata_path.is_file():
            raise FileNotFoundError(f"latest.json was not found: {metadata_path}")
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("latest.json root must be a JSON object")
        validate_metadata(data, platform=platform, require_signatures=True, allow_local_urls=False)
        artifacts = data.get("artifacts", [])
        details["artifactCount"] = len(artifacts) if isinstance(artifacts, list) else 0
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("latest.json artifacts must list at least one release artifact for final readiness")
        if artifact_dir:
            if not artifact_dir.is_dir():
                raise FileNotFoundError(f"release artifact directory was not found: {artifact_dir}")
            if sha256sums and not sha256sums.is_file():
                raise FileNotFoundError(f"SHA256SUMS.txt was not found: {sha256sums}")
            details["localArtifactsVerified"] = validate_local_artifacts(
                data,
                artifact_dir=artifact_dir,
                sha256sums_path=sha256sums,
            )
    except Exception as exc:
        failures.append(str(exc))
    return ReadinessCheck(
        name="signedTauriUpdaterMetadata",
        ok=not failures,
        failures=failures,
        details=details,
    )


def validate_media_preparation_report(report_path: Path | None) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "requiredChecks": [
            "file_upload_compression",
            "video_upload_audio_extraction",
            "youtube_post_download_normalization",
            "azure_mai_audio_preparation",
            "ffprobe_duration_probe",
        ],
    }
    if report_path is None:
        failures.append("media preparation smoke report is required")
        return ReadinessCheck("mediaPreparationSmoke", False, failures, details)

    report = read_json_object(report_path, failures, "media preparation smoke report")
    if not report:
        return ReadinessCheck("mediaPreparationSmoke", False, failures, details)

    details.update(
        {
            "apiVersion": report.get("apiVersion", ""),
            "durationMs": report.get("durationMs", None),
            "mediaTools": report.get("mediaTools", {}),
            "summary": report.get("summary", {}),
        }
    )
    if report.get("ok") is not True:
        failures.append("media preparation smoke report ok must be true")
    if str(report.get("apiVersion") or "") != "1":
        failures.append("media preparation smoke report apiVersion must be 1")

    media_tools = report.get("mediaTools")
    if not isinstance(media_tools, dict):
        failures.append("media preparation smoke report mediaTools must be an object")
        media_tools = {}
    if not str(media_tools.get("ffmpeg") or "").strip():
        failures.append("media preparation smoke report must record the ffmpeg path")
    if not str(media_tools.get("ffprobe") or "").strip():
        failures.append("media preparation smoke report must record the ffprobe path for standard release readiness")
    if media_tools.get("requireFfprobe") is not True:
        failures.append("media preparation smoke report must be produced with requireFfprobe=true")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("media preparation smoke report summary must be an object")
    elif summary.get("failedChecks") != 0:
        failures.append("media preparation smoke report failedChecks must be 0")

    checks = report.get("checks")
    if not isinstance(checks, list):
        failures.append("media preparation smoke report checks must be a list")
        checks = []
    checks_by_name: dict[str, dict[str, Any]] = {}
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            failures.append(f"media preparation smoke checks[{index}] must be an object")
            continue
        name = str(check.get("name") or "")
        if not name:
            failures.append(f"media preparation smoke checks[{index}].name is required")
            continue
        checks_by_name[name] = check
        if check.get("ok") is not True:
            failures.append(f"media preparation smoke check failed: {name}")

    for required_name in details["requiredChecks"]:
        check = checks_by_name.get(required_name)
        if check is None:
            failures.append(f"media preparation smoke is missing required check: {required_name}")
            continue
        if required_name == "file_upload_compression":
            output = check.get("output")
            if not isinstance(output, dict) or output.get("suffix") != ".webm":
                failures.append("file_upload_compression must produce a .webm output")
        if required_name == "video_upload_audio_extraction":
            output = check.get("output")
            if not isinstance(output, dict) or output.get("suffix") != ".webm":
                failures.append("video_upload_audio_extraction must produce a .webm output")
        if required_name == "youtube_post_download_normalization":
            output = check.get("output")
            if not isinstance(output, dict) or output.get("suffix") != ".webm":
                failures.append("youtube_post_download_normalization must produce a .webm output")
        if required_name == "azure_mai_audio_preparation":
            prepared = check.get("prepared")
            if not isinstance(prepared, dict) or prepared.get("suffix") != ".mp3":
                failures.append("azure_mai_audio_preparation must produce a .mp3 prepared file")
            if isinstance(prepared, dict) and prepared.get("contentType") != "audio/mpeg":
                failures.append("azure_mai_audio_preparation must report audio/mpeg content type")
        if required_name == "ffprobe_duration_probe":
            if check.get("ffprobeAvailable") is not True:
                failures.append("ffprobe_duration_probe must report ffprobeAvailable=true")
            duration = check.get("durationSeconds")
            if not isinstance(duration, (int, float)) or duration <= 0:
                failures.append("ffprobe_duration_probe must report a positive duration")

    details["checkCount"] = len(checks)
    details["passedCheckCount"] = sum(1 for check in checks if isinstance(check, dict) and check.get("ok") is True)

    return ReadinessCheck("mediaPreparationSmoke", not failures, failures, details)


def validate_runtime_dependency_footprint_report(report_path: Path | None) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "trackedDependencies": ["scipy", "onnxruntime"],
    }
    if report_path is None:
        failures.append("runtime dependency footprint report is required")
        return ReadinessCheck("runtimeDependencyFootprint", False, failures, details)

    report = read_json_object(report_path, failures, "runtime dependency footprint report")
    if not report:
        return ReadinessCheck("runtimeDependencyFootprint", False, failures, details)

    details.update(
        {
            "apiVersion": report.get("apiVersion", ""),
            "summary": report.get("summary", {}),
            "budgets": report.get("budgets", {}),
        }
    )
    if report.get("ok") is not True:
        failures.append("runtime dependency footprint report ok must be true")
    if str(report.get("apiVersion") or "") != "1":
        failures.append("runtime dependency footprint report apiVersion must be 1")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("runtime dependency footprint report summary must be an object")
        summary = {}
    missing = summary.get("missingRequiredPaths", [])
    if not isinstance(missing, list):
        failures.append("runtime dependency footprint missingRequiredPaths must be a list")
    elif missing:
        failures.append("runtime dependency footprint has missing required paths: " + ", ".join(map(str, missing)))
    budget_failures = summary.get("budgetFailures", [])
    if not isinstance(budget_failures, list):
        failures.append("runtime dependency footprint budgetFailures must be a list")
    elif budget_failures:
        failures.append("runtime dependency footprint has budget failures: " + ", ".join(map(str, budget_failures)))
    disallowed = summary.get("disallowedPaths", [])
    if not isinstance(disallowed, list):
        failures.append("runtime dependency footprint disallowedPaths must be a list")
    elif disallowed:
        failures.append("runtime dependency footprint has disallowed paths: " + ", ".join(map(str, disallowed)))
    unexpected_present = summary.get("unexpectedPresentDependencies", [])
    if not isinstance(unexpected_present, list):
        failures.append("runtime dependency footprint unexpectedPresentDependencies must be a list")
    elif unexpected_present:
        failures.append(
            "runtime dependency footprint has unexpected dependencies: "
            + ", ".join(map(str, unexpected_present))
        )
    total_mb = summary.get("totalMb")
    if not isinstance(total_mb, (int, float)) or total_mb <= 0:
        failures.append("runtime dependency footprint summary.totalMb must be positive")

    dependencies = report.get("dependencies")
    if not isinstance(dependencies, dict):
        failures.append("runtime dependency footprint dependencies must be an object")
        dependencies = {}
    for dependency_name in details["trackedDependencies"]:
        dependency = dependencies.get(dependency_name)
        if not isinstance(dependency, dict):
            failures.append(f"runtime dependency footprint is missing dependency: {dependency_name}")
            continue
        if dependency.get("name") != dependency_name:
            failures.append(f"runtime dependency {dependency_name} name is invalid")
        expected_present = dependency.get("expectedPresent", True) is not False
        total = dependency.get("totalMb", 0)
        if expected_present and total <= 0:
            failures.append(f"runtime dependency {dependency_name} totalMb must be positive")
        if not expected_present and total > 0:
            failures.append(f"runtime dependency {dependency_name} must not be bundled")
        if dependency.get("unexpectedPresent") is True:
            failures.append(f"runtime dependency {dependency_name} is unexpectedly bundled")
        dependency_missing = dependency.get("missingRequiredPaths", [])
        if not isinstance(dependency_missing, list):
            failures.append(f"runtime dependency {dependency_name} missingRequiredPaths must be a list")
        elif dependency_missing:
            failures.append(
                f"runtime dependency {dependency_name} has missing required paths: "
                + ", ".join(map(str, dependency_missing))
            )
        paths = dependency.get("paths")
        if not isinstance(paths, list) or not paths:
            failures.append(f"runtime dependency {dependency_name} paths must be a non-empty list")
        dependency_disallowed = dependency.get("disallowedPaths", [])
        if not isinstance(dependency_disallowed, list):
            failures.append(f"runtime dependency {dependency_name} disallowedPaths must be a list")
        elif dependency_disallowed:
            failures.append(f"runtime dependency {dependency_name} has disallowed paths")

    return ReadinessCheck("runtimeDependencyFootprint", not failures, failures, details)


def validate_rust_audio_sidecar_report(
    report_path: Path | None,
    *,
    required: bool,
    min_duration_sec: float,
    require_prewarm_adoption: bool = False,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "minDurationSec": min_duration_sec,
        "requirePrewarmAdoption": require_prewarm_adoption,
    }
    if report_path is None:
        if required or require_prewarm_adoption:
            failures.append("Rust audio sidecar smoke report is required")
        return ReadinessCheck("rustAudioSidecarSmoke", not failures, failures, details)

    report = read_json_object(report_path, failures, "Rust audio sidecar smoke report")
    if not report:
        return ReadinessCheck("rustAudioSidecarSmoke", False, failures, details)

    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("Rust audio sidecar smoke report summary must be an object")
        summary = {}
    requested = report.get("requested")
    if not isinstance(requested, dict):
        failures.append("Rust audio sidecar smoke report requested must be an object")
        requested = {}
    captures = report.get("captures")
    if not isinstance(captures, list) or not captures:
        failures.append("Rust audio sidecar smoke report captures must be a non-empty list")
        captures = []

    details.update(
        {
            "apiVersion": report.get("apiVersion", ""),
            "mode": report.get("mode", ""),
            "requested": requested,
            "summary": summary,
            "captureCount": len(captures),
        }
    )
    if report.get("ok") is not True:
        failures.append("Rust audio sidecar smoke report ok must be true")
    if report.get("planOnly") is True:
        failures.append("Rust audio sidecar smoke report must not be plan-only evidence")
    if str(report.get("apiVersion") or "") != "1":
        failures.append("Rust audio sidecar smoke report apiVersion must be 1")
    if str(report.get("mode") or "") != "wasapi":
        failures.append("Rust audio sidecar smoke report mode must be wasapi for physical readiness")
    duration = requested.get("durationSec")
    prebuffer_ms = requested.get("prebufferMs", 0)
    prebuffer_required = isinstance(prebuffer_ms, (int, float)) and float(prebuffer_ms) > 0
    prewarm_adoption_required = require_prewarm_adoption or requested.get("prewarmBeforeCapture") is True
    if require_prewarm_adoption and requested.get("prewarmBeforeCapture") is not True:
        failures.append(
            "Rust audio sidecar smoke requested.prewarmBeforeCapture must be true when prewarm adoption is required"
        )
    if min_duration_sec > 0 and (
        not isinstance(duration, (int, float)) or float(duration) < min_duration_sec
    ):
        failures.append(
            f"Rust audio sidecar smoke durationSec must be at least {min_duration_sec:g}"
        )
    if summary.get("failedCaptureCount") != 0:
        failures.append("Rust audio sidecar smoke failedCaptureCount must be 0")
    if summary.get("totalFramesRead", 0) <= 0:
        failures.append("Rust audio sidecar smoke totalFramesRead must be positive")
    if prebuffer_required and summary.get("totalFramesWritten", 0) < summary.get(
        "totalFramesRead", 0
    ):
        failures.append(
            "Rust audio sidecar smoke totalFramesWritten must be at least totalFramesRead when prebufferMs is requested"
        )
    if summary.get("totalPrebufferAfterLiveCount", 0) != 0:
        failures.append("Rust audio sidecar smoke totalPrebufferAfterLiveCount must be 0")
    if prebuffer_required and summary.get("totalPrebufferFramesRead", 0) <= 0:
        failures.append(
            "Rust audio sidecar smoke totalPrebufferFramesRead must be positive when prebufferMs is requested"
        )
    if prebuffer_required and summary.get("totalPrebufferFramesWritten", 0) < summary.get(
        "totalPrebufferFramesRead", 0
    ):
        failures.append(
            "Rust audio sidecar smoke totalPrebufferFramesWritten must be at least totalPrebufferFramesRead when prebufferMs is requested"
        )
    if prebuffer_required and summary.get("totalLiveFramesWritten", 0) < summary.get(
        "totalLiveFramesRead", 0
    ):
        failures.append(
            "Rust audio sidecar smoke totalLiveFramesWritten must be at least totalLiveFramesRead when prebufferMs is requested"
        )
    if prewarm_adoption_required and summary.get("totalAdoptedPrewarmBlocks", 0) <= 0:
        failures.append(
            "Rust audio sidecar smoke totalAdoptedPrewarmBlocks must be positive when prewarmBeforeCapture is requested"
        )
    if summary.get("selectedHashVerified") is not True:
        failures.append("Rust audio sidecar smoke must verify selected native endpoint hash capture")

    captures_by_name = {
        str(capture.get("name") or ""): capture
        for capture in captures
        if isinstance(capture, dict)
    }
    default_capture = captures_by_name.get("default")
    selected_capture = captures_by_name.get("selected-native-endpoint-hash")
    if not isinstance(default_capture, dict):
        failures.append("Rust audio sidecar smoke is missing default capture")
    else:
        failures.extend(
            validate_rust_audio_capture(
                default_capture,
                "default",
                require_prebuffer=prebuffer_required,
                min_observed_duration_sec=min_duration_sec,
            )
        )
        if prewarm_adoption_required:
            failures.extend(validate_rust_audio_adopted_prewarm(default_capture, "default"))
    if not isinstance(selected_capture, dict):
        failures.append("Rust audio sidecar smoke is missing selected-native-endpoint-hash capture")
    else:
        failures.extend(
            validate_rust_audio_capture(
                selected_capture,
                "selected-native-endpoint-hash",
                expected_selection_mode="nativeEndpointHash",
                require_prebuffer=prebuffer_required,
            )
        )

    return ReadinessCheck("rustAudioSidecarSmoke", not failures, failures, details)


def validate_rust_audio_adopted_prewarm(capture: dict[str, Any], name: str) -> list[str]:
    failures: list[str] = []
    start = capture.get("start")
    if not isinstance(start, dict):
        return [f"Rust audio capture {name} start must be an object for prewarm adoption"]
    adopted = start.get("adoptedPrewarm")
    if not isinstance(adopted, dict):
        return [f"Rust audio capture {name} adoptedPrewarm must be an object"]
    if adopted.get("adopted") is not True:
        failures.append(f"Rust audio capture {name} adoptedPrewarm.adopted must be true")
    if int(adopted.get("blocks") or 0) <= 0:
        failures.append(f"Rust audio capture {name} adoptedPrewarm.blocks must be positive")
    if int(adopted.get("audioFrames") or 0) <= 0:
        failures.append(f"Rust audio capture {name} adoptedPrewarm.audioFrames must be positive")
    stop = adopted.get("stop")
    if not isinstance(stop, dict):
        failures.append(f"Rust audio capture {name} adoptedPrewarm.stop must be an object")
    elif stop.get("reason") != "adoptedIntoCapture":
        failures.append(f"Rust audio capture {name} adoptedPrewarm.stop.reason must be adoptedIntoCapture")
    return failures


def validate_rust_audio_prewarm_sidecar_report(
    report_path: Path | None,
    *,
    required: bool,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
    }
    if report_path is None:
        if required:
            failures.append("Rust audio prewarm sidecar smoke report is required")
        return ReadinessCheck("rustAudioPrewarmSidecarSmoke", not failures, failures, details)

    report = read_json_object(report_path, failures, "Rust audio prewarm sidecar smoke report")
    if not report:
        return ReadinessCheck("rustAudioPrewarmSidecarSmoke", False, failures, details)

    requested = report.get("requested")
    if not isinstance(requested, dict):
        failures.append("Rust audio prewarm sidecar smoke requested must be an object")
        requested = {}
    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("Rust audio prewarm sidecar smoke summary must be an object")
        summary = {}
    prewarm = report.get("prewarm")
    if not isinstance(prewarm, dict):
        failures.append("Rust audio prewarm sidecar smoke prewarm must be an object")
        prewarm = {}

    details.update(
        {
            "apiVersion": report.get("apiVersion", ""),
            "mode": report.get("mode", ""),
            "requested": requested,
            "summary": summary,
        }
    )
    if report.get("ok") is not True:
        failures.append("Rust audio prewarm sidecar smoke report ok must be true")
    if report.get("planOnly") is True:
        failures.append("Rust audio prewarm sidecar smoke report must not be plan-only evidence")
    if str(report.get("apiVersion") or "") != "1":
        failures.append("Rust audio prewarm sidecar smoke apiVersion must be 1")
    mode = str(report.get("mode") or "")
    if mode not in {"synthetic", "wasapi"}:
        failures.append("Rust audio prewarm sidecar smoke mode must be synthetic or wasapi")
    if prewarm.get("ok") is not True:
        failures.append("Rust audio prewarm sidecar smoke prewarm.ok must be true")
    if summary.get("prewarmOk") is not True:
        failures.append("Rust audio prewarm sidecar smoke summary.prewarmOk must be true")

    start = prewarm.get("start")
    if not isinstance(start, dict):
        failures.append("Rust audio prewarm sidecar smoke start must be an object")
        start = {}
    if mode == "synthetic":
        if start.get("syntheticPrewarm") is not True:
            failures.append("Rust audio prewarm sidecar smoke synthetic start.syntheticPrewarm must be true")
    if mode == "wasapi":
        if start.get("wasapiPrewarm") is not True:
            failures.append("Rust audio prewarm sidecar smoke WASAPI start.wasapiPrewarm must be true")
        if not str(start.get("nativeEndpointIdHash") or ""):
            failures.append("Rust audio prewarm sidecar smoke WASAPI nativeEndpointIdHash is required")
        endpoint_selection = start.get("endpointSelection")
        if not isinstance(endpoint_selection, dict):
            failures.append("Rust audio prewarm sidecar smoke WASAPI endpointSelection must be an object")
    stop = prewarm.get("stop")
    if not isinstance(stop, dict):
        failures.append("Rust audio prewarm sidecar smoke stop must be an object")
        stop = {}

    prewarm_id = str(start.get("prewarmId") or "")
    if not prewarm_id:
        failures.append("Rust audio prewarm sidecar smoke start.prewarmId is required")
    if stop.get("stopped") is not True:
        failures.append("Rust audio prewarm sidecar smoke stop.stopped must be true")
    if prewarm_id and stop.get("prewarmId") != prewarm_id:
        failures.append("Rust audio prewarm sidecar smoke stop.prewarmId must match start.prewarmId")
    if stop.get("prewarmError") not in (None, ""):
        failures.append("Rust audio prewarm sidecar smoke stop.prewarmError must be empty")

    total_blocks = numeric_field(stop, "totalBlocksObserved")
    total_frames = numeric_field(stop, "totalAudioFramesObserved")
    buffered_blocks = numeric_field(stop, "bufferedBlocks")
    buffered_frames = numeric_field(stop, "bufferedAudioFrames")
    prebuffer_target = numeric_field(start, "prebufferFrameTarget")
    prebuffer_ms = numeric_field(requested, "prebufferMs")
    require_prebuffer = prebuffer_ms is not None and prebuffer_ms > 0
    if total_blocks is None or total_blocks <= 0:
        failures.append("Rust audio prewarm sidecar smoke totalBlocksObserved must be positive")
    if total_frames is None or total_frames <= 0:
        failures.append("Rust audio prewarm sidecar smoke totalAudioFramesObserved must be positive")
    if require_prebuffer:
        if prebuffer_target is None or prebuffer_target <= 0:
            failures.append(
                "Rust audio prewarm sidecar smoke prebufferFrameTarget must be positive when prebufferMs is requested"
            )
        if buffered_blocks is None or buffered_blocks <= 0:
            failures.append(
                "Rust audio prewarm sidecar smoke bufferedBlocks must be positive when prebufferMs is requested"
            )
        if buffered_frames is None or buffered_frames <= 0:
            failures.append(
                "Rust audio prewarm sidecar smoke bufferedAudioFrames must be positive when prebufferMs is requested"
            )
        if (
            prebuffer_target is not None
            and buffered_blocks is not None
            and buffered_blocks > prebuffer_target
        ):
            failures.append("Rust audio prewarm sidecar smoke bufferedBlocks must not exceed prebufferFrameTarget")
    summary_total_blocks = numeric_field(summary, "totalBlocksObserved")
    summary_buffered_frames = numeric_field(summary, "bufferedAudioFrames")
    if summary_total_blocks is None or summary_total_blocks <= 0:
        failures.append("Rust audio prewarm sidecar smoke summary.totalBlocksObserved must be positive")
    if require_prebuffer and (summary_buffered_frames is None or summary_buffered_frames <= 0):
        failures.append(
            "Rust audio prewarm sidecar smoke summary.bufferedAudioFrames must be positive when prebufferMs is requested"
        )

    return ReadinessCheck("rustAudioPrewarmSidecarSmoke", not failures, failures, details)


def validate_rust_audio_app_prewarm_report(
    report_path: Path | None,
    *,
    required: bool,
    min_duration_sec: float = 0.0,
    min_prewarm_duration_sec: float = 0.0,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "minDurationSec": min_duration_sec,
        "minPrewarmDurationSec": min_prewarm_duration_sec,
    }
    if report_path is None:
        if required:
            failures.append("Rust audio app prewarm smoke report is required")
        return ReadinessCheck("rustAudioAppPrewarmSmoke", not failures, failures, details)

    report = read_json_object(report_path, failures, "Rust audio app prewarm smoke report")
    if not report:
        return ReadinessCheck("rustAudioAppPrewarmSmoke", False, failures, details)

    requested = report.get("requested")
    if not isinstance(requested, dict):
        failures.append("Rust audio app prewarm smoke requested must be an object")
        requested = {}
    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("Rust audio app prewarm smoke summary must be an object")
        summary = {}
    manager_start = report.get("managerStart")
    if not isinstance(manager_start, dict):
        failures.append("Rust audio app prewarm smoke managerStart must be an object")
        manager_start = {}
    manager_adoption = report.get("managerAdoption")
    if not isinstance(manager_adoption, dict):
        failures.append("Rust audio app prewarm smoke managerAdoption must be an object")
        manager_adoption = {}
    source_final = report.get("sourceFinal")
    if not isinstance(source_final, dict):
        failures.append("Rust audio app prewarm smoke sourceFinal must be an object")
        source_final = {}
    manager_resume = report.get("managerResume")
    if manager_resume is not None and not isinstance(manager_resume, dict):
        failures.append("Rust audio app prewarm smoke managerResume must be an object when present")
        manager_resume = {}

    details.update(
        {
            "apiVersion": report.get("apiVersion", ""),
            "mode": report.get("mode", ""),
            "requested": requested,
            "summary": summary,
        }
    )
    if report.get("ok") is not True:
        failures.append("Rust audio app prewarm smoke report ok must be true")
    if report.get("planOnly") is True:
        failures.append("Rust audio app prewarm smoke report must not be plan-only evidence")
    if str(report.get("apiVersion") or "") != "1":
        failures.append("Rust audio app prewarm smoke apiVersion must be 1")
    if str(report.get("mode") or "") != "wasapi":
        failures.append("Rust audio app prewarm smoke mode must be wasapi for release readiness")
    if requested.get("honorFavoriteMic") is not False:
        failures.append("Rust audio app prewarm smoke must use the stable default endpoint path")
    duration = numeric_field(requested, "durationSec")
    if min_duration_sec > 0 and (duration is None or duration < min_duration_sec):
        failures.append(
            f"Rust audio app prewarm smoke durationSec must be at least {min_duration_sec:g}"
        )
    prewarm_duration = numeric_field(requested, "prewarmDurationSec")
    if min_prewarm_duration_sec > 0 and (
        prewarm_duration is None or prewarm_duration < min_prewarm_duration_sec
    ):
        failures.append(
            f"Rust audio app prewarm smoke prewarmDurationSec must be at least {min_prewarm_duration_sec:g}"
        )
    if manager_start.get("active") is not True:
        failures.append("Rust audio app prewarm smoke managerStart.active must be true")
    if not str(manager_start.get("prewarmIdHash") or ""):
        failures.append("Rust audio app prewarm smoke managerStart.prewarmIdHash is required")
    if not str(manager_adoption.get("prewarmIdHash") or ""):
        failures.append("Rust audio app prewarm smoke managerAdoption.prewarmIdHash is required")
    if manager_resume is not None and manager_resume.get("active") is not True:
        failures.append("Rust audio app prewarm smoke managerResume.active must be true when present")

    adopted = source_final.get("adoptedPrewarm")
    if not isinstance(adopted, dict):
        failures.append("Rust audio app prewarm smoke sourceFinal.adoptedPrewarm must be an object")
        adopted = {}
    if adopted.get("adopted") is not True:
        failures.append("Rust audio app prewarm smoke adoptedPrewarm.adopted must be true")
    if numeric_field(adopted, "blocks") is None or numeric_field(adopted, "blocks") <= 0:
        failures.append("Rust audio app prewarm smoke adoptedPrewarm.blocks must be positive")
    if not str(source_final.get("nativeEndpointIdHash") or ""):
        failures.append("Rust audio app prewarm smoke nativeEndpointIdHash is required")
    if source_final.get("lastError") not in (None, ""):
        failures.append("Rust audio app prewarm smoke sourceFinal.lastError must be empty")
    if numeric_field(source_final, "callbackCount") is None or numeric_field(source_final, "callbackCount") <= 0:
        failures.append("Rust audio app prewarm smoke callbackCount must be positive")
    if (
        numeric_field(source_final, "framePipePrebufferFramesRead") is None
        or numeric_field(source_final, "framePipePrebufferFramesRead") <= 0
    ):
        failures.append("Rust audio app prewarm smoke framePipePrebufferFramesRead must be positive")
    if (
        numeric_field(source_final, "framePipeLiveFramesRead") is None
        or numeric_field(source_final, "framePipeLiveFramesRead") <= 0
    ):
        failures.append("Rust audio app prewarm smoke framePipeLiveFramesRead must be positive")
    if numeric_field(source_final, "framePipePrebufferAfterLiveCount") != 0:
        failures.append("Rust audio app prewarm smoke framePipePrebufferAfterLiveCount must be 0")
    if numeric_field(source_final, "framePipeSequenceErrorCount") != 0:
        failures.append("Rust audio app prewarm smoke framePipeSequenceErrorCount must be 0")
    if numeric_field(source_final, "framePipeProtocolErrorCount") != 0:
        failures.append("Rust audio app prewarm smoke framePipeProtocolErrorCount must be 0")

    if numeric_field(summary, "adoptedPrewarmBlocks") is None or numeric_field(summary, "adoptedPrewarmBlocks") <= 0:
        failures.append("Rust audio app prewarm smoke summary.adoptedPrewarmBlocks must be positive")
    if numeric_field(summary, "prebufferFramesRead") is None or numeric_field(summary, "prebufferFramesRead") <= 0:
        failures.append("Rust audio app prewarm smoke summary.prebufferFramesRead must be positive")
    if numeric_field(summary, "liveFramesRead") is None or numeric_field(summary, "liveFramesRead") <= 0:
        failures.append("Rust audio app prewarm smoke summary.liveFramesRead must be positive")
    if numeric_field(summary, "prebufferAfterLiveCount") != 0:
        failures.append("Rust audio app prewarm smoke summary.prebufferAfterLiveCount must be 0")
    if numeric_field(summary, "sequenceErrorCount") != 0:
        failures.append("Rust audio app prewarm smoke summary.sequenceErrorCount must be 0")
    if numeric_field(summary, "protocolErrorCount") != 0:
        failures.append("Rust audio app prewarm smoke summary.protocolErrorCount must be 0")

    return ReadinessCheck("rustAudioAppPrewarmSmoke", not failures, failures, details)


def validate_installed_live_recording_smoke_report(
    report_path: Path | None,
    *,
    required: bool,
    min_duration_sec: float = 0.0,
    require_rust_audio: bool = False,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "minDurationSec": min_duration_sec,
        "requireRustAudio": require_rust_audio,
    }
    if report_path is None:
        if required or require_rust_audio:
            failures.append("Installed live recording smoke report is required")
        return ReadinessCheck("installedLiveRecordingSmoke", not failures, failures, details)

    report = read_json_object(report_path, failures, "installed live recording smoke report")
    if not report:
        return ReadinessCheck("installedLiveRecordingSmoke", False, failures, details)

    smoke = report
    if not isinstance(smoke.get("liveRecording"), dict):
        nested_smoke = smoke.get("smoke")
        if isinstance(nested_smoke, dict):
            smoke = nested_smoke

    live_recording = smoke.get("liveRecording")
    if not isinstance(live_recording, dict):
        failures.append("installed live recording smoke liveRecording must be an object")
        live_recording = {}
    stability = live_recording.get("stability")
    if not isinstance(stability, dict):
        failures.append("installed live recording smoke liveRecording.stability must be an object")
        stability = {}

    details.update(
        {
            "appPid": smoke.get("appPid"),
            "backendPid": smoke.get("backendPid"),
            "backendPort": smoke.get("backendPort"),
            "runtimeMode": smoke.get("runtimeMode", ""),
            "apiVersion": smoke.get("apiVersion", ""),
            "ready": smoke.get("ready"),
            "launchKind": smoke.get("launchKind", ""),
            "externalAttach": smoke.get("externalAttach"),
            "cleanupVerified": smoke.get("cleanupVerified"),
            "liveRecording": live_recording,
            "stability": stability,
            "rustAudioEvidence": summarize_installed_live_recording_rust_audio_evidence(
                stability.get("samples") if isinstance(stability, dict) else None
            ),
        }
    )
    if smoke.get("ok") is not True:
        failures.append("installed live recording smoke report ok must be true")
    if str(smoke.get("runtimeMode") or "") != "tauri-supervised":
        failures.append("installed live recording smoke runtimeMode must be tauri-supervised")
    if str(smoke.get("launchKind") or "") != "managed":
        failures.append("installed live recording smoke launchKind must be managed")
    if smoke.get("externalAttach") is True:
        failures.append("installed live recording smoke must not use an external backend")
    if smoke.get("ready") is not True:
        failures.append("installed live recording smoke ready must be true")
    if str(smoke.get("apiVersion") or "") != "1":
        failures.append("installed live recording smoke apiVersion must be 1")
    app_pid = numeric_field(smoke, "appPid")
    backend_pid = numeric_field(smoke, "backendPid")
    backend_port = numeric_field(smoke, "backendPort")
    if app_pid is None or app_pid <= 0:
        failures.append("installed live recording smoke appPid must be positive")
    if backend_pid is None or backend_pid <= 0:
        failures.append("installed live recording smoke backendPid must be positive")
    if backend_port is None or backend_port <= 0:
        failures.append("installed live recording smoke backendPort must be positive")
    if app_pid is not None and backend_pid is not None and app_pid == backend_pid:
        failures.append("installed live recording smoke appPid and backendPid must differ")
    if smoke.get("cleanupVerified") is not True:
        failures.append("installed live recording smoke cleanupVerified must be true")
    if live_recording.get("verified") is not True:
        failures.append("installed live recording smoke liveRecording.verified must be true")

    duration = numeric_field(live_recording, "durationSec")
    if duration is None or duration <= 0:
        failures.append("installed live recording smoke liveRecording.durationSec must be positive")
    elif min_duration_sec > 0 and duration < min_duration_sec:
        failures.append(
            f"installed live recording smoke liveRecording.durationSec must be at least {min_duration_sec:g}"
        )

    if live_recording.get("startResponseOk") is not True:
        failures.append("installed live recording smoke startResponseOk must be true")
    started_state = str(live_recording.get("startedRecordingState") or "")
    started_listening = live_recording.get("startedListening") is True
    if started_state != "recording" and not started_listening:
        failures.append("installed live recording smoke must observe recording state after start")
    if live_recording.get("stopResponseOk") is not True:
        failures.append("installed live recording smoke stopResponseOk must be true")
    if str(live_recording.get("stoppedRecordingState") or "") != "idle":
        failures.append("installed live recording smoke stoppedRecordingState must be idle")
    if live_recording.get("stoppedListening") is not False:
        failures.append("installed live recording smoke stoppedListening must be false")
    if numeric_field(live_recording, "nonRecordingSampleCount") != 0:
        failures.append("installed live recording smoke nonRecordingSampleCount must be 0")

    if stability.get("verified") is not True:
        failures.append("installed live recording smoke stability.verified must be true")
    sample_count = numeric_field(stability, "sampleCount")
    if sample_count is None or sample_count <= 0:
        failures.append("installed live recording smoke stability.sampleCount must be positive")
    stability_duration = numeric_field(stability, "durationSec")
    if stability_duration is None or stability_duration <= 0:
        failures.append("installed live recording smoke stability.durationSec must be positive")
    elif min_duration_sec > 0 and stability_duration < min_duration_sec:
        failures.append(
            f"installed live recording smoke stability.durationSec must be at least {min_duration_sec:g}"
        )
    stability_backend_pid = numeric_field(stability, "backendPid")
    if (
        backend_pid is not None
        and stability_backend_pid is not None
        and stability_backend_pid != backend_pid
    ):
        failures.append("installed live recording smoke stability.backendPid must match backendPid")
    probe_interval = numeric_field(stability, "probeIntervalSec") or numeric_field(
        live_recording,
        "probeIntervalSec",
    )
    if (
        sample_count is not None
        and stability_duration is not None
        and probe_interval is not None
        and probe_interval > 0
    ):
        min_sample_count = max(1, int((stability_duration / probe_interval) * 0.5))
        if sample_count < min_sample_count:
            failures.append(
                "installed live recording smoke stability.sampleCount must cover at least "
                f"50% of expected probes ({sample_count:g} < {min_sample_count})"
            )

    samples = stability.get("samples")
    if not isinstance(samples, list) or not samples:
        failures.append("installed live recording smoke stability.samples must be a non-empty list")
    else:
        for index, sample in enumerate(samples, start=1):
            if not isinstance(sample, dict):
                failures.append(f"installed live recording smoke sample {index} must be an object")
                continue
            recording_state = str(sample.get("recordingState") or "")
            listening = sample.get("listening") is True
            if sample.get("healthReady") is not True:
                failures.append(f"installed live recording smoke sample {index} healthReady must be true")
                break
            sample_backend_pid = numeric_field(sample, "backendPid")
            if (
                backend_pid is not None
                and sample_backend_pid is not None
                and sample_backend_pid != backend_pid
            ):
                failures.append(f"installed live recording smoke sample {index} backendPid must match backendPid")
                break
            sample_elapsed = numeric_field(sample, "elapsedSec")
            if sample_elapsed is None or sample_elapsed < 0:
                failures.append(f"installed live recording smoke sample {index} elapsedSec must be non-negative")
                break
            if recording_state != "recording" and not listening:
                failures.append(
                    f"installed live recording smoke sample {index} must remain in recording or listening state"
                )
                break
        if stability_duration is not None and samples:
            last_sample = samples[-1]
            last_elapsed = numeric_field(last_sample, "elapsedSec") if isinstance(last_sample, dict) else None
            if last_elapsed is None or last_elapsed < stability_duration * 0.75:
                failures.append("installed live recording smoke samples must span at least 75% of stability.durationSec")
        if require_rust_audio:
            failures.extend(validate_installed_live_recording_rust_audio_samples(samples))

    return ReadinessCheck("installedLiveRecordingSmoke", not failures, failures, details)


def summarize_installed_live_recording_rust_audio_evidence(samples: Any) -> dict[str, Any]:
    if not isinstance(samples, list):
        return {
            "sampleCount": 0,
            "audioDiagnosticsSampleCount": 0,
            "rustFramePipeSampleCount": 0,
            "fallbackCircuitOpenCount": 0,
        }

    audio_diagnostics_count = 0
    rust_frame_pipe_count = 0
    fallback_open_count = 0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        diagnostics = sample.get("audioDiagnostics")
        if not isinstance(diagnostics, dict):
            continue
        audio_diagnostics_count += 1
        feature_flags = diagnostics.get("featureFlags")
        active_capture = diagnostics.get("activeCapture")
        fallback_circuit = diagnostics.get("rustAudioFallbackCircuit")
        if (
            isinstance(feature_flags, dict)
            and isinstance(active_capture, dict)
            and feature_flags.get("audioEngine") == RUST_AUDIO_ENGINE
            and active_capture.get("engine") == RUST_AUDIO_ENGINE
            and active_capture.get("frameSource") == RUST_AUDIO_FRAME_SOURCE
        ):
            rust_frame_pipe_count += 1
        if isinstance(fallback_circuit, dict) and fallback_circuit.get("open") is True:
            fallback_open_count += 1

    return {
        "sampleCount": len(samples),
        "audioDiagnosticsSampleCount": audio_diagnostics_count,
        "rustFramePipeSampleCount": rust_frame_pipe_count,
        "fallbackCircuitOpenCount": fallback_open_count,
    }


def validate_installed_live_recording_rust_audio_samples(samples: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(samples, list) or not samples:
        return ["installed live recording smoke Rust audio evidence requires stability samples"]

    for index, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            failures.append(f"installed live recording smoke sample {index} must be an object")
            break
        diagnostics = sample.get("audioDiagnostics")
        if not isinstance(diagnostics, dict):
            failures.append(f"installed live recording smoke sample {index} audioDiagnostics must be an object")
            break
        feature_flags = diagnostics.get("featureFlags")
        if not isinstance(feature_flags, dict):
            failures.append(f"installed live recording smoke sample {index} audioDiagnostics.featureFlags must be an object")
            break
        if feature_flags.get("audioEngine") != RUST_AUDIO_ENGINE:
            failures.append(
                f"installed live recording smoke sample {index} audioEngine must be {RUST_AUDIO_ENGINE}"
            )
            break
        if feature_flags.get("rustAudioRequested") is not True:
            failures.append(f"installed live recording smoke sample {index} rustAudioRequested must be true")
            break
        if feature_flags.get("rustAudioAvailable") is not True:
            failures.append(f"installed live recording smoke sample {index} rustAudioAvailable must be true")
            break

        active_capture = diagnostics.get("activeCapture")
        if not isinstance(active_capture, dict):
            failures.append(f"installed live recording smoke sample {index} activeCapture must be an object")
            break
        if active_capture.get("running") is not True:
            failures.append(f"installed live recording smoke sample {index} activeCapture.running must be true")
            break
        if active_capture.get("engine") != RUST_AUDIO_ENGINE:
            failures.append(
                f"installed live recording smoke sample {index} activeCapture.engine must be {RUST_AUDIO_ENGINE}"
            )
            break
        if active_capture.get("frameSource") != RUST_AUDIO_FRAME_SOURCE:
            failures.append(
                f"installed live recording smoke sample {index} activeCapture.frameSource must be {RUST_AUDIO_FRAME_SOURCE}"
            )
            break
        if active_capture.get("streamActive") is not True:
            failures.append(f"installed live recording smoke sample {index} activeCapture.streamActive must be true")
            break
        if numeric_field(active_capture, "callbackCount") is None or numeric_field(active_capture, "callbackCount") <= 0:
            failures.append(f"installed live recording smoke sample {index} activeCapture.callbackCount must be positive")
            break
        if numeric_field(active_capture, "framePipeFramesRead") is None or numeric_field(active_capture, "framePipeFramesRead") <= 0:
            failures.append(f"installed live recording smoke sample {index} framePipeFramesRead must be positive")
            break
        if numeric_field(active_capture, "framePipeAudioFramesRead") is None or numeric_field(active_capture, "framePipeAudioFramesRead") <= 0:
            failures.append(f"installed live recording smoke sample {index} framePipeAudioFramesRead must be positive")
            break
        if not str(active_capture.get("nativeEndpointIdHash") or active_capture.get("sourceNativeEndpointIdHash") or ""):
            failures.append(f"installed live recording smoke sample {index} nativeEndpointIdHash is required")
            break
        if numeric_field(active_capture, "framePipeSequenceErrorCount") != 0:
            failures.append(
                f"installed live recording smoke sample {index} framePipeSequenceErrorCount must be 0"
            )
            break
        if numeric_field(active_capture, "framePipeProtocolErrorCount") != 0:
            failures.append(
                f"installed live recording smoke sample {index} framePipeProtocolErrorCount must be 0"
            )
            break
        if numeric_field(active_capture, "framePipePrebufferAfterLiveCount") != 0:
            failures.append(
                f"installed live recording smoke sample {index} framePipePrebufferAfterLiveCount must be 0"
            )
            break

        fallback_circuit = diagnostics.get("rustAudioFallbackCircuit")
        if not isinstance(fallback_circuit, dict):
            failures.append(f"installed live recording smoke sample {index} rustAudioFallbackCircuit must be an object")
            break
        if fallback_circuit.get("available") is not True:
            failures.append(f"installed live recording smoke sample {index} rustAudioFallbackCircuit.available must be true")
            break
        if fallback_circuit.get("open") is True:
            failures.append(f"installed live recording smoke sample {index} rustAudioFallbackCircuit.open must be false")
            break

    return failures


def validate_tauri_text_injection_smoke_report(
    report_path: Path | None,
    *,
    required: bool,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
    }
    if report_path is None:
        if required:
            failures.append("Tauri text injection smoke report is required")
        return ReadinessCheck("tauriTextInjectionSmoke", not failures, failures, details)

    report = read_json_object(report_path, failures, "Tauri text injection smoke report")
    if not report:
        return ReadinessCheck("tauriTextInjectionSmoke", False, failures, details)

    details.update(validate_tauri_text_injection_payload(report, failures, "Tauri text injection smoke"))
    return ReadinessCheck("tauriTextInjectionSmoke", not failures, failures, details)


def validate_tauri_text_injection_payload(
    report: dict[str, Any],
    failures: list[str],
    label: str,
) -> dict[str, Any]:
    shell_ipc = report.get("shellIpc")
    if not isinstance(shell_ipc, dict):
        failures.append(f"{label} shellIpc must be an object")
        shell_ipc = {}
    last_response = shell_ipc.get("lastResponse")
    if not isinstance(last_response, dict):
        failures.append(f"{label} shellIpc.lastResponse must be an object")
        last_response = {}
    response_payload = last_response.get("payload")
    if not isinstance(response_payload, dict):
        failures.append(f"{label} shellIpc.lastResponse.payload must be an object")
        response_payload = {}

    if report.get("ok") is not True:
        failures.append(f"{label} report ok must be true")
    if report.get("validateOnly") is True:
        failures.append(f"{label} report must not be validate-only evidence")
    if str(report.get("schemaVersion") or "") != "1":
        failures.append(f"{label} schemaVersion must be 1")
    if str(report.get("method") or "") != "tauri":
        failures.append(f"{label} method must be tauri")
    if str(report.get("status") or "") != "passed":
        failures.append(f"{label} status must be passed")
    if report.get("callbackVerified") is not True:
        failures.append(f"{label} callbackVerified must be true")
    if report.get("targetTextVerified") is not True:
        failures.append(f"{label} targetTextVerified must be true")
    if str(report.get("targetError") or ""):
        failures.append(f"{label} targetError must be empty")
    if numeric_field(report, "expectedChars") is None or numeric_field(report, "expectedChars") <= 0:
        failures.append(f"{label} expectedChars must be positive")
    if numeric_field(report, "callbackElapsedMs") is None or numeric_field(report, "callbackElapsedMs") < 0:
        failures.append(f"{label} callbackElapsedMs must be non-negative")
    if numeric_field(report, "targetTextElapsedMs") is None or numeric_field(report, "targetTextElapsedMs") < 0:
        failures.append(f"{label} targetTextElapsedMs must be non-negative")

    target_focus = report.get("targetFocus")
    if not isinstance(target_focus, dict):
        failures.append(f"{label} targetFocus must be an object")
        target_focus = {}
    if target_focus.get("attempted") is True and target_focus.get("ok") is not True:
        failures.append(f"{label} targetFocus.ok must be true when focus was attempted")

    if shell_ipc.get("available") is not True:
        failures.append(f"{label} shellIpc.available must be true")
    if shell_ipc.get("lastCommand") != "injectText":
        failures.append(f"{label} shellIpc.lastCommand must be injectText")
    if shell_ipc.get("lastSuccess") is not True:
        failures.append(f"{label} shellIpc.lastSuccess must be true")
    if shell_ipc.get("lastErrorCode") not in (None, ""):
        failures.append(f"{label} shellIpc.lastErrorCode must be empty")
    if last_response.get("success") is not True:
        failures.append(f"{label} shellIpc.lastResponse.success must be true")

    if response_payload.get("method") != "tauri":
        failures.append(f"{label} shellIpc response method must be tauri")
    markers = response_payload.get("markers")
    if not isinstance(markers, list) or "clipboard_set" not in markers or "paste" not in markers:
        failures.append(f"{label} response markers must include clipboard_set and paste")
    timings = response_payload.get("timingsMs")
    if not isinstance(timings, dict):
        failures.append(f"{label} response timingsMs must be an object")
    else:
        for key in ("clipboardSet", "pasteDispatch", "total"):
            value = timings.get(key)
            if value is None or not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                failures.append(f"{label} timing {key} must be non-negative")

    return {
        "schemaVersion": report.get("schemaVersion"),
        "method": report.get("method", ""),
        "status": report.get("status", ""),
        "callbackElapsedMs": report.get("callbackElapsedMs"),
        "targetTextElapsedMs": report.get("targetTextElapsedMs"),
        "shellIpc": shell_ipc,
    }


def validate_tauri_text_injection_matrix_report(
    report_path: Path | None,
    *,
    required: bool,
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "requiredScenarioIds": list(REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
        "optionalScenarioIds": list(OPTIONAL_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS),
    }
    if report_path is None:
        if required:
            failures.append("Tauri text injection matrix report is required")
        return ReadinessCheck("tauriTextInjectionMatrix", not failures, failures, details)

    report = read_json_object(report_path, failures, "Tauri text injection matrix report")
    if not report:
        return ReadinessCheck("tauriTextInjectionMatrix", False, failures, details)

    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        failures.append("Tauri text injection matrix scenarios must be a non-empty list")
        scenarios = []

    details.update(
        {
            "schemaVersion": report.get("schemaVersion"),
            "method": report.get("method", ""),
            "scenarioCount": len(scenarios),
            "ok": report.get("ok"),
        }
    )
    if report.get("ok") is not True:
        failures.append("Tauri text injection matrix report ok must be true")
    if report.get("validateOnly") is True:
        failures.append("Tauri text injection matrix report must not be validate-only evidence")
    if str(report.get("schemaVersion") or "") != "1":
        failures.append("Tauri text injection matrix schemaVersion must be 1")
    if str(report.get("method") or "") != "tauri":
        failures.append("Tauri text injection matrix method must be tauri")

    covered_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    valid_count = 0
    unsupported: dict[str, str] = {}
    for index, scenario in enumerate(scenarios, start=1):
        if not isinstance(scenario, dict):
            failures.append(f"Tauri text injection matrix scenario {index} must be an object")
            continue
        scenario_id = str(scenario.get("id") or scenario.get("scenario") or "").strip()
        if not scenario_id:
            failures.append(f"Tauri text injection matrix scenario {index} id is required")
            continue
        if scenario_id in covered_ids:
            duplicate_ids.add(scenario_id)
            failures.append(f"Tauri text injection matrix scenario {scenario_id} is duplicated")
            continue
        covered_ids.add(scenario_id)

        if scenario.get("unsupported") is True:
            reason = str(scenario.get("unsupportedReason") or "").strip()
            if scenario_id in REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS:
                failures.append(
                    f"Tauri text injection matrix required scenario {scenario_id} cannot be marked unsupported"
                )
            elif not reason:
                failures.append(
                    f"Tauri text injection matrix optional scenario {scenario_id} unsupportedReason is required"
                )
            else:
                unsupported[scenario_id] = reason
            continue

        scenario_report = scenario.get("report")
        if not isinstance(scenario_report, dict):
            scenario_report = scenario
        before = len(failures)
        validate_tauri_text_injection_payload(
            scenario_report,
            failures,
            f"Tauri text injection matrix scenario {scenario_id}",
        )
        if len(failures) == before:
            valid_count += 1

    missing = [
        scenario_id
        for scenario_id in REQUIRED_TAURI_TEXT_INJECTION_MATRIX_SCENARIOS
        if scenario_id not in covered_ids
    ]
    if missing:
        failures.append(
            "Tauri text injection matrix is missing required scenario(s): " + ", ".join(missing)
        )

    details.update(
        {
            "coveredScenarioIds": sorted(covered_ids),
            "duplicateScenarioIds": sorted(duplicate_ids),
            "validScenarioCount": valid_count,
            "unsupportedOptionalScenarios": unsupported,
        }
    )
    return ReadinessCheck("tauriTextInjectionMatrix", not failures, failures, details)


def validate_recording_hot_path_comparison_report(
    report_path: Path | None,
    *,
    required: bool,
) -> ReadinessCheck:
    min_samples_per_report = 3
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "minSamplesPerReport": min_samples_per_report,
    }
    if report_path is None:
        if required:
            failures.append("Recording hot-path Python/Rust comparison report is required")
        return ReadinessCheck("recordingHotPathPythonRustComparison", not failures, failures, details)

    report = read_json_object(report_path, failures, "recording hot-path Python/Rust comparison report")
    if not report:
        return ReadinessCheck("recordingHotPathPythonRustComparison", False, failures, details)

    reports = report.get("reports")
    if not isinstance(reports, dict):
        failures.append("recording hot-path comparison reports must be an object")
        reports = {}
    summary = report.get("summary")
    if not isinstance(summary, dict):
        failures.append("recording hot-path comparison summary must be an object")
        summary = {}
    checks = report.get("checks")
    if not isinstance(checks, list):
        failures.append("recording hot-path comparison checks must be a list")
        checks = []
    checks_by_name = {
        str(check.get("name") or ""): check
        for check in checks
        if isinstance(check, dict)
    }

    details.update(
        {
            "schemaVersion": report.get("schemaVersion"),
            "reports": reports,
            "summary": summary,
            "checkCount": len(checks),
        }
    )
    if report.get("ok") is not True:
        failures.append("recording hot-path comparison report ok must be true")
    if str(report.get("schemaVersion") or "") != "1":
        failures.append("recording hot-path comparison schemaVersion must be 1")
    if report.get("failures"):
        failures.append("recording hot-path comparison failures must be empty")

    python_report = reports.get("python") if isinstance(reports, dict) else {}
    rust_report = reports.get("rust") if isinstance(reports, dict) else {}
    if not isinstance(python_report, dict) or python_report.get("audioEngine") != "python":
        failures.append("recording hot-path comparison Python report must use audioEngine=python")
    if not isinstance(rust_report, dict) or rust_report.get("audioEngine") != "rust-prototype":
        failures.append("recording hot-path comparison Rust report must use audioEngine=rust-prototype")
    python_samples = numeric_field(python_report, "samples") if isinstance(python_report, dict) else None
    rust_samples = numeric_field(rust_report, "samples") if isinstance(rust_report, dict) else None
    if python_samples is None or python_samples < min_samples_per_report:
        failures.append(
            f"recording hot-path comparison Python report must include at least {min_samples_per_report} samples"
        )
    if rust_samples is None or rust_samples < min_samples_per_report:
        failures.append(
            f"recording hot-path comparison Rust report must include at least {min_samples_per_report} samples"
        )

    for check_name in (
        "physicalReports",
        "sampleCount",
        "providerTranscript",
        "sameProvider",
        "rustAudioEngine",
        "rustFallbackCircuitClosed",
        "audioOwnedLatencyNoRegression",
        "pythonAudioEngine",
    ):
        check = checks_by_name.get(check_name)
        if not isinstance(check, dict):
            failures.append(f"recording hot-path comparison is missing check: {check_name}")
        elif check.get("ok") is not True:
            failures.append(f"recording hot-path comparison check failed: {check_name}")

    provider_transcript = summary.get("providerTranscript")
    if not isinstance(provider_transcript, dict) or provider_transcript.get("complete") is not True:
        failures.append("recording hot-path comparison providerTranscript segment must be complete")
    hotkey_to_first_audio = summary.get("hotkeyToFirstAudioFrame")
    if not isinstance(hotkey_to_first_audio, dict) or hotkey_to_first_audio.get("complete") is not True:
        failures.append("recording hot-path comparison hotkeyToFirstAudioFrame segment must be complete")
    stop_to_text = summary.get("stopToTextInjection")
    if not isinstance(stop_to_text, dict) or stop_to_text.get("complete") is not True:
        failures.append("recording hot-path comparison stopToTextInjection segment must be complete")

    complete_segments = summary.get("completeSegmentCount")
    if not isinstance(complete_segments, int) or complete_segments < 3:
        failures.append("recording hot-path comparison must include at least three complete compared segments")

    return ReadinessCheck("recordingHotPathPythonRustComparison", not failures, failures, details)


def validate_rust_audio_capture(
    capture: dict[str, Any],
    name: str,
    *,
    expected_selection_mode: str = "",
    require_prebuffer: bool = False,
    min_observed_duration_sec: float = 0.0,
) -> list[str]:
    failures: list[str] = []
    if capture.get("ok") is not True:
        failures.append(f"Rust audio capture {name} ok must be true")
    frames = capture.get("frames")
    if not isinstance(frames, dict):
        failures.append(f"Rust audio capture {name} frames must be an object")
        frames = {}
    if frames.get("framesRead", 0) <= 0:
        failures.append(f"Rust audio capture {name} framesRead must be positive")
    if frames.get("sequenceGapCount") != 0:
        failures.append(f"Rust audio capture {name} sequenceGapCount must be 0")
    if frames.get("prebufferAfterLiveCount", 0) != 0:
        failures.append(f"Rust audio capture {name} prebufferAfterLiveCount must be 0")
    if require_prebuffer:
        if frames.get("prebufferFramesRead", 0) <= 0:
            failures.append(
                f"Rust audio capture {name} prebufferFramesRead must be positive when prebufferMs is requested"
            )
        if frames.get("liveFramesRead", 0) <= 0:
            failures.append(
                f"Rust audio capture {name} liveFramesRead must be positive when prebufferMs is requested"
            )
        first_live_frame_ms = frames.get("firstLiveFrameReadMs")
        if not isinstance(first_live_frame_ms, (int, float)) or first_live_frame_ms < 0:
            failures.append(
                f"Rust audio capture {name} firstLiveFrameReadMs must be non-negative when prebufferMs is requested"
            )
        first_live_sequence = frames.get("firstLiveSequence")
        if not isinstance(first_live_sequence, int) or first_live_sequence < 0:
            failures.append(
                f"Rust audio capture {name} firstLiveSequence must be non-negative when prebufferMs is requested"
            )
    first_frame_ms = frames.get("firstFrameReadMs")
    if not isinstance(first_frame_ms, (int, float)) or first_frame_ms < 0:
        failures.append(f"Rust audio capture {name} firstFrameReadMs must be non-negative")
    observed_duration = frames.get("observedDurationSec")
    if min_observed_duration_sec > 0 and (
        not isinstance(observed_duration, (int, float))
        or float(observed_duration) < min_observed_duration_sec
    ):
        failures.append(
            f"Rust audio capture {name} observedDurationSec must be at least {min_observed_duration_sec:g}"
        )

    stop = capture.get("stop")
    if not isinstance(stop, dict):
        failures.append(f"Rust audio capture {name} stop must be an object")
        stop = {}
    if stop.get("stopped") is not True:
        failures.append(f"Rust audio capture {name} stop.stopped must be true")
    if stop.get("connected") is not True:
        failures.append(f"Rust audio capture {name} stop.connected must be true")
    if stop.get("framesWritten", 0) <= 0:
        failures.append(f"Rust audio capture {name} stop.framesWritten must be positive")
    if require_prebuffer and stop.get("framesWritten", 0) < frames.get("framesRead", 0):
        failures.append(
            f"Rust audio capture {name} stop.framesWritten must be at least framesRead when prebufferMs is requested"
        )
    if require_prebuffer:
        if stop.get("prebufferFramesWritten", 0) <= 0:
            failures.append(
                f"Rust audio capture {name} stop.prebufferFramesWritten must be positive when prebufferMs is requested"
            )
        if stop.get("prebufferFramesWritten", 0) < frames.get("prebufferFramesRead", 0):
            failures.append(
                f"Rust audio capture {name} stop.prebufferFramesWritten must be at least prebufferFramesRead when prebufferMs is requested"
            )
        if stop.get("liveFramesWritten", 0) <= 0:
            failures.append(
                f"Rust audio capture {name} stop.liveFramesWritten must be positive when prebufferMs is requested"
            )
        if stop.get("liveFramesWritten", 0) < frames.get("liveFramesRead", 0):
            failures.append(
                f"Rust audio capture {name} stop.liveFramesWritten must be at least liveFramesRead when prebufferMs is requested"
            )
    if stop.get("writerError") not in (None, ""):
        failures.append(f"Rust audio capture {name} writerError must be empty")

    start = capture.get("start")
    if not isinstance(start, dict):
        failures.append(f"Rust audio capture {name} start must be an object")
        start = {}
    if not str(start.get("nativeEndpointIdHash") or ""):
        failures.append(f"Rust audio capture {name} nativeEndpointIdHash is required")
    if expected_selection_mode:
        selection = start.get("endpointSelection")
        if not isinstance(selection, dict):
            failures.append(f"Rust audio capture {name} endpointSelection must be an object")
        elif selection.get("mode") != expected_selection_mode:
            failures.append(
                f"Rust audio capture {name} endpointSelection.mode must be {expected_selection_mode}"
            )
    return failures


def validate_updater_publication_report(report_path: Path | None, *, metadata_path: Path) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "metadata": str(metadata_path),
    }
    if report_path is None:
        failures.append("published updater evidence report is required")
        return ReadinessCheck("publishedUpdaterManifest", False, failures, details)

    report = read_json_object(report_path, failures, "updater publication report")
    if not report:
        return ReadinessCheck("publishedUpdaterManifest", False, failures, details)

    details.update(
        {
            "url": report.get("url", ""),
            "finalUrl": report.get("finalUrl", ""),
            "statusCode": report.get("statusCode", None),
            "metadataSha256": report.get("metadataSha256", ""),
        }
    )
    if report.get("ok") is not True:
        failures.append("updater publication report ok must be true")
    if report.get("statusCode") != 200:
        failures.append("updater publication report statusCode must be 200")
    url = str(report.get("url") or "")
    if not is_https_url(url):
        failures.append("updater publication report url must be absolute HTTPS")
    final_url = str(report.get("finalUrl") or "")
    if not is_https_url(final_url):
        failures.append("updater publication report finalUrl must be absolute HTTPS")
    if report.get("requireSignatures") is not True:
        failures.append("updater publication report must record requireSignatures=true")

    reported_sha = str(report.get("metadataSha256") or "").lower()
    if not SHA256_RE.match(reported_sha):
        failures.append("updater publication report metadataSha256 must be a SHA256 hex digest")
    elif metadata_path.is_file():
        actual_sha = sha256_file(metadata_path).lower()
        details["localMetadataSha256"] = actual_sha
        if reported_sha != actual_sha:
            failures.append("published metadataSha256 does not match local latest.json")

    return ReadinessCheck("publishedUpdaterManifest", not failures, failures, details)


def validate_authenticode_report(
    report_path: Path | None,
    *,
    expected_publisher: str,
    require_timestamp: bool,
    expected_artifact_names: list[str] | None = None,
) -> ReadinessCheck:
    expected_artifact_names = expected_artifact_names or []
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "expectedPublisher": expected_publisher,
        "requireTimestamp": require_timestamp,
        "expectedArtifactNames": expected_artifact_names,
    }
    if report_path is None:
        failures.append("Authenticode validation report is required")
        return ReadinessCheck("authenticodeSignatures", False, failures, details)

    report = read_json_object(report_path, failures, "Authenticode report")
    if not report:
        return ReadinessCheck("authenticodeSignatures", False, failures, details)

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        failures.append("Authenticode report must include at least one artifact")
        artifacts = []
    details["artifactCount"] = len(artifacts)
    if report.get("ok") is not True:
        failures.append("Authenticode report ok must be true")
    if isinstance(report.get("count"), int) and report["count"] != len(artifacts):
        failures.append("Authenticode report count must match artifacts length")
    if not expected_artifact_names:
        failures.append("latest.json must list at least one release artifact name for Authenticode linkage")

    reported_artifact_names: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            failures.append(f"Authenticode artifacts[{index}] must be an object")
            continue
        artifact_path = str(artifact.get("path") or "")
        artifact_name = Path(artifact_path).name
        if not artifact_name:
            failures.append(f"Authenticode artifacts[{index}].path is required")
        else:
            reported_artifact_names.add(artifact_name.casefold())
        status = artifact.get("status")
        if status != "Valid":
            failures.append(f"Authenticode artifacts[{index}].status must be Valid")
        signer_subject = str(artifact.get("signerSubject") or "")
        if expected_publisher and expected_publisher not in signer_subject:
            failures.append(f"Authenticode artifacts[{index}].signerSubject must contain expected publisher")
        timestamp_subject = str(artifact.get("timestampSubject") or "")
        if require_timestamp and not timestamp_subject:
            failures.append(f"Authenticode artifacts[{index}].timestampSubject is required")

    missing_expected = [
        name
        for name in expected_artifact_names
        if name.casefold() not in reported_artifact_names
    ]
    if missing_expected:
        failures.append(
            "Authenticode report is missing release artifact(s): " + ", ".join(missing_expected)
        )

    return ReadinessCheck("authenticodeSignatures", not failures, failures, details)


def numeric_field(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def read_json_object(path: Path, failures: list[str], label: str) -> dict[str, Any]:
    try:
        if not path.is_file():
            raise FileNotFoundError(f"{label} was not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(str(exc))
        return {}
    if not isinstance(payload, dict):
        failures.append(f"{label} root must be a JSON object")
        return {}
    return payload


def is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def read_updater_artifact_names(metadata_path: Path) -> list[str]:
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    names: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        name = artifact.get("name")
        if isinstance(name, str) and name.strip() and Path(name).name == name:
            names.append(name.strip())
    return names


def parse_optional_path(raw: str) -> Path | None:
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def write_output(payload: dict[str, Any], path: str) -> None:
    if not path:
        return
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate final Scriber hybrid architecture release-readiness evidence.",
    )
    parser.add_argument("--hardware-input-dir", default="tmp/hybrid-baseline")
    parser.add_argument("--updater-metadata", default=str(DEFAULT_METADATA))
    parser.add_argument("--updater-artifact-dir", default="")
    parser.add_argument("--sha256sums", default="")
    parser.add_argument("--media-preparation-report", default="")
    parser.add_argument("--runtime-dependency-footprint-report", default="")
    parser.add_argument("--updater-publication-report", default="")
    parser.add_argument("--authenticode-report", default="")
    parser.add_argument("--rust-audio-sidecar-report", default="")
    parser.add_argument("--rust-audio-prewarm-sidecar-report", default="")
    parser.add_argument("--rust-audio-app-prewarm-report", default="")
    parser.add_argument("--installed-live-recording-smoke-report", default="")
    parser.add_argument("--tauri-text-injection-smoke-report", default="")
    parser.add_argument("--tauri-text-injection-matrix-report", default="")
    parser.add_argument("--recording-hot-path-comparison-report", default="")
    parser.add_argument("--require-rust-audio-sidecar-smoke", action="store_true")
    parser.add_argument("--require-rust-audio-prewarm-sidecar-smoke", action="store_true")
    parser.add_argument("--require-rust-audio-app-prewarm-smoke", action="store_true")
    parser.add_argument("--require-installed-live-recording-smoke", action="store_true")
    parser.add_argument("--require-installed-live-recording-rust-audio", action="store_true")
    parser.add_argument("--require-tauri-text-injection-smoke", action="store_true")
    parser.add_argument("--require-tauri-text-injection-matrix", action="store_true")
    parser.add_argument("--require-recording-hot-path-comparison", action="store_true")
    parser.add_argument("--require-rust-endpoint-inventory", action="store_true")
    parser.add_argument("--require-device-refresh-evidence", action="store_true")
    parser.add_argument("--require-rust-audio-sidecar-prewarm-adoption", action="store_true")
    parser.add_argument("--min-rust-audio-duration-sec", type=float, default=0.0)
    parser.add_argument("--min-rust-audio-app-prewarm-duration-sec", type=float, default=0.0)
    parser.add_argument("--min-rust-audio-app-prewarm-prewarm-duration-sec", type=float, default=0.0)
    parser.add_argument("--min-installed-live-recording-duration-sec", type=float, default=0.0)
    parser.add_argument("--expected-authenticode-publisher", default="")
    parser.add_argument("--require-authenticode-timestamp", action="store_true")
    parser.add_argument("--platform", default="windows-x86_64")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    payload = validate_release_readiness(
        hardware_input_dir=Path(args.hardware_input_dir).expanduser().resolve(),
        updater_metadata=Path(args.updater_metadata).expanduser().resolve(),
        updater_artifact_dir=parse_optional_path(args.updater_artifact_dir),
        sha256sums=parse_optional_path(args.sha256sums),
        media_preparation_report=parse_optional_path(args.media_preparation_report),
        runtime_dependency_footprint_report=parse_optional_path(args.runtime_dependency_footprint_report),
        updater_publication_report=parse_optional_path(args.updater_publication_report),
        authenticode_report=parse_optional_path(args.authenticode_report),
        rust_audio_sidecar_report=parse_optional_path(args.rust_audio_sidecar_report),
        rust_audio_prewarm_sidecar_report=parse_optional_path(args.rust_audio_prewarm_sidecar_report),
        rust_audio_app_prewarm_report=parse_optional_path(args.rust_audio_app_prewarm_report),
        installed_live_recording_smoke_report=parse_optional_path(args.installed_live_recording_smoke_report),
        tauri_text_injection_smoke_report=parse_optional_path(args.tauri_text_injection_smoke_report),
        tauri_text_injection_matrix_report=parse_optional_path(args.tauri_text_injection_matrix_report),
        recording_hot_path_comparison_report=parse_optional_path(args.recording_hot_path_comparison_report),
        require_rust_audio_sidecar_smoke=args.require_rust_audio_sidecar_smoke,
        require_rust_audio_prewarm_sidecar_smoke=args.require_rust_audio_prewarm_sidecar_smoke,
        require_rust_audio_app_prewarm_smoke=args.require_rust_audio_app_prewarm_smoke,
        require_installed_live_recording_smoke=args.require_installed_live_recording_smoke,
        require_installed_live_recording_rust_audio=args.require_installed_live_recording_rust_audio,
        require_tauri_text_injection_smoke=args.require_tauri_text_injection_smoke,
        require_tauri_text_injection_matrix=args.require_tauri_text_injection_matrix,
        require_recording_hot_path_comparison=args.require_recording_hot_path_comparison,
        require_rust_endpoint_inventory=args.require_rust_endpoint_inventory,
        require_device_refresh_evidence=args.require_device_refresh_evidence,
        require_rust_audio_sidecar_prewarm_adoption=args.require_rust_audio_sidecar_prewarm_adoption,
        min_rust_audio_duration_sec=args.min_rust_audio_duration_sec,
        min_rust_audio_app_prewarm_duration_sec=args.min_rust_audio_app_prewarm_duration_sec,
        min_rust_audio_app_prewarm_prewarm_duration_sec=args.min_rust_audio_app_prewarm_prewarm_duration_sec,
        min_installed_live_recording_duration_sec=args.min_installed_live_recording_duration_sec,
        expected_authenticode_publisher=args.expected_authenticode_publisher,
        require_authenticode_timestamp=args.require_authenticode_timestamp,
        platform=args.platform,
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

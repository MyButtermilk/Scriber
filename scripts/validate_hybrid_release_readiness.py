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
    require_rust_audio_sidecar_smoke: bool = False,
    require_rust_audio_prewarm_sidecar_smoke: bool = False,
    min_rust_audio_duration_sec: float = 0.0,
    expected_authenticode_publisher: str = "",
    require_authenticode_timestamp: bool = False,
    platform: str = "windows-x86_64",
) -> dict[str, Any]:
    expected_signed_artifact_names = read_updater_artifact_names(updater_metadata)
    checks = [
        validate_physical_microphone_matrix(hardware_input_dir),
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
    if require_rust_audio_sidecar_smoke or rust_audio_sidecar_report is not None:
        checks.append(
            validate_rust_audio_sidecar_report(
                rust_audio_sidecar_report,
                required=require_rust_audio_sidecar_smoke,
                min_duration_sec=min_rust_audio_duration_sec,
            )
        )
    if require_rust_audio_prewarm_sidecar_smoke or rust_audio_prewarm_sidecar_report is not None:
        checks.append(
            validate_rust_audio_prewarm_sidecar_report(
                rust_audio_prewarm_sidecar_report,
                required=require_rust_audio_prewarm_sidecar_smoke,
            )
        )
    return {
        "ok": all(check.ok for check in checks),
        "checks": [check.to_public() for check in checks],
    }


def validate_physical_microphone_matrix(input_dir: Path) -> ReadinessCheck:
    payload = validate_microphone_matrix(input_dir=input_dir)
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
) -> ReadinessCheck:
    failures: list[str] = []
    details: dict[str, Any] = {
        "report": str(report_path) if report_path else "",
        "required": required,
        "minDurationSec": min_duration_sec,
    }
    if report_path is None:
        if required:
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
    parser.add_argument("--require-rust-audio-sidecar-smoke", action="store_true")
    parser.add_argument("--require-rust-audio-prewarm-sidecar-smoke", action="store_true")
    parser.add_argument("--min-rust-audio-duration-sec", type=float, default=0.0)
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
        require_rust_audio_sidecar_smoke=args.require_rust_audio_sidecar_smoke,
        require_rust_audio_prewarm_sidecar_smoke=args.require_rust_audio_prewarm_sidecar_smoke,
        min_rust_audio_duration_sec=args.min_rust_audio_duration_sec,
        expected_authenticode_publisher=args.expected_authenticode_publisher,
        require_authenticode_timestamp=args.require_authenticode_timestamp,
        platform=args.platform,
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

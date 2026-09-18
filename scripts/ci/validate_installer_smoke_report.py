"""Require complete installed-release smoke evidence before publication.

This reads only the existing report and installer path. It neither imports the
application nor repeats downloads, installation, or runtime checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MEDIA_CHECK_NAMES = frozenset(
    {
        "file_upload_compression",
        "video_upload_audio_extraction",
        "youtube_post_download_normalization",
        "azure_mai_audio_preparation",
        "ffprobe_duration_probe",
    }
)


class ReportValidationError(ValueError):
    """The existing report does not prove the required installed-release gates."""


def _field(report: Any, path: str) -> Any:
    value = report
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            raise ReportValidationError(f"Missing required smoke field: {path}")
        value = value[name]
    return value


def _require(report: dict[str, Any], path: str, expected: Any) -> None:
    value = _field(report, path)
    # In Python 1 == True and False == 0. Neither is valid JSON evidence for
    # the other: success flags and counts must retain their actual types.
    if type(value) is not type(expected) or value != expected:
        raise ReportValidationError(f"Invalid required smoke field: {path}")


def _true(report: dict[str, Any], prefix: str, names: tuple[str, ...]) -> None:
    for name in names:
        _require(report, prefix + name, True)


def _frontend(report: dict[str, Any], prefix: str) -> None:
    _true(
        report,
        prefix,
        (
            "verified",
            "tauriOriginCors",
            "privateNetworkPreflight",
            "runtimeCorsVerified",
            "webViewReady",
            "webViewTauriRuntime",
        ),
    )
    _require(report, prefix + "source", "tauri-webview")
    _require(report, prefix + "backendStaticFallbackAvailable", False)
    _require(report, prefix + "backendStaticFallbackStatusCode", 404)


def _support_bundle(report: dict[str, Any], prefix: str) -> None:
    _true(report, prefix, ("verified", "tokenProtected", "redactionVerified"))
    _require(report, prefix + "unauthorizedStatus", 401)
    _true(
        report,
        prefix + "meetingPrivacy.",
        (
            "verified",
            "audioAbsent",
            "transcriptStoresAbsent",
            "outlookCredentialArtifactsAbsent",
            "webhookSecretArtifactsAbsent",
            "voiceprintArtifactsAbsent",
        ),
    )
    _require(report, prefix + "meetingPrivacy.sensitiveFindingCount", 0)


def validate_report(report: Any, *, installer: Path) -> None:
    if not isinstance(report, dict):
        raise ReportValidationError("Smoke report must be a JSON object")
    expected_installer = installer.resolve(strict=True)
    if not expected_installer.is_file():
        raise ReportValidationError("Expected installer is not a file")
    recorded_installer = _field(report, "installer")
    if not isinstance(recorded_installer, str) or not Path(recorded_installer).is_absolute():
        raise ReportValidationError("Smoke installer path must be absolute")
    if Path(recorded_installer).resolve(strict=True) != expected_installer:
        raise ReportValidationError("Smoke report belongs to a different installer")

    for prefix in ("", "cleanInstall."):
        _true(report, prefix, ("ok", "ready", "cleanupVerified"))
        _require(report, prefix + "runtimeMode", "tauri-supervised")
        _require(report, prefix + "launchKind", "sidecar")
    _require(report, "desktopSmokeExitCode", 0)
    _require(report, "cleanInstall.processExitCode", 0)
    _frontend(report, "cleanInstall.frontend.")
    _support_bundle(report, "cleanInstall.supportBundle.")
    _true(
        report,
        "upgrade.",
        ("verified", "sentinelPreserved", "retiredApplicationModuleRemoved", "secondCleanupVerified"),
    )
    _require(report, "upgrade.secondRuntimeMode", "tauri-supervised")
    _require(report, "upgrade.secondLaunchKind", "sidecar")
    for prefix in ("", "upgrade."):
        _frontend(report, prefix + "frontend.")
        _support_bundle(report, prefix + "supportBundle.")
        _true(report, prefix + "frontendAssetOwnership.", ("verified", "backendFrontendAssetTreesAbsent"))
        _require(report, prefix + "frontendAssetOwnership.source", "tauri-webview")
        _true(report, prefix + "localPolishingRuntime.", ("ok", "cpuFallback"))
        _require(report, prefix + "localPolishingRuntime.bundledGgufCount", 0)
        _require(report, prefix + "localPolishingRuntime.primaryBackend", "vulkan")
        _require(report, prefix + "localPolishingRuntime.version.exitCode", 0)

    _true(report, "meetingResources.", ("verified", "aec3NoticePresent", "optionalWeSpeakerModelAbsent"))
    _true(report, "meetingResources.diarizationWorker.", ("ok", "optionalModelsAbsent"))
    _require(report, "meetingResources.diarizationWorker.selfTest.loadsModels", False)
    _require(report, "meetingResources.diarizationWorker.selfTest.loadsUserAudio", False)
    _require(report, "meetingResources.diarizationWorker.selfTest.memoryLimit", "jobObject")

    _true(report, "mediaPreparation.", ("verified", "requireFfprobe"))
    _require(report, "mediaPreparation.report.ok", True)
    _require(report, "mediaPreparation.report.mediaTools.requireFfprobe", True)
    for field, count in (("failedChecks", 0), ("passedChecks", 5), ("totalChecks", 5)):
        _require(report, "mediaPreparation.report.summary." + field, count)
    checks = _field(report, "mediaPreparation.report.checks")
    if not isinstance(checks, list) or len(checks) != len(MEDIA_CHECK_NAMES):
        raise ReportValidationError("Incomplete installed media checks")
    names = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str) or check.get("ok") is not True:
            raise ReportValidationError("Invalid installed media check")
        names.append(check["name"])
    if set(names) != MEDIA_CHECK_NAMES:
        raise ReportValidationError("Missing or duplicate installed media checks")
    by_name = {check["name"]: check for check in checks}
    _require(by_name, "ffprobe_duration_probe.ffprobeAvailable", True)
    _require(by_name, "youtube_post_download_normalization.inputRemoved", True)
    _require(by_name, "azure_mai_audio_preparation.temporaryFileCleaned", True)

    _true(report, "uninstall.", ("attempted", "verified", "installArtifactsRemoved", "dataDirPreserved"))
    _require(report, "uninstall.remainingInstallArtifacts", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8-sig"))
        validate_report(report, installer=args.installer)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Installed release smoke evidence rejected: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Installed release smoke evidence verified for the expected installer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

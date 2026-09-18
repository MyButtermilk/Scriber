from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.ci.validate_installer_smoke_report import ReportValidationError, validate_report

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "validate_installer_smoke_report.py"


@pytest.fixture
def smoke(tmp_path):
    installer = tmp_path / "Scriber_0.5.116_x64-setup.exe"
    installer.write_bytes(b"existing downloaded installer fixture")
    # Actual Windows v0.5.116 schema, stripped only of non-gating diagnostics.
    frontend = {
        "verified": True,
        "source": "tauri-webview",
        "backendStaticFallbackAvailable": False,
        "backendStaticFallbackStatusCode": 404,
        "tauriOriginCors": True,
        "privateNetworkPreflight": True,
        "runtimeCorsVerified": True,
        "webViewReady": True,
        "webViewTauriRuntime": True,
    }
    support = {
        "verified": True,
        "tokenProtected": True,
        "unauthorizedStatus": 401,
        "redactionVerified": True,
        "meetingPrivacy": {
            "verified": True,
            "sensitiveFindingCount": 0,
            "audioAbsent": True,
            "transcriptStoresAbsent": True,
            "outlookCredentialArtifactsAbsent": True,
            "webhookSecretArtifactsAbsent": True,
            "voiceprintArtifactsAbsent": True,
        },
    }
    ownership = {"verified": True, "source": "tauri-webview", "backendFrontendAssetTreesAbsent": True}
    polishing = {
        "ok": True,
        "cpuFallback": True,
        "bundledGgufCount": 0,
        "primaryBackend": "vulkan",
        "version": {"exitCode": 0},
    }
    report = {
        "installer": str(installer.resolve()),
        "ok": True,
        "ready": True,
        "cleanupVerified": True,
        "runtimeMode": "tauri-supervised",
        "launchKind": "sidecar",
        "desktopSmokeExitCode": 0,
        "cleanInstall": {
            "ok": True,
            "ready": True,
            "cleanupVerified": True,
            "runtimeMode": "tauri-supervised",
            "launchKind": "sidecar",
            "processExitCode": 0,
            "frontend": deepcopy(frontend),
            "supportBundle": deepcopy(support),
        },
        "frontend": frontend,
        "supportBundle": support,
        "frontendAssetOwnership": ownership,
        "localPolishingRuntime": polishing,
        "upgrade": {
            "verified": True,
            "sentinelPreserved": True,
            "retiredApplicationModuleRemoved": True,
            "secondCleanupVerified": True,
            "secondRuntimeMode": "tauri-supervised",
            "secondLaunchKind": "sidecar",
            "frontend": deepcopy(frontend),
            "supportBundle": deepcopy(support),
            "frontendAssetOwnership": deepcopy(ownership),
            "localPolishingRuntime": deepcopy(polishing),
        },
        "meetingResources": {
            "verified": True,
            "aec3NoticePresent": True,
            "optionalWeSpeakerModelAbsent": True,
            "diarizationWorker": {
                "ok": True,
                "optionalModelsAbsent": True,
                "selfTest": {"loadsModels": False, "loadsUserAudio": False, "memoryLimit": "jobObject"},
            },
        },
        "mediaPreparation": {
            "verified": True,
            "requireFfprobe": True,
            "report": {
                "ok": True,
                "mediaTools": {"requireFfprobe": True},
                "summary": {"failedChecks": 0, "passedChecks": 5, "totalChecks": 5},
                "checks": [
                    {"name": "file_upload_compression", "ok": True},
                    {"name": "video_upload_audio_extraction", "ok": True},
                    {"name": "youtube_post_download_normalization", "ok": True, "inputRemoved": True},
                    {"name": "azure_mai_audio_preparation", "ok": True, "temporaryFileCleaned": True},
                    {"name": "ffprobe_duration_probe", "ok": True, "ffprobeAvailable": True},
                ],
            },
        },
        "uninstall": {
            "attempted": True,
            "verified": True,
            "installArtifactsRemoved": True,
            "dataDirPreserved": True,
            "remainingInstallArtifacts": [],
        },
    }
    return report, installer


def _change(report, field, value):
    names = field.split(".")
    target = report
    for name in names[:-1]:
        target = target[name]
    target[names[-1]] = value


def test_complete_real_shaped_report_accepts_existing_installer(smoke) -> None:
    report, installer = smoke
    validate_report(report, installer=installer)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ok", False),
        ("ok", 1),
        ("ready", "true"),
        ("cleanupVerified", False),
        ("runtimeMode", "external-attached"),
        ("launchKind", "source"),
        ("desktopSmokeExitCode", 1),
        ("desktopSmokeExitCode", False),
        ("cleanInstall.ok", False),
        ("cleanInstall.ready", False),
        ("cleanInstall.cleanupVerified", False),
        ("cleanInstall.runtimeMode", "external-attached"),
        ("cleanInstall.launchKind", "source"),
        ("cleanInstall.processExitCode", 1),
        ("cleanInstall.frontend.webViewReady", False),
        ("cleanInstall.frontend.privateNetworkPreflight", False),
        ("cleanInstall.supportBundle.redactionVerified", False),
        ("cleanInstall.supportBundle.meetingPrivacy.audioAbsent", False),
        ("upgrade.verified", False),
        ("upgrade.sentinelPreserved", False),
        ("upgrade.retiredApplicationModuleRemoved", False),
        ("upgrade.secondCleanupVerified", False),
        ("upgrade.secondRuntimeMode", "external-attached"),
        ("upgrade.secondLaunchKind", "source"),
        ("frontend.verified", False),
        ("frontend.webViewReady", False),
        ("frontend.webViewTauriRuntime", False),
        ("frontend.backendStaticFallbackAvailable", True),
        ("frontend.backendStaticFallbackStatusCode", 200),
        ("frontend.tauriOriginCors", False),
        ("frontend.privateNetworkPreflight", False),
        ("frontend.runtimeCorsVerified", False),
        ("upgrade.frontend.webViewReady", False),
        ("supportBundle.verified", False),
        ("supportBundle.tokenProtected", False),
        ("supportBundle.unauthorizedStatus", 200),
        ("supportBundle.redactionVerified", False),
        ("supportBundle.meetingPrivacy.sensitiveFindingCount", 1),
        ("supportBundle.meetingPrivacy.audioAbsent", False),
        ("supportBundle.meetingPrivacy.transcriptStoresAbsent", False),
        ("supportBundle.meetingPrivacy.outlookCredentialArtifactsAbsent", False),
        ("supportBundle.meetingPrivacy.webhookSecretArtifactsAbsent", False),
        ("supportBundle.meetingPrivacy.voiceprintArtifactsAbsent", False),
        ("upgrade.supportBundle.redactionVerified", False),
        ("frontendAssetOwnership.backendFrontendAssetTreesAbsent", False),
        ("upgrade.frontendAssetOwnership.verified", False),
        ("meetingResources.verified", False),
        ("meetingResources.diarizationWorker.ok", False),
        ("meetingResources.diarizationWorker.selfTest.loadsUserAudio", True),
        ("localPolishingRuntime.ok", False),
        ("localPolishingRuntime.cpuFallback", False),
        ("localPolishingRuntime.bundledGgufCount", False),
        ("localPolishingRuntime.version.exitCode", 1),
        ("upgrade.localPolishingRuntime.ok", False),
        ("mediaPreparation.verified", False),
        ("mediaPreparation.requireFfprobe", False),
        ("mediaPreparation.report.ok", False),
        ("mediaPreparation.report.summary.failedChecks", 1),
        ("mediaPreparation.report.summary.passedChecks", 4),
        ("mediaPreparation.report.summary.totalChecks", 6),
        ("uninstall.attempted", False),
        ("uninstall.verified", False),
        ("uninstall.installArtifactsRemoved", False),
        ("uninstall.dataDirPreserved", False),
        ("uninstall.remainingInstallArtifacts", ["scriber-desktop.exe"]),
    ],
)
def test_failed_or_wrongly_typed_required_gate_is_rejected(smoke, field, value) -> None:
    report, installer = smoke
    _change(report, field, value)
    with pytest.raises(ReportValidationError):
        validate_report(report, installer=installer)


@pytest.mark.parametrize(
    "field", ("ok", "cleanInstall", "upgrade", "frontend", "supportBundle", "mediaPreparation", "uninstall")
)
def test_missing_required_evidence_is_rejected(smoke, field) -> None:
    report, installer = smoke
    del report[field]
    with pytest.raises(ReportValidationError, match="Missing required smoke field"):
        validate_report(report, installer=installer)


@pytest.mark.parametrize("mode", ("missing", "duplicate", "failed", "truthy", "malformed", "ffprobe", "cleanup"))
def test_media_summary_cannot_hide_incomplete_or_failed_individual_checks(smoke, mode) -> None:
    report, installer = smoke
    checks = report["mediaPreparation"]["report"]["checks"]
    if mode == "missing":
        checks.pop()
    elif mode == "duplicate":
        checks[1] = deepcopy(checks[0])
    elif mode == "failed":
        checks[0]["ok"] = False
    elif mode == "truthy":
        checks[0]["ok"] = 1
    elif mode == "malformed":
        checks[0] = "success"
    elif mode == "ffprobe":
        checks[4]["ffprobeAvailable"] = False
    else:
        checks[3]["temporaryFileCleaned"] = False
    with pytest.raises(ReportValidationError):
        validate_report(report, installer=installer)


@pytest.mark.parametrize("mode", ("different", "relative", "missing", "directory", "not-string"))
def test_installer_identity_cannot_be_replaced_or_omitted(smoke, tmp_path, mode) -> None:
    report, installer = smoke
    if mode == "different":
        other = tmp_path / "another-installer.exe"
        other.write_bytes(installer.read_bytes())
        report["installer"] = str(other)
    elif mode == "relative":
        report["installer"] = installer.name
    elif mode == "missing":
        installer.unlink()
    elif mode == "directory":
        installer = tmp_path
        report["installer"] = str(installer)
    else:
        report["installer"] = None
    with pytest.raises((ReportValidationError, OSError)):
        validate_report(report, installer=installer)


@pytest.mark.parametrize("mode", ("valid", "false", "missing-report", "invalid-json", "array"))
def test_cli_exits_nonzero_unless_complete_existing_evidence_passes(smoke, tmp_path, mode) -> None:
    report, installer = smoke
    path = tmp_path / "smoke.json"
    if mode == "false":
        report["upgrade"]["verified"] = False
    if mode != "missing-report":
        payload = "{" if mode == "invalid-json" else json.dumps([] if mode == "array" else report)
        path.write_text(payload, encoding="utf-8-sig")
    result = subprocess.run(
        [sys.executable, str(HELPER), "--report", str(path), "--installer", str(installer)],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == (0 if mode == "valid" else 1), result.stderr
    assert installer.read_bytes() == b"existing downloaded installer fixture"

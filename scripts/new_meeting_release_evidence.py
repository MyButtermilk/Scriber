from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_meeting_release_matrix import REPORT_KIND, REQUIRED_COVERAGE, SCHEMA_VERSION


PROFILE_COVERAGE: dict[str, dict[str, list[str]]] = {
    "teams-laptop-speakerphone": {
        "meetingApps": ["teams-desktop"],
        "audioRoutes": ["laptop-speakers"],
        "audioConditions": [
            "quiet-speech",
            "background-noise",
            "remote-echo",
            "double-talk",
            "multiple-remote-speakers",
        ],
        "validationAreas": ["canonical-transcript", "analysis-citations"],
    },
    "zoom-wired-headset": {
        "meetingApps": ["zoom-desktop"],
        "audioRoutes": ["wired-headset"],
        "audioConditions": ["quiet-speech"],
    },
    "meet-bluetooth-headset": {
        "meetingApps": ["google-meet-chrome"],
        "audioRoutes": ["bluetooth-headset"],
        "audioConditions": ["quiet-speech"],
    },
    "teams-usb-microphone": {
        "meetingApps": ["teams-desktop"],
        "audioRoutes": ["usb-microphone"],
        "audioConditions": ["background-noise"],
    },
    "zoom-default-device-switch": {
        "meetingApps": ["zoom-desktop"],
        "audioRoutes": ["default-device-switch"],
        "audioConditions": ["quiet-speech"],
    },
    "provider-network-reconnect": {
        "failureModes": ["network-loss", "provider-reconnect"],
    },
    "backend-crash-recovery": {"failureModes": ["backend-crash"]},
    "shell-exit-resume": {"failureModes": ["shell-exit", "resume"]},
    "corrupt-chunk-recovery": {"failureModes": ["corrupt-chunk"]},
    "disk-full-recovery": {"failureModes": ["disk-full"]},
    "outlook-work-school": {
        "outlookAccounts": ["work-school"],
        "outlookScenarios": [
            "connect",
            "reconnect",
            "token-expiry",
            "delta-pagination",
            "tenant-block",
            "offline",
        ],
    },
    "outlook-microsoft-personal": {
        "outlookAccounts": ["microsoft-personal"],
        "outlookScenarios": ["connect", "reconnect"],
    },
    "recording-60m": {"soakScenarios": ["recording-60m"]},
    "stability-2h": {"soakScenarios": ["stability-2h"]},
    "voiceprint-held-corpus": {
        "validationAreas": ["voiceprint-held-corpus"],
    },
    "support-bundle-privacy": {
        "validationAreas": ["support-bundle-privacy"],
    },
    "eu-voiceprint-privacy-review": {
        "validationAreas": ["eu-voiceprint-privacy-review"],
    },
    "automated-regression-suite": {
        "validationAreas": ["automated-regression-suite"],
    },
    "signed-release": {
        "validationAreas": ["signed-release"],
    },
}


def build_template(
    *,
    profile: str,
    app_version: str,
    installer_sha256: str,
    signed_installer: bool,
) -> dict[str, Any]:
    coverage = {key: [] for key in REQUIRED_COVERAGE}
    for key, values in PROFILE_COVERAGE[profile].items():
        coverage[key] = list(values)
    measurements, checks, outlook_results = _required_placeholders(coverage)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "scenarioId": profile,
        "completed": False,
        "operatorConfirmed": False,
        "capturedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "appVersion": app_version,
        "build": {
            "installedApp": True,
            "signedInstaller": bool(signed_installer),
            "authenticodeValid": bool(signed_installer),
            "updaterSignatureVerified": False,
            "installerSha256": installer_sha256,
        },
        "coverage": coverage,
        "measurements": measurements,
        "checks": checks,
        "outlookResults": outlook_results,
        "artifacts": [
            {
                "kind": "replace-with-redacted-supporting-evidence",
                "path": "artifacts/replace-me",
                "sha256": "",
            }
        ],
        "notes": "Replace placeholders with measurements from the real installed-app scenario. Do not include transcript, audio, tokens, endpoint IDs, voiceprints, or personal data.",
    }


def _required_placeholders(
    coverage: dict[str, list[str]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    measurements: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    outlook_results = {item: False for item in coverage["outlookScenarios"]}

    if coverage["meetingApps"]:
        measurements.update(captureStartLatencyMs=None, liveInterimP95Ms=None)
        checks.update(
            microphoneSourceActive=False,
            systemSourceActive=False,
            canonicalSegmentsChronological=False,
            canonicalSegmentsClickable=False,
            canonicalSegmentsAudioAligned=False,
            analysisSchemaValid=False,
            analysisCitationsValid=False,
        )
    conditions = set(coverage["audioConditions"])
    if "remote-echo" in conditions:
        measurements["aecEchoReductionDb"] = None
        checks["aecRenderReferenceActive"] = False
    if conditions & {"remote-echo", "double-talk"}:
        checks["localDoubleTalkSpeechPreserved"] = False
    if "multiple-remote-speakers" in conditions:
        checks["multipleRemoteSpeakersSeparated"] = False
    if "default-device-switch" in coverage["audioRoutes"]:
        measurements.update(deviceSwitchCount=None, deviceSwitchGapCount=None)
        checks["deviceReconnectSucceeded"] = False

    failure_modes = set(coverage["failureModes"])
    if failure_modes & {"network-loss", "provider-reconnect"}:
        measurements.update(providerOutageCount=None, providerReconnectGapCount=None)
        checks["providerRecovered"] = False
    if "backend-crash" in failure_modes:
        measurements["crashLostAudioSeconds"] = None
        checks.update(existingChunksFinalizable=False, backendRecoverySucceeded=False)
    if "shell-exit" in failure_modes:
        checks["shellCleanupSucceeded"] = False
    if "resume" in failure_modes:
        measurements.update(resumeCount=None, resumeGapCount=None)
        checks["resumeSucceeded"] = False
    if "corrupt-chunk" in failure_modes:
        measurements["corruptChunkGapCount"] = None
        checks.update(corruptChunkQuarantined=False, remainingChunksFinalized=False)
    if "disk-full" in failure_modes:
        checks.update(
            diskFullDetected=False,
            completedChunksPreserved=False,
            partialChunkPublished=True,
        )

    if coverage["outlookAccounts"] and "connect" not in coverage["outlookScenarios"]:
        checks["outlookAccountConnected"] = False
    if "offline" in coverage["outlookScenarios"]:
        checks["offlineMeetingCaptureAvailable"] = False

    soak = set(coverage["soakScenarios"])
    if "recording-60m" in soak:
        measurements.update(
            recordingDurationSeconds=None,
            unmarkedAudioLossCount=None,
            intentionalGapExpectedCount=None,
            intentionalGapObservedCount=None,
        )
    if "stability-2h" in soak:
        measurements["stabilityDurationSeconds"] = None
        checks["stabilitySoakPassed"] = False

    areas = set(coverage["validationAreas"])
    if "canonical-transcript" in areas:
        checks.update(
            canonicalSegmentsChronological=False,
            canonicalSegmentsClickable=False,
            canonicalSegmentsAudioAligned=False,
        )
    if "analysis-citations" in areas:
        checks.update(analysisSchemaValid=False, analysisCitationsValid=False)
    if "voiceprint-held-corpus" in areas:
        measurements["voiceprintFalseHighConfidenceMatches"] = None
        checks["ambiguousVoiceMatchesRemainAnonymous"] = False
    if "support-bundle-privacy" in areas:
        measurements["supportBundleSensitiveFindingCount"] = None
        checks.update(
            supportBundleAudioAbsent=False,
            supportBundleTranscriptContentAbsent=False,
            supportBundleOutlookSecretsAbsent=False,
            supportBundleWebhookSecretsAbsent=False,
            supportBundleVoiceprintsAbsent=False,
        )
    if "eu-voiceprint-privacy-review" in areas:
        checks["voiceprintPrivacyLegalReviewApproved"] = False
    if "automated-regression-suite" in areas:
        measurements["automatedTestsPassed"] = None
        checks["automatedRegressionSuitePassed"] = False
    if "signed-release" in areas:
        checks["releaseAssetsVerified"] = False
    return measurements, checks, outlook_results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a non-passing draft for one real Meeting release scenario.",
    )
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_COVERAGE))
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--signed-installer", action="store_true")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        print(f"refusing to overwrite existing draft: {output}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_template(
        profile=args.profile,
        app_version=args.app_version,
        installer_sha256=args.installer_sha256,
        signed_installer=bool(args.signed_installer),
    )
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": True, "profile": args.profile, "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

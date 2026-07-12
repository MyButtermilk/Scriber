from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.new_meeting_release_evidence import PROFILE_COVERAGE, build_template
from scripts.validate_meeting_release_matrix import validate_matrix


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SHA = "a" * 64


def _write_complete_matrix(root: Path) -> list[Path]:
    paths: list[Path] = []
    artifact_dir = root / "artifacts"
    artifact_dir.mkdir(parents=True)
    for profile in PROFILE_COVERAGE:
        artifact = artifact_dir / f"{profile}.json"
        artifact.write_text(json.dumps({"ok": True, "scenario": profile}), encoding="utf-8")
        payload = build_template(
            profile=profile,
            app_version="0.4.35",
            installer_sha256=INSTALLER_SHA,
            signed_installer=True,
        )
        payload["completed"] = True
        payload["operatorConfirmed"] = True
        payload["build"]["authenticodeValid"] = True
        payload["build"]["updaterSignatureVerified"] = True
        payload["measurements"].update(
            captureStartLatencyMs=2400,
            liveInterimP95Ms=1400,
            aecEchoReductionDb=8.5,
            deviceSwitchCount=1,
            deviceSwitchGapCount=1,
            providerOutageCount=1,
            providerReconnectGapCount=1,
            crashLostAudioSeconds=22,
            resumeCount=1,
            resumeGapCount=1,
            corruptChunkGapCount=1,
            recordingDurationSeconds=3605,
            unmarkedAudioLossCount=0,
            intentionalGapExpectedCount=1,
            intentionalGapObservedCount=1,
            stabilityDurationSeconds=7210,
            voiceprintFalseHighConfidenceMatches=0,
            supportBundleSensitiveFindingCount=0,
            automatedTestsPassed=1237,
        )
        payload["checks"].update(
            microphoneSourceActive=True,
            systemSourceActive=True,
            canonicalSegmentsChronological=True,
            canonicalSegmentsClickable=True,
            canonicalSegmentsAudioAligned=True,
            analysisSchemaValid=True,
            analysisCitationsValid=True,
            aecRenderReferenceActive=True,
            localDoubleTalkSpeechPreserved=True,
            multipleRemoteSpeakersSeparated=True,
            deviceReconnectSucceeded=True,
            providerRecovered=True,
            existingChunksFinalizable=True,
            backendRecoverySucceeded=True,
            shellCleanupSucceeded=True,
            resumeSucceeded=True,
            corruptChunkQuarantined=True,
            remainingChunksFinalized=True,
            diskFullDetected=True,
            completedChunksPreserved=True,
            partialChunkPublished=False,
            offlineMeetingCaptureAvailable=True,
            stabilitySoakPassed=True,
            ambiguousVoiceMatchesRemainAnonymous=True,
            supportBundleAudioAbsent=True,
            supportBundleTranscriptContentAbsent=True,
            supportBundleOutlookSecretsAbsent=True,
            supportBundleWebhookSecretsAbsent=True,
            supportBundleVoiceprintsAbsent=True,
            voiceprintPrivacyLegalReviewApproved=True,
            automatedRegressionSuitePassed=True,
            releaseAssetsVerified=True,
        )
        payload["outlookResults"] = {
            key: True for key in payload["coverage"]["outlookScenarios"]
        }
        payload["artifacts"] = [
            {
                "kind": "redacted-scenario-report",
                "path": f"artifacts/{artifact.name}",
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ]
        path = root / f"meeting-release-evidence-{profile}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        paths.append(path)
    return paths


def test_complete_real_matrix_contract_passes(tmp_path: Path) -> None:
    _write_complete_matrix(tmp_path)

    payload = validate_matrix(input_dir=tmp_path, expected_app_version="0.4.35")

    assert payload["ok"] is True
    assert payload["reportCount"] == len(PROFILE_COVERAGE)
    assert payload["failedReportCount"] == 0
    assert payload["installerSha256"] == INSTALLER_SHA
    assert all(category["ok"] for category in payload["coverage"].values())
    assert all(check["ok"] for check in payload["acceptanceChecks"])


def test_missing_profile_is_reported_as_coverage_failure(tmp_path: Path) -> None:
    paths = _write_complete_matrix(tmp_path)
    missing = next(path for path in paths if "meet-bluetooth-headset" in path.name)
    missing.unlink()

    payload = validate_matrix(input_dir=tmp_path, expected_app_version="0.4.35")

    assert payload["ok"] is False
    assert payload["coverage"]["meetingApps"]["missing"] == ["google-meet-chrome"]
    assert "bluetooth-headset" in payload["coverage"]["audioRoutes"]["missing"]


def test_scenario_thresholds_are_enforced(tmp_path: Path) -> None:
    paths = _write_complete_matrix(tmp_path)
    target = next(path for path in paths if "teams-laptop-speakerphone" in path.name)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["measurements"]["captureStartLatencyMs"] = 3001
    payload["measurements"]["liveInterimP95Ms"] = 2001
    payload["measurements"]["aecEchoReductionDb"] = 0
    target.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_matrix(input_dir=tmp_path)
    failures = next(item["failures"] for item in result["reports"] if item["path"] == str(target))

    assert result["ok"] is False
    assert any("captureStartLatencyMs" in failure for failure in failures)
    assert any("liveInterimP95Ms" in failure for failure in failures)
    assert any("aecEchoReductionDb" in failure for failure in failures)


def test_tampered_artifact_and_sensitive_content_are_rejected(tmp_path: Path) -> None:
    paths = _write_complete_matrix(tmp_path)
    target = paths[0]
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["accessToken"] = "secret-token-value"
    target.write_text(json.dumps(payload), encoding="utf-8")
    artifact = tmp_path / payload["artifacts"][0]["path"]
    artifact.write_text("tampered", encoding="utf-8")

    result = validate_matrix(input_dir=tmp_path)
    failures = next(item["failures"] for item in result["reports"] if item["path"] == str(target))

    assert result["ok"] is False
    assert any("forbidden sensitive field" in failure for failure in failures)
    assert any("sha256 does not match" in failure for failure in failures)


def test_partial_mode_validates_present_report_without_claiming_full_coverage(tmp_path: Path) -> None:
    paths = _write_complete_matrix(tmp_path)
    for path in paths[1:]:
        path.unlink()

    payload = validate_matrix(
        input_dir=tmp_path,
        expected_app_version="0.4.35",
        require_full_matrix=False,
    )

    assert payload["ok"] is True
    assert payload["coverage"]["meetingApps"]["ok"] is False
    assert payload["requireFullMatrix"] is False


def test_cli_writes_machine_readable_failure_report(tmp_path: Path) -> None:
    output = tmp_path / "validation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "validate_meeting_release_matrix.py"),
            "--input-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["reportCount"] == 0

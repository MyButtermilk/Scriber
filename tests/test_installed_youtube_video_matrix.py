from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.installed_youtube_video_matrix import (
    CONTRACT,
    LANE_IDS,
    PREVET_CONTRACT,
    MatrixContractError,
    aggregate_lane_reports,
    finalize_lane_report,
    load_fixture,
    normalized_path_sha256,
    sha256_file,
    sha256_text,
    validate_fixture,
    validate_prevet_evidence,
)
from scripts.prevet_installed_youtube_video_matrix import _parse_row as parse_prevet_row
from scripts.smoke_installed_transcription_workflows import (
    WorkflowFailure,
    classify_failure,
    parse_duration_seconds,
    wait_for_youtube_completion_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "scripts" / "fixtures" / "installed-youtube-video-matrix-v1.json"
INSTALLER_SHA = "a" * 64
DATA_ROOT_SHA = "b" * 64


def fixture_payload() -> dict:
    lanes = []
    for index, lane_id in enumerate(LANE_IDS, start=1):
        captions_first = lane_id == "captions-first"
        lanes.append(
            {
                "laneId": lane_id,
                "preVetted": True,
                "installedExtractionPreVetted": True,
                "executionMode": "captions-first" if captions_first else "audio-provider",
                "expectedCompletionEvent": (
                    "youtube.captions.completed" if captions_first else "pipeline.transcription.completed"
                ),
                "probeKind": "caption-route" if captions_first else "audio-format",
                "expectedExtractionLane": f"lane-{index}",
                "expectedExtractionObservation": (
                    {
                        "routeKinds": ["youtube-captions"],
                        "captionKinds": ["manual", "automatic"],
                        "captionFormats": ["json3", "vtt"],
                        "minCueCount": 1,
                        "minTextChars": 20,
                    }
                    if captions_first
                    else {
                        "routeKinds": ["audio-format"],
                        "selectorIndexes": [1, 2, 3],
                    }
                ),
                "primary": {
                    "url": f"https://example.invalid/primary-{index}",
                    "durationBandSeconds": {"min": index, "max": index + 100},
                },
                "replacement": {
                    "url": f"https://example.invalid/replacement-{index}",
                    "durationBandSeconds": {"min": index + 1, "max": index + 101},
                },
            }
        )
    return {
        "schemaVersion": "1",
        "contract": CONTRACT,
        "lanes": lanes,
    }


def passing_report(lane: dict, index: int) -> dict:
    observed_extraction = (
        {
            "durationSeconds": index + 10,
            "routeKind": "youtube-captions",
            "captionKind": "manual",
            "captionFormat": "json3",
            "language": "en",
            "cueCount": 3,
            "textChars": 50,
        }
        if lane["probeKind"] == "caption-route"
        else {
            "durationSeconds": index + 10,
            "routeKind": "audio-format",
            "selectorIndex": 1,
            "selector": "bestaudio",
            "ext": "webm",
            "container": "webm_dash",
            "acodec": "opus",
            "protocol": "https",
            "formatId": "251",
        }
    )
    return {
        "schemaVersion": "1",
        "contract": CONTRACT,
        "laneId": lane["laneId"],
        "fixtureUrlSha256": sha256_text(lane["primary"]["url"]),
        "selectionMarker": "primary",
        "attemptIndex": 1,
        "transientRetryUsed": False,
        "durationBandSeconds": lane["primary"]["durationBandSeconds"],
        "expectedExtractionLane": lane["expectedExtractionLane"],
        "observedExtraction": observed_extraction,
        "executionMode": lane["executionMode"],
        "completionEvent": lane["expectedCompletionEvent"],
        "outcome": "passed",
        "ok": True,
        "failureCategory": None,
        "failureCode": None,
        "observedDurationSeconds": index + 10,
        "contentChars": 50,
        "summaryChars": 25,
        "elapsedMs": 1000,
        "sourceAssetCleanupVerified": True,
        "childProcessCleanupVerified": True,
        "releaseTag": "v0.5.48",
        "installerSha256": INSTALLER_SHA,
        "dataRootSha256": DATA_ROOT_SHA,
    }


def test_tracked_fixture_is_versioned_pre_vetted_and_distinct() -> None:
    fixture = load_fixture(FIXTURE_PATH)

    assert tuple(lane["laneId"] for lane in fixture["lanes"]) == LANE_IDS
    urls = [lane[marker]["url"] for lane in fixture["lanes"] for marker in ("primary", "replacement")]
    assert len(urls) == len(set(urls)) == 10
    pending = {lane["laneId"] for lane in fixture["lanes"] if not lane["installedExtractionPreVetted"]}
    assert pending == set(LANE_IDS)


def test_fixture_validation_fails_closed_for_unvetted_or_duplicate_candidates() -> None:
    payload = fixture_payload()
    payload["lanes"][0]["preVetted"] = False
    with pytest.raises(MatrixContractError, match="preVetted"):
        validate_fixture(payload)

    payload = fixture_payload()
    payload["lanes"][1]["primary"]["url"] = payload["lanes"][0]["primary"]["url"]
    with pytest.raises(MatrixContractError, match="distinct"):
        validate_fixture(payload)

    payload = fixture_payload()
    payload["lanes"][1]["installedExtractionPreVetted"] = False
    with pytest.raises(MatrixContractError, match="before an installed matrix run"):
        validate_fixture(payload, require_installed_extraction_pre_vetted=True)


def test_lane_finalizer_retains_only_redacted_contract_fields() -> None:
    fixture = fixture_payload()
    lane = fixture["lanes"][0]
    workflow = {
        "ok": True,
        "identity": {
            "installerSha256": INSTALLER_SHA,
            "releaseTag": "v0.5.48",
            "dataRootSha256": DATA_ROOT_SHA,
        },
        "checks": [
            {
                "workflow": "youtube",
                "ok": True,
                "urlSha256": sha256_text(lane["primary"]["url"]),
                "laneId": lane["laneId"],
                "selectionMarker": "primary",
                "expectedExtractionLane": lane["expectedExtractionLane"],
                "executionMode": lane["executionMode"],
                "completionEvent": lane["expectedCompletionEvent"],
                "youtubeUrl": "must-not-survive",
                "transcript": {
                    "id": "secret-id",
                    "title": "private title",
                    "sourceUrl": "must-not-survive",
                    "durationSeconds": 12.5,
                    "contentChars": 40,
                    "summaryChars": 30,
                },
                "sourceAssetCleanupVerified": True,
                "elapsedMs": 321,
            }
        ],
    }
    desktop = {"ok": True, "cleanupVerified": True, "dataDir": "must-not-survive"}

    report = finalize_lane_report(
        workflow_report=workflow,
        desktop_report=desktop,
        lane=lane,
        selection_marker="primary",
        attempt_index=1,
        transient_retry_used=False,
        installer_sha256=INSTALLER_SHA,
        release_tag="v0.5.48",
        data_root_sha256=DATA_ROOT_SHA,
    )

    serialized = json.dumps(report)
    assert report["ok"] is True
    assert report["fixtureUrlSha256"] == sha256_text(lane["primary"]["url"])
    assert "must-not-survive" not in serialized
    assert "secret-id" not in serialized
    assert "private title" not in serialized


def test_lane_finalizer_fails_closed_without_installed_route_event() -> None:
    lane = fixture_payload()["lanes"][3]
    workflow = {
        "ok": True,
        "identity": {
            "installerSha256": INSTALLER_SHA,
            "releaseTag": "v0.5.48",
            "dataRootSha256": DATA_ROOT_SHA,
        },
        "checks": [
            {
                "workflow": "youtube",
                "ok": True,
                "urlSha256": sha256_text(lane["primary"]["url"]),
                "laneId": lane["laneId"],
                "selectionMarker": "primary",
                "expectedExtractionLane": lane["expectedExtractionLane"],
                "executionMode": "captions-first",
                "completionEvent": "pipeline.transcription.completed",
                "sourceAssetCleanupVerified": True,
                "transcript": {
                    "durationSeconds": 12.5,
                    "contentChars": 40,
                    "summaryChars": 30,
                },
            }
        ],
    }

    report = finalize_lane_report(
        workflow_report=workflow,
        desktop_report={"ok": True, "cleanupVerified": True},
        lane=lane,
        selection_marker="primary",
        attempt_index=1,
        transient_retry_used=False,
        installer_sha256=INSTALLER_SHA,
        release_tag="v0.5.48",
        data_root_sha256=DATA_ROOT_SHA,
    )

    assert report["ok"] is False
    assert report["failureCode"] == "workflow_route_mismatch"


def test_aggregate_requires_same_identity_distinct_urls_and_cleanup() -> None:
    fixture = fixture_payload()
    reports = [passing_report(lane, index) for index, lane in enumerate(fixture["lanes"], start=1)]

    aggregate = aggregate_lane_reports(fixture, reports)
    assert aggregate["ok"] is True
    assert aggregate["laneCount"] == 5
    assert aggregate["distinctSuccessfulFixtureCount"] == 5

    drifted = copy.deepcopy(reports)
    drifted[1]["installerSha256"] = "c" * 64
    drifted[2]["fixtureUrlSha256"] = drifted[0]["fixtureUrlSha256"]
    drifted[3]["childProcessCleanupVerified"] = False
    aggregate = aggregate_lane_reports(fixture, drifted)
    assert aggregate["ok"] is False
    assert any("same installer" in failure for failure in aggregate["failures"])
    assert any("distinct successful" in failure for failure in aggregate["failures"])
    assert any("child process cleanup" in failure for failure in aggregate["failures"])


def test_prevet_evidence_requires_observed_format_for_all_ten_candidates(tmp_path: Path) -> None:
    fixture = fixture_payload()
    rows = []
    for lane in fixture["lanes"]:
        for marker in ("primary", "replacement"):
            rows.append(
                {
                    "laneId": lane["laneId"],
                    "selectionMarker": marker,
                    "urlSha256": sha256_text(lane[marker]["url"]),
                    "status": "pass",
                    "failureCode": None,
                    "observed": {
                        "durationSeconds": lane[marker]["durationBandSeconds"]["min"] + 1,
                        "routeKind": "audio-format",
                        "selectorIndex": 1,
                        "selector": "bestaudio",
                        "ext": "webm",
                        "container": "webm_dash",
                        "acodec": "opus",
                        "protocol": "https",
                        "formatId": "251",
                    },
                }
            )
            if lane["probeKind"] == "caption-route":
                rows[-1]["observed"] = {
                    "durationSeconds": lane[marker]["durationBandSeconds"]["min"] + 1,
                    "routeKind": "youtube-captions",
                    "captionKind": "automatic",
                    "captionFormat": "json3",
                    "language": "en",
                    "cueCount": 2,
                    "textChars": 50,
                }
    binding_paths = {
        "backend": Path("backend/scriber-backend.exe"),
        "quickJsWrapper": Path("backend/tools/ffmpeg/qjs.exe"),
        "quickJsEngine": Path("backend/tools/ffmpeg/qjs-engine.exe"),
        "ffmpeg": Path("backend/tools/ffmpeg/ffmpeg.exe"),
        "runtimeManifest": Path("backend/tools/ffmpeg/js-runtime-manifest.json"),
    }
    bindings = {}
    for index, (name, relative_path) in enumerate(binding_paths.items(), start=1):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([index]))
        bindings[name] = {"sha256": sha256_file(path), "length": path.stat().st_size}
    evidence = {
        "schemaVersion": "1",
        "contract": PREVET_CONTRACT,
        "status": "pass",
        "fixtureSha256": "f" * 64,
        "bindings": bindings,
        "rows": rows,
    }

    assert (
        validate_prevet_evidence(
            fixture,
            evidence,
            fixture_sha256="f" * 64,
            candidate_payload=tmp_path,
        )
        == []
    )

    evidence["bindings"]["runtimeManifest"]["length"] += 1
    failures = validate_prevet_evidence(
        fixture,
        evidence,
        fixture_sha256="f" * 64,
        candidate_payload=tmp_path,
    )
    assert "installed candidate binding mismatch: runtimeManifest" in failures
    evidence["bindings"]["runtimeManifest"]["length"] -= 1

    evidence["rows"][0]["observed"]["selectorIndex"] = 9
    failures = validate_prevet_evidence(fixture, evidence, fixture_sha256="f" * 64)
    assert any("does not prove the expected extraction lane" in failure for failure in failures)


def test_tracked_captions_lane_rejects_ordinary_audio_format_selection() -> None:
    fixture = load_fixture(FIXTURE_PATH)
    rows = []
    for lane in fixture["lanes"]:
        for marker in ("primary", "replacement"):
            candidate = lane[marker]
            rows.append(
                {
                    "laneId": lane["laneId"],
                    "selectionMarker": marker,
                    "urlSha256": sha256_text(candidate["url"]),
                    "status": "pass",
                    "failureCode": None,
                    "observed": {
                        "durationSeconds": candidate["durationBandSeconds"]["min"] + 1,
                        "routeKind": "audio-format",
                        "selectorIndex": 1,
                        "selector": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
                        "ext": "webm",
                        "container": "webm_dash",
                        "acodec": "opus",
                        "protocol": "https",
                        "formatId": "251",
                    },
                }
            )
    evidence = {
        "schemaVersion": "1",
        "contract": PREVET_CONTRACT,
        "status": "pass",
        "fixtureSha256": "f" * 64,
        "bindings": {
            name: {"sha256": character * 64, "length": 1}
            for name, character in zip(
                ("backend", "quickJsWrapper", "quickJsEngine", "ffmpeg", "runtimeManifest"),
                "abcde",
                strict=True,
            )
        },
        "rows": rows,
    }

    failures = validate_prevet_evidence(fixture, evidence, fixture_sha256="f" * 64)

    assert any(
        failure.startswith("captions-first/")
        and "observed route does not prove the expected extraction lane" in failure
        for failure in failures
    )


def test_prevet_caption_row_retains_only_redacted_route_counts() -> None:
    payload = {
        "probeContract": "InstallerYoutubeFrozenHoldoutProbeV1",
        "schemaVersion": 1,
        "status": "pass",
        "policy": {
            "configDiscovery": False,
            "externalPlugins": False,
            "remoteComponents": False,
            "download": False,
            "explicitSingleRuntime": True,
        },
        "selectedCaption": {
            "routeKind": "youtube-captions",
            "captionKind": "manual",
            "captionFormat": "json3",
            "language": "en",
            "cueCount": 82,
            "textChars": 2794,
            "durationSeconds": 630,
            "captionUrl": "must-not-survive",
            "text": "must-not-survive",
        },
    }
    result = SimpleNamespace(
        cleanup_verified=True,
        status="completed",
        return_code=0,
        stdout=json.dumps(payload).encode(),
    )

    row = parse_prevet_row(
        lane_id="captions-first",
        marker="primary",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        result=result,
        probe_kind="caption-route",
    )

    serialized = json.dumps(row)
    assert row["status"] == "pass"
    assert row["observed"] == {
        "durationSeconds": 630,
        "routeKind": "youtube-captions",
        "captionKind": "manual",
        "captionFormat": "json3",
        "language": "en",
        "cueCount": 82,
        "textChars": 2794,
    }
    assert "must-not-survive" not in serialized
    assert "youtube.com" not in serialized


def test_data_root_hash_is_normalized_and_does_not_expose_path(tmp_path: Path) -> None:
    digest = normalized_path_sha256(tmp_path / "data")

    assert len(digest) == 64
    assert str(tmp_path) not in digest


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("YouTube video unavailable in this region", "external-availability"),
        ("QuickJS runtime manifest mismatch", "bundled-runtime"),
        ("yt-dlp extractor failed", "extractor"),
        ("ffmpeg codec conversion failed", "media-preparation"),
        ("provider authentication failed", "provider"),
    ],
)
def test_workflow_failure_classification_is_bounded(message: str, expected: str) -> None:
    assert classify_failure(message) == expected


def test_duration_parser_accepts_clock_values_and_rejects_invalid_values() -> None:
    assert parse_duration_seconds("01:19:56") == 4796
    assert parse_duration_seconds("02:34") == 154
    assert parse_duration_seconds("unknown") is None


def test_provider_http_403_is_not_misclassified_as_fixture_availability() -> None:
    assert classify_failure("Provider authentication failed with HTTP 403") == "provider"


class _RuntimeLogClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def request_json(self, method: str, path: str) -> dict:
        assert method == "GET"
        assert path == "/api/runtime/logs?limit=2000"
        return self.payload


def _completion_log_payload(event: str, *, truncated: bool = False) -> dict:
    return {
        "truncated": truncated,
        "items": [
            {
                "context": {
                    "event": event,
                    "workflow": "youtube",
                    "stage": "transcript_done",
                    "outcome": "success",
                }
            }
        ],
    }


def test_installed_route_probe_accepts_only_the_expected_completion_event() -> None:
    client = _RuntimeLogClient(_completion_log_payload("youtube.captions.completed"))

    assert (
        wait_for_youtube_completion_event(
            client,
            execution_mode="captions-first",
            timeout_sec=0.1,
        )
        == "youtube.captions.completed"
    )


def test_installed_route_probe_rejects_opposite_completion_event() -> None:
    client = _RuntimeLogClient(_completion_log_payload("pipeline.transcription.completed"))

    with pytest.raises(WorkflowFailure) as caught:
        wait_for_youtube_completion_event(
            client,
            execution_mode="captions-first",
            timeout_sec=0.1,
        )

    assert caught.value.category == "bundled-runtime"
    assert caught.value.code == "youtube_execution_route_mismatch"


def test_installed_route_probe_rejects_truncated_log_window() -> None:
    client = _RuntimeLogClient(
        _completion_log_payload(
            "youtube.captions.completed",
            truncated=True,
        )
    )

    with pytest.raises(WorkflowFailure) as caught:
        wait_for_youtube_completion_event(
            client,
            execution_mode="captions-first",
            timeout_sec=0.1,
        )

    assert caught.value.code == "youtube_route_log_window_truncated"

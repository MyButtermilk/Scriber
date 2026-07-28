from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

CONTRACT = "scriber-installed-youtube-video-matrix-v1"
PREVET_CONTRACT = "scriber-installed-youtube-video-matrix-prevet-v1"
SCHEMA_VERSION = "1"
LANE_IDS = (
    "short-direct-audio",
    "segmented-adaptive",
    "non-ascii-metadata",
    "captions-first",
    "long-spoken-word",
)
EXECUTION_MODES = {"audio-provider", "captions-first"}
EXPECTED_COMPLETION_EVENTS = {
    "audio-provider": "pipeline.transcription.completed",
    "captions-first": "youtube.captions.completed",
}
PROBE_KINDS = {"audio-format", "caption-route"}
SELECTION_MARKERS = {"primary", "replacement"}
FAILURE_CATEGORIES = {
    "bundled-runtime",
    "extractor",
    "media-preparation",
    "provider",
    "external-availability",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MatrixContractError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixContractError(f"invalid JSON input: {path.name}") from exc
    if not isinstance(payload, dict):
        raise MatrixContractError(f"JSON input must be an object: {path.name}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_path_sha256(value: str | Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(value))).replace("\\", "/")
    return sha256_text(normalized)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, field: str) -> str:
    candidate = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(candidate):
        raise MatrixContractError(f"{field} must be a lowercase SHA-256")
    return candidate


def _require_nonempty_string(value: Any, field: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        raise MatrixContractError(f"{field} must be non-empty")
    return candidate


def validate_fixture(
    payload: dict[str, Any],
    *,
    require_installed_extraction_pre_vetted: bool = False,
) -> dict[str, Any]:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise MatrixContractError(f"schemaVersion must be {SCHEMA_VERSION}")
    if payload.get("contract") != CONTRACT:
        raise MatrixContractError(f"contract must be {CONTRACT}")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list) or len(lanes) != len(LANE_IDS):
        raise MatrixContractError(f"fixture must contain exactly {len(LANE_IDS)} lanes")

    observed_ids: list[str] = []
    observed_urls: set[str] = set()
    for index, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise MatrixContractError(f"lanes[{index}] must be an object")
        lane_id = _require_nonempty_string(lane.get("laneId"), f"lanes[{index}].laneId")
        observed_ids.append(lane_id)
        if lane.get("preVetted") is not True:
            raise MatrixContractError(f"{lane_id}.preVetted must be true")
        extraction_pre_vetted = lane.get("installedExtractionPreVetted")
        if not isinstance(extraction_pre_vetted, bool):
            raise MatrixContractError(f"{lane_id}.installedExtractionPreVetted must be a boolean")
        if require_installed_extraction_pre_vetted and not extraction_pre_vetted:
            raise MatrixContractError(
                f"{lane_id}.installedExtractionPreVetted must be true before an installed matrix run"
            )
        execution_mode = _require_nonempty_string(lane.get("executionMode"), f"{lane_id}.executionMode")
        if execution_mode not in EXECUTION_MODES:
            raise MatrixContractError(f"{lane_id}.executionMode is invalid")
        expected_completion_event = _require_nonempty_string(
            lane.get("expectedCompletionEvent"),
            f"{lane_id}.expectedCompletionEvent",
        )
        if expected_completion_event != EXPECTED_COMPLETION_EVENTS[execution_mode]:
            raise MatrixContractError(f"{lane_id}.expectedCompletionEvent does not match its execution mode")
        probe_kind = _require_nonempty_string(lane.get("probeKind"), f"{lane_id}.probeKind")
        if probe_kind not in PROBE_KINDS:
            raise MatrixContractError(f"{lane_id}.probeKind is invalid")
        if (execution_mode == "captions-first") != (probe_kind == "caption-route"):
            raise MatrixContractError(f"{lane_id}.probeKind does not match its execution mode")
        _require_nonempty_string(lane.get("expectedExtractionLane"), f"{lane_id}.expectedExtractionLane")
        observation = lane.get("expectedExtractionObservation")
        if not isinstance(observation, dict) or not observation:
            raise MatrixContractError(f"{lane_id}.expectedExtractionObservation must be a non-empty object")
        allowed_observation_fields = {
            "routeKinds",
            "selectorIndexes",
            "protocols",
            "extensions",
            "containers",
            "audioCodecPrefixes",
            "formatIds",
            "captionKinds",
            "captionFormats",
            "languagePrefixes",
            "minCueCount",
            "minTextChars",
        }
        if not set(observation).issubset(allowed_observation_fields):
            raise MatrixContractError(f"{lane_id}.expectedExtractionObservation has unknown fields")
        for field, values in observation.items():
            if field in {"minCueCount", "minTextChars"}:
                if isinstance(values, bool) or not isinstance(values, int) or values <= 0:
                    raise MatrixContractError(
                        f"{lane_id}.expectedExtractionObservation.{field} must be a positive integer"
                    )
            elif not isinstance(values, list) or not values:
                raise MatrixContractError(f"{lane_id}.expectedExtractionObservation.{field} must be a non-empty list")
        if probe_kind == "audio-format":
            forbidden = {"captionKinds", "captionFormats", "languagePrefixes", "minCueCount", "minTextChars"}
            if set(observation) & forbidden:
                raise MatrixContractError(f"{lane_id}.expectedExtractionObservation mixes caption and audio fields")
            if observation.get("routeKinds") not in (None, ["audio-format"]):
                raise MatrixContractError(f"{lane_id}.expectedExtractionObservation.routeKinds must prove audio-format")
        else:
            required_caption_fields = {"routeKinds", "captionKinds", "captionFormats", "minCueCount", "minTextChars"}
            if not required_caption_fields.issubset(observation):
                raise MatrixContractError(f"{lane_id}.expectedExtractionObservation must prove parsed caption cues")
            if observation["routeKinds"] != ["youtube-captions"]:
                raise MatrixContractError(
                    f"{lane_id}.expectedExtractionObservation.routeKinds must prove youtube-captions"
                )
            forbidden = {
                "selectorIndexes",
                "protocols",
                "extensions",
                "containers",
                "audioCodecPrefixes",
                "formatIds",
            }
            if set(observation) & forbidden:
                raise MatrixContractError(f"{lane_id}.expectedExtractionObservation mixes caption and audio fields")
        for marker in ("primary", "replacement"):
            candidate = lane.get(marker)
            if not isinstance(candidate, dict):
                raise MatrixContractError(f"{lane_id}.{marker} must be an object")
            url = _require_nonempty_string(candidate.get("url"), f"{lane_id}.{marker}.url")
            if not url.startswith("https://"):
                raise MatrixContractError(f"{lane_id}.{marker}.url must use HTTPS")
            band = candidate.get("durationBandSeconds")
            if not isinstance(band, dict):
                raise MatrixContractError(f"{lane_id}.{marker}.durationBandSeconds must be an object")
            minimum = band.get("min")
            maximum = band.get("max")
            if (
                isinstance(minimum, bool)
                or isinstance(maximum, bool)
                or not isinstance(minimum, (int, float))
                or not isinstance(maximum, (int, float))
                or minimum < 0
                or maximum <= minimum
            ):
                raise MatrixContractError(f"{lane_id}.{marker}.durationBandSeconds must define 0 <= min < max")
            url_hash = sha256_text(url)
            if url_hash in observed_urls:
                raise MatrixContractError("all primary and replacement fixture URLs must be distinct")
            observed_urls.add(url_hash)

    if tuple(observed_ids) != LANE_IDS:
        raise MatrixContractError(f"lane order and IDs must be exactly {', '.join(LANE_IDS)}")
    return payload


def _observation_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    route_kinds = expected.get("routeKinds")
    if route_kinds is not None and observed.get("routeKind") not in route_kinds:
        return False
    selector_indexes = expected.get("selectorIndexes")
    if selector_indexes is not None and observed.get("selectorIndex") not in selector_indexes:
        return False
    protocols = expected.get("protocols")
    if protocols is not None and observed.get("protocol") not in protocols:
        return False
    extensions = expected.get("extensions")
    if extensions is not None and observed.get("ext") not in extensions:
        return False
    containers = expected.get("containers")
    if containers is not None and observed.get("container") not in containers:
        return False
    format_ids = expected.get("formatIds")
    if format_ids is not None and observed.get("formatId") not in format_ids:
        return False
    caption_kinds = expected.get("captionKinds")
    if caption_kinds is not None and observed.get("captionKind") not in caption_kinds:
        return False
    caption_formats = expected.get("captionFormats")
    if caption_formats is not None and observed.get("captionFormat") not in caption_formats:
        return False
    language_prefixes = expected.get("languagePrefixes")
    if language_prefixes is not None and not any(
        str(observed.get("language") or "").casefold().startswith(str(prefix).casefold())
        for prefix in language_prefixes
    ):
        return False
    min_cue_count = expected.get("minCueCount")
    if min_cue_count is not None and (
        isinstance(observed.get("cueCount"), bool)
        or not isinstance(observed.get("cueCount"), int)
        or observed["cueCount"] < min_cue_count
    ):
        return False
    min_text_chars = expected.get("minTextChars")
    if min_text_chars is not None and (
        isinstance(observed.get("textChars"), bool)
        or not isinstance(observed.get("textChars"), int)
        or observed["textChars"] < min_text_chars
    ):
        return False
    prefixes = expected.get("audioCodecPrefixes")
    return prefixes is None or any(
        str(observed.get("acodec") or "").casefold().startswith(str(prefix).casefold()) for prefix in prefixes
    )


def validate_prevet_evidence(
    fixture: dict[str, Any],
    evidence: dict[str, Any],
    *,
    fixture_sha256: str | None = None,
    candidate_payload: Path | None = None,
) -> list[str]:
    fixture = validate_fixture(fixture)
    failures: list[str] = []
    if evidence.get("schemaVersion") != SCHEMA_VERSION or evidence.get("contract") != PREVET_CONTRACT:
        failures.append("invalid pre-vetting evidence contract")
    if evidence.get("status") != "pass":
        failures.append("pre-vetting evidence did not pass")
    if fixture_sha256 is not None and evidence.get("fixtureSha256") != fixture_sha256:
        failures.append("pre-vetting fixture SHA-256 mismatch")

    bindings = evidence.get("bindings")
    if not isinstance(bindings, dict):
        failures.append("pre-vetting bindings are missing")
        bindings = {}
    required_binding_paths = {
        "backend": Path("backend/scriber-backend.exe"),
        "quickJsWrapper": Path("backend/tools/ffmpeg/qjs.exe"),
        "quickJsEngine": Path("backend/tools/ffmpeg/qjs-engine.exe"),
        "ffmpeg": Path("backend/tools/ffmpeg/ffmpeg.exe"),
        "runtimeManifest": Path("backend/tools/ffmpeg/js-runtime-manifest.json"),
    }
    for name, relative_path in required_binding_paths.items():
        binding = bindings.get(name)
        if not isinstance(binding, dict):
            failures.append(f"pre-vetting binding {name} is missing")
            continue
        try:
            expected_hash = _require_sha256(binding.get("sha256"), f"bindings.{name}.sha256")
        except MatrixContractError as exc:
            failures.append(str(exc))
            continue
        expected_length = binding.get("length")
        if isinstance(expected_length, bool) or not isinstance(expected_length, int) or expected_length <= 0:
            failures.append(f"bindings.{name}.length must be a positive integer")
            continue
        if candidate_payload is not None:
            installed_path = candidate_payload / relative_path
            if (
                not installed_path.is_file()
                or installed_path.stat().st_size != expected_length
                or sha256_file(installed_path) != expected_hash
            ):
                failures.append(f"installed candidate binding mismatch: {name}")

    rows = evidence.get("rows")
    if not isinstance(rows, list) or len(rows) != len(LANE_IDS) * 2:
        failures.append("pre-vetting evidence must contain exactly ten candidate rows")
        rows = []
    row_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            failures.append("pre-vetting rows must be objects")
            continue
        key = (str(row.get("laneId") or ""), str(row.get("selectionMarker") or ""))
        if key in row_map:
            failures.append("pre-vetting rows must have unique lane/selection identities")
            continue
        row_map[key] = row

    for lane in fixture["lanes"]:
        lane_id = str(lane["laneId"])
        for marker in ("primary", "replacement"):
            row = row_map.get((lane_id, marker))
            if row is None:
                failures.append(f"{lane_id}/{marker}: missing pre-vetting row")
                continue
            candidate = lane[marker]
            if row.get("urlSha256") != sha256_text(str(candidate["url"])):
                failures.append(f"{lane_id}/{marker}: URL hash mismatch")
            if row.get("status") != "pass":
                failures.append(f"{lane_id}/{marker}: exact-runtime probe did not pass")
            observed = row.get("observed")
            if not isinstance(observed, dict):
                failures.append(f"{lane_id}/{marker}: observed selection is missing")
                continue
            if not _duration_in_band(observed.get("durationSeconds"), candidate["durationBandSeconds"]):
                failures.append(f"{lane_id}/{marker}: duration is outside the candidate band")
            if not _observation_matches(lane["expectedExtractionObservation"], observed):
                failures.append(f"{lane_id}/{marker}: observed route does not prove the expected extraction lane")
            if lane["probeKind"] == "audio-format":
                if observed.get("routeKind") != "audio-format":
                    failures.append(f"{lane_id}/{marker}: observed route kind is invalid")
                if not isinstance(observed.get("selectorIndex"), int) or isinstance(
                    observed.get("selectorIndex"), bool
                ):
                    failures.append(f"{lane_id}/{marker}: selector index is invalid")
                for field in ("selector", "ext", "container", "acodec", "protocol", "formatId"):
                    if not isinstance(observed.get(field), str) or not observed[field]:
                        failures.append(f"{lane_id}/{marker}: observed {field} is invalid")
            else:
                if observed.get("routeKind") != "youtube-captions":
                    failures.append(f"{lane_id}/{marker}: observed route kind is invalid")
                for field in ("captionKind", "captionFormat", "language"):
                    if not isinstance(observed.get(field), str) or not observed[field]:
                        failures.append(f"{lane_id}/{marker}: observed {field} is invalid")
                for field in ("cueCount", "textChars"):
                    if (
                        isinstance(observed.get(field), bool)
                        or not isinstance(observed.get(field), int)
                        or observed[field] <= 0
                    ):
                        failures.append(f"{lane_id}/{marker}: observed {field} is invalid")
    return failures


def load_fixture(
    path: Path,
    *,
    require_installed_extraction_pre_vetted: bool = False,
) -> dict[str, Any]:
    return validate_fixture(
        _read_json(path),
        require_installed_extraction_pre_vetted=require_installed_extraction_pre_vetted,
    )


def _duration_in_band(duration: Any, band: dict[str, Any]) -> bool:
    return isinstance(duration, (int, float)) and band["min"] <= duration <= band["max"]


def validate_lane_report(report: dict[str, Any], lane: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    lane_id = str(lane["laneId"])
    if report.get("schemaVersion") != SCHEMA_VERSION or report.get("contract") != CONTRACT:
        failures.append(f"{lane_id}: invalid report contract")
    if report.get("laneId") != lane_id:
        failures.append(f"{lane_id}: lane identity mismatch")
    marker = report.get("selectionMarker")
    if marker not in SELECTION_MARKERS:
        failures.append(f"{lane_id}: invalid selection marker")
    else:
        expected_url_hash = sha256_text(str(lane[marker]["url"]))
        if report.get("fixtureUrlSha256") != expected_url_hash:
            failures.append(f"{lane_id}: fixture URL hash mismatch")
    expected_band = lane[marker]["durationBandSeconds"] if marker in SELECTION_MARKERS else None
    if report.get("durationBandSeconds") != expected_band:
        failures.append(f"{lane_id}: duration band mismatch")
    if report.get("expectedExtractionLane") != lane["expectedExtractionLane"]:
        failures.append(f"{lane_id}: extraction lane mismatch")
    if report.get("executionMode") != lane["executionMode"]:
        failures.append(f"{lane_id}: execution mode mismatch")
    if report.get("completionEvent") != lane["expectedCompletionEvent"]:
        failures.append(f"{lane_id}: installed completion event mismatch")
    observed_extraction = report.get("observedExtraction")
    if not isinstance(observed_extraction, dict) or not _observation_matches(
        lane["expectedExtractionObservation"],
        observed_extraction,
    ):
        failures.append(f"{lane_id}: observed extraction does not prove the expected lane")
    if report.get("outcome") != "passed" or report.get("ok") is not True:
        failures.append(f"{lane_id}: lane did not pass")
    if expected_band is None or not _duration_in_band(report.get("observedDurationSeconds"), expected_band):
        failures.append(f"{lane_id}: observed duration is outside the expected band")
    if report.get("sourceAssetCleanupVerified") is not True:
        failures.append(f"{lane_id}: source asset cleanup was not verified")
    if report.get("childProcessCleanupVerified") is not True:
        failures.append(f"{lane_id}: child process cleanup was not verified")
    for field in ("installerSha256", "dataRootSha256", "fixtureUrlSha256"):
        try:
            _require_sha256(report.get(field), f"{lane_id}.{field}")
        except MatrixContractError as exc:
            failures.append(str(exc))
    try:
        _require_nonempty_string(report.get("releaseTag"), f"{lane_id}.releaseTag")
    except MatrixContractError as exc:
        failures.append(str(exc))
    category = report.get("failureCategory")
    if category is not None and category not in FAILURE_CATEGORIES:
        failures.append(f"{lane_id}: invalid failure category")
    return failures


def aggregate_lane_reports(
    fixture: dict[str, Any],
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    fixture = validate_fixture(fixture)
    failures: list[str] = []
    if len(reports) != len(LANE_IDS):
        failures.append(f"expected exactly {len(LANE_IDS)} lane reports")

    reports_by_id: dict[str, dict[str, Any]] = {}
    for report in reports:
        lane_id = str(report.get("laneId") or "")
        if not lane_id or lane_id in reports_by_id:
            failures.append("lane reports must contain unique non-empty lane IDs")
            continue
        reports_by_id[lane_id] = report

    ordered_reports: list[dict[str, Any]] = []
    for lane in fixture["lanes"]:
        lane_id = str(lane["laneId"])
        report = reports_by_id.get(lane_id)
        if report is None:
            failures.append(f"{lane_id}: missing lane report")
            continue
        failures.extend(validate_lane_report(report, lane))
        ordered_reports.append(report)

    installer_hashes = {str(item.get("installerSha256") or "") for item in ordered_reports}
    release_tags = {str(item.get("releaseTag") or "") for item in ordered_reports}
    data_root_hashes = {str(item.get("dataRootSha256") or "") for item in ordered_reports}
    fixture_url_hashes = {str(item.get("fixtureUrlSha256") or "") for item in ordered_reports}
    if len(installer_hashes) != 1:
        failures.append("all lane reports must bind the same installer SHA-256")
    if len(release_tags) != 1:
        failures.append("all lane reports must bind the same release tag")
    if len(data_root_hashes) != 1:
        failures.append("all lane reports must bind the same data-root SHA-256")
    if len(fixture_url_hashes) != len(LANE_IDS):
        failures.append("five distinct successful fixture URL hashes are required")

    def singleton(values: set[str]) -> str | None:
        return next(iter(values)) if len(values) == 1 else None

    return {
        "schemaVersion": SCHEMA_VERSION,
        "contract": CONTRACT,
        "ok": not failures,
        "releaseTag": singleton(release_tags),
        "installerSha256": singleton(installer_hashes),
        "dataRootSha256": singleton(data_root_hashes),
        "laneCount": len(ordered_reports),
        "distinctSuccessfulFixtureCount": len(fixture_url_hashes),
        "lanes": ordered_reports,
        "failures": failures,
    }


def _workflow_youtube_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return None
    for item in checks:
        if isinstance(item, dict) and item.get("workflow") == "youtube":
            return item
    failures = payload.get("failures")
    if isinstance(failures, list):
        for item in failures:
            if isinstance(item, dict) and item.get("workflow") == "youtube":
                return item
    return None


def finalize_lane_report(
    *,
    workflow_report: dict[str, Any],
    desktop_report: dict[str, Any],
    lane: dict[str, Any],
    selection_marker: str,
    attempt_index: int,
    transient_retry_used: bool,
    installer_sha256: str,
    release_tag: str,
    data_root_sha256: str,
) -> dict[str, Any]:
    if selection_marker not in SELECTION_MARKERS:
        raise MatrixContractError("selection marker must be primary or replacement")
    _require_sha256(installer_sha256, "installerSha256")
    _require_sha256(data_root_sha256, "dataRootSha256")
    _require_nonempty_string(release_tag, "releaseTag")
    result = _workflow_youtube_result(workflow_report) or {}
    expected_url_hash = sha256_text(str(lane[selection_marker]["url"]))
    identity = workflow_report.get("identity")
    if not isinstance(identity, dict):
        identity = {}
    workflow_identity_matches = (
        result.get("urlSha256") == expected_url_hash
        and result.get("laneId") == lane["laneId"]
        and result.get("selectionMarker") == selection_marker
        and result.get("expectedExtractionLane") == lane["expectedExtractionLane"]
        and identity.get("installerSha256") == installer_sha256
        and identity.get("releaseTag") == release_tag
        and identity.get("dataRootSha256") == data_root_sha256
    )
    workflow_route_matches = (
        result.get("executionMode") == lane["executionMode"]
        and result.get("completionEvent") == lane["expectedCompletionEvent"]
    )
    desktop_failed = desktop_report.get("ok") is not True
    passed = (
        result.get("ok") is True
        and workflow_report.get("ok") is True
        and workflow_identity_matches
        and workflow_route_matches
        and not desktop_failed
    )
    transcript = result.get("transcript")
    if not isinstance(transcript, dict):
        transcript = {}
    failure_category = (
        None
        if passed
        else result.get("failureCategory")
        or (
            "bundled-runtime"
            if not workflow_identity_matches or not workflow_route_matches or desktop_failed
            else "provider"
        )
    )
    if failure_category not in FAILURE_CATEGORIES:
        failure_category = "provider"
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "contract": CONTRACT,
        "laneId": lane["laneId"],
        "fixtureUrlSha256": expected_url_hash,
        "selectionMarker": selection_marker,
        "attemptIndex": attempt_index,
        "transientRetryUsed": transient_retry_used,
        "durationBandSeconds": lane[selection_marker]["durationBandSeconds"],
        "expectedExtractionLane": lane["expectedExtractionLane"],
        "executionMode": lane["executionMode"],
        "completionEvent": result.get("completionEvent"),
        "outcome": "passed" if passed else "failed",
        "ok": passed,
        "failureCategory": failure_category,
        "failureCode": (
            None
            if passed
            else str(
                result.get("failureCode")
                or (
                    "workflow_identity_mismatch"
                    if not workflow_identity_matches
                    else "workflow_route_mismatch"
                    if not workflow_route_matches
                    else "desktop_smoke_failed"
                    if desktop_failed
                    else "workflow_failed"
                )
            )
        ),
        "observedDurationSeconds": transcript.get("durationSeconds"),
        "contentChars": transcript.get("contentChars", 0),
        "summaryChars": transcript.get("summaryChars", 0),
        "elapsedMs": result.get("elapsedMs"),
        "sourceAssetCleanupVerified": result.get("sourceAssetCleanupVerified") is True,
        "childProcessCleanupVerified": desktop_report.get("cleanupVerified") is True,
        "releaseTag": release_tag,
        "installerSha256": installer_sha256,
        "dataRootSha256": data_root_sha256,
    }
    return report


def _find_lane(fixture: dict[str, Any], lane_id: str) -> dict[str, Any]:
    for lane in fixture["lanes"]:
        if lane["laneId"] == lane_id:
            return lane
    raise MatrixContractError(f"unknown lane ID: {lane_id}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and aggregate installed YouTube video matrix evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-fixture")
    validate.add_argument("--fixture", required=True)

    validate_prevet = subparsers.add_parser("validate-preflight")
    validate_prevet.add_argument("--fixture", required=True)
    validate_prevet.add_argument("--evidence", required=True)
    validate_prevet.add_argument("--candidate-payload", default="")

    path_hash = subparsers.add_parser("path-hash")
    path_hash.add_argument("--path", required=True)

    finalize = subparsers.add_parser("finalize-lane")
    finalize.add_argument("--fixture", required=True)
    finalize.add_argument("--workflow-report", required=True)
    finalize.add_argument("--desktop-report", required=True)
    finalize.add_argument("--lane-id", required=True)
    finalize.add_argument("--selection-marker", choices=sorted(SELECTION_MARKERS), required=True)
    finalize.add_argument("--attempt-index", type=int, required=True)
    finalize.add_argument("--transient-retry-used", action="store_true")
    finalize.add_argument("--installer-sha256", required=True)
    finalize.add_argument("--release-tag", required=True)
    finalize.add_argument("--data-root-sha256", required=True)
    finalize.add_argument("--output", required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--fixture", required=True)
    aggregate.add_argument("--lane-report", action="append", required=True)
    aggregate.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate-fixture":
            fixture = load_fixture(Path(args.fixture))
            print(json.dumps({"ok": True, "contract": fixture["contract"], "laneCount": len(fixture["lanes"])}))
            return 0
        if args.command == "validate-preflight":
            fixture_path = Path(args.fixture)
            fixture = load_fixture(fixture_path)
            failures = validate_prevet_evidence(
                fixture,
                _read_json(Path(args.evidence)),
                fixture_sha256=sha256_file(fixture_path),
                candidate_payload=Path(args.candidate_payload) if args.candidate_payload else None,
            )
            print(json.dumps({"ok": not failures, "failureCount": len(failures)}))
            return 0 if not failures else 2
        if args.command == "path-hash":
            print(normalized_path_sha256(args.path))
            return 0
        if args.command == "finalize-lane":
            fixture = load_fixture(Path(args.fixture))
            lane = _find_lane(fixture, args.lane_id)
            report = finalize_lane_report(
                workflow_report=_read_json(Path(args.workflow_report)),
                desktop_report=_read_json(Path(args.desktop_report)),
                lane=lane,
                selection_marker=args.selection_marker,
                attempt_index=args.attempt_index,
                transient_retry_used=args.transient_retry_used,
                installer_sha256=args.installer_sha256,
                release_tag=args.release_tag,
                data_root_sha256=args.data_root_sha256,
            )
            _write_json(Path(args.output), report)
            return 0 if report["ok"] else 2
        if args.command == "aggregate":
            fixture = load_fixture(Path(args.fixture))
            report = aggregate_lane_reports(fixture, [_read_json(Path(path)) for path in args.lane_report])
            _write_json(Path(args.output), report)
            return 0 if report["ok"] else 2
    except MatrixContractError as exc:
        print(f"matrix contract failed: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
REPORT_KIND = "scriber-meeting-release-evidence"
REPORT_GLOB = "meeting-release-evidence-*.json"

REQUIRED_COVERAGE: dict[str, tuple[str, ...]] = {
    "meetingApps": ("teams-desktop", "zoom-desktop", "google-meet-chrome"),
    "audioRoutes": (
        "laptop-speakers",
        "wired-headset",
        "bluetooth-headset",
        "usb-microphone",
        "default-device-switch",
    ),
    "audioConditions": (
        "quiet-speech",
        "background-noise",
        "remote-echo",
        "double-talk",
        "multiple-remote-speakers",
    ),
    "failureModes": (
        "network-loss",
        "provider-reconnect",
        "backend-crash",
        "shell-exit",
        "resume",
        "corrupt-chunk",
        "disk-full",
    ),
    "outlookAccounts": ("work-school", "microsoft-personal"),
    "outlookScenarios": (
        "connect",
        "reconnect",
        "token-expiry",
        "delta-pagination",
        "tenant-block",
        "offline",
    ),
    "soakScenarios": ("recording-60m", "stability-2h"),
    "validationAreas": (
        "canonical-transcript",
        "analysis-citations",
        "voiceprint-held-corpus",
        "support-bundle-privacy",
        "eu-voiceprint-privacy-review",
        "automated-regression-suite",
        "signed-release",
    ),
}

ALLOWED_COVERAGE = {key: frozenset(values) for key, values in REQUIRED_COVERAGE.items()}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SCENARIO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

SENSITIVE_KEYS = {
    "accesstoken",
    "refreshtoken",
    "sessiontoken",
    "scribertoken",
    "webhooksecret",
    "authorizationheader",
    "transcripttext",
    "rawtranscript",
    "audiodata",
    "audiobase64",
    "voiceprintblob",
    "embeddingblob",
    "rawendpointid",
}
REDACTED_MARKERS = {"[REDACTED]", "[redacted]", "<redacted>", "***REDACTED***"}


@dataclass(frozen=True)
class ReportValidation:
    scenario_id: str
    path: str
    ok: bool
    failures: list[str]
    coverage: dict[str, list[str]]
    app_version: str
    installer_sha256: str

    def to_public(self) -> dict[str, Any]:
        return {
            "scenarioId": self.scenario_id,
            "path": self.path,
            "ok": self.ok,
            "failures": self.failures,
            "coverage": self.coverage,
            "appVersion": self.app_version,
            "installerSha256": self.installer_sha256,
        }


def validate_matrix(
    *,
    input_dir: Path,
    report_paths: list[Path] | None = None,
    expected_app_version: str = "",
    require_full_matrix: bool = True,
    require_signed_installer: bool = True,
) -> dict[str, Any]:
    root = input_dir.expanduser().resolve()
    paths = _resolve_report_paths(root, report_paths)
    validated: list[ReportValidation] = []
    payloads: list[dict[str, Any]] = []
    for path in paths:
        result, payload = validate_report(
            path,
            evidence_root=root,
            require_signed_installer=require_signed_installer,
        )
        validated.append(result)
        if payload is not None:
            payloads.append(payload)

    matrix_failures: list[str] = []
    if not paths:
        matrix_failures.append(f"no evidence reports matched {REPORT_GLOB}")

    versions = sorted({item.app_version for item in validated if item.app_version})
    installer_hashes = sorted(
        {item.installer_sha256.lower() for item in validated if item.installer_sha256}
    )
    if len(versions) > 1:
        matrix_failures.append(f"reports use multiple app versions: {versions}")
    if expected_app_version and versions != [expected_app_version]:
        matrix_failures.append(
            f"reports must use expected app version {expected_app_version!r}; found {versions}"
        )
    if len(installer_hashes) > 1:
        matrix_failures.append("reports are not bound to one installer SHA-256")

    coverage = _aggregate_coverage(payloads)
    coverage_summary: dict[str, Any] = {}
    for category, required in REQUIRED_COVERAGE.items():
        found = sorted(coverage[category])
        missing = sorted(set(required) - coverage[category])
        coverage_summary[category] = {
            "required": list(required),
            "found": found,
            "missing": missing,
            "ok": not missing,
        }
        if require_full_matrix and missing:
            matrix_failures.append(f"missing {category} coverage: {', '.join(missing)}")

    acceptance_checks = _build_acceptance_checks(payloads, require_full_matrix=require_full_matrix)
    if require_full_matrix:
        matrix_failures.extend(
            f"acceptance check failed: {item['id']} ({item['detail']})"
            for item in acceptance_checks
            if not item["ok"]
        )

    ok = bool(validated) and all(item.ok for item in validated) and not matrix_failures
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "scriber-meeting-release-matrix-validation",
        "ok": ok,
        "inputDir": str(root),
        "expectedAppVersion": expected_app_version or None,
        "requireFullMatrix": bool(require_full_matrix),
        "requireSignedInstaller": bool(require_signed_installer),
        "appVersions": versions,
        "installerSha256": installer_hashes[0] if len(installer_hashes) == 1 else None,
        "reportCount": len(validated),
        "passedReportCount": sum(1 for item in validated if item.ok),
        "failedReportCount": sum(1 for item in validated if not item.ok),
        "matrixFailures": matrix_failures,
        "coverage": coverage_summary,
        "acceptanceChecks": acceptance_checks,
        "reports": [item.to_public() for item in validated],
    }


def validate_report(
    path: Path,
    *,
    evidence_root: Path,
    require_signed_installer: bool,
) -> tuple[ReportValidation, dict[str, Any] | None]:
    resolved = path.expanduser().resolve()
    failures: list[str] = []
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        result = ReportValidation(
            scenario_id=resolved.stem,
            path=str(resolved),
            ok=False,
            failures=[f"invalid JSON: {exc}"],
            coverage={key: [] for key in REQUIRED_COVERAGE},
            app_version="",
            installer_sha256="",
        )
        return result, None

    if not isinstance(payload, dict):
        failures.append("report root must be a JSON object")
        payload = {}

    scenario_id = str(payload.get("scenarioId") or "").strip()
    app_version = str(payload.get("appVersion") or "").strip()
    build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
    installer_sha256 = str(build.get("installerSha256") or "").strip()
    normalized_coverage = _normalize_coverage(payload.get("coverage"), failures)

    if payload.get("schemaVersion") != SCHEMA_VERSION:
        failures.append(f"schemaVersion must be {SCHEMA_VERSION}")
    if payload.get("kind") != REPORT_KIND:
        failures.append(f"kind must be {REPORT_KIND!r}")
    if payload.get("completed") is not True:
        failures.append("completed must be true; draft/template reports are not evidence")
    if payload.get("operatorConfirmed") is not True:
        failures.append("operatorConfirmed must be true after the real scenario is performed")
    if not SCENARIO_ID_RE.fullmatch(scenario_id):
        failures.append("scenarioId must be a 3-80 character lowercase slug")
    if not VERSION_RE.fullmatch(app_version):
        failures.append("appVersion must be a semantic version such as 0.4.35")
    if not _is_utc_timestamp(payload.get("capturedAtUtc")):
        failures.append("capturedAtUtc must be an ISO-8601 UTC timestamp")

    if build.get("installedApp") is not True:
        failures.append("build.installedApp must be true")
    if not SHA256_RE.fullmatch(installer_sha256):
        failures.append("build.installerSha256 must be a 64-character SHA-256")
    if require_signed_installer and build.get("signedInstaller") is not True:
        failures.append("build.signedInstaller must be true for release evidence")
    if require_signed_installer and build.get("authenticodeValid") is not True:
        failures.append("build.authenticodeValid must be true for release evidence")
    if require_signed_installer and build.get("updaterSignatureVerified") is not True:
        failures.append("build.updaterSignatureVerified must be true for release evidence")

    if not any(normalized_coverage.values()):
        failures.append("coverage must contain at least one supported scenario value")
    failures.extend(_find_sensitive_content(payload))
    failures.extend(_validate_artifacts(payload.get("artifacts"), resolved, evidence_root))
    failures.extend(_validate_scenario_requirements(payload, normalized_coverage))

    result = ReportValidation(
        scenario_id=scenario_id or resolved.stem,
        path=str(resolved),
        ok=not failures,
        failures=failures,
        coverage=normalized_coverage,
        app_version=app_version,
        installer_sha256=installer_sha256,
    )
    return result, payload


def _resolve_report_paths(root: Path, paths: list[Path] | None) -> list[Path]:
    if paths:
        return sorted({path.expanduser().resolve() for path in paths})
    if not root.is_dir():
        return []
    return sorted(root.glob(REPORT_GLOB))


def _normalize_coverage(value: Any, failures: list[str]) -> dict[str, list[str]]:
    normalized = {key: [] for key in REQUIRED_COVERAGE}
    if not isinstance(value, dict):
        failures.append("coverage must be a JSON object")
        return normalized
    unknown_categories = sorted(set(value) - set(REQUIRED_COVERAGE))
    if unknown_categories:
        failures.append(f"coverage contains unknown categories: {', '.join(unknown_categories)}")
    for category, allowed in ALLOWED_COVERAGE.items():
        raw = value.get(category, [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            failures.append(f"coverage.{category} must be an array of strings")
            continue
        duplicates = sorted({item for item in raw if raw.count(item) > 1})
        if duplicates:
            failures.append(f"coverage.{category} contains duplicates: {', '.join(duplicates)}")
        unknown = sorted(set(raw) - allowed)
        if unknown:
            failures.append(f"coverage.{category} contains unknown values: {', '.join(unknown)}")
        normalized[category] = sorted(set(raw) & allowed)
    return normalized


def _validate_artifacts(value: Any, report_path: Path, evidence_root: Path) -> list[str]:
    failures: list[str] = []
    if not isinstance(value, list) or not value:
        return ["artifacts must contain at least one hashed supporting file"]
    root = evidence_root.resolve()
    for index, artifact in enumerate(value):
        prefix = f"artifacts[{index}]"
        if not isinstance(artifact, dict):
            failures.append(f"{prefix} must be an object")
            continue
        kind = str(artifact.get("kind") or "").strip()
        raw_path = str(artifact.get("path") or "").strip()
        expected_hash = str(artifact.get("sha256") or "").strip().lower()
        if not kind:
            failures.append(f"{prefix}.kind must be present")
        if not raw_path:
            failures.append(f"{prefix}.path must be present")
            continue
        relative = Path(raw_path)
        if relative.is_absolute():
            failures.append(f"{prefix}.path must be relative to the evidence directory")
            continue
        artifact_path = (report_path.parent / relative).resolve()
        try:
            artifact_path.relative_to(root)
        except ValueError:
            failures.append(f"{prefix}.path escapes the evidence directory")
            continue
        if not artifact_path.is_file():
            failures.append(f"{prefix}.path does not exist: {raw_path}")
            continue
        if not SHA256_RE.fullmatch(expected_hash):
            failures.append(f"{prefix}.sha256 must be a 64-character SHA-256")
            continue
        actual_hash = _sha256_file(artifact_path)
        if actual_hash != expected_hash:
            failures.append(f"{prefix}.sha256 does not match {raw_path}")
    return failures


def _validate_scenario_requirements(
    payload: dict[str, Any], coverage: dict[str, list[str]]
) -> list[str]:
    failures: list[str] = []
    measurements = payload.get("measurements")
    checks = payload.get("checks")
    outlook_results = payload.get("outlookResults")
    if not isinstance(measurements, dict):
        failures.append("measurements must be an object")
        measurements = {}
    if not isinstance(checks, dict):
        failures.append("checks must be an object")
        checks = {}
    if not isinstance(outlook_results, dict):
        outlook_results = {}

    if coverage["meetingApps"]:
        _require_max(measurements, "captureStartLatencyMs", 3000, failures)
        _require_true(checks, "microphoneSourceActive", failures)
        _require_true(checks, "systemSourceActive", failures)
        _require_max(measurements, "liveInterimP95Ms", 2000, failures)
        _require_true(checks, "canonicalSegmentsChronological", failures)
        _require_true(checks, "canonicalSegmentsClickable", failures)
        _require_true(checks, "canonicalSegmentsAudioAligned", failures)
        _require_true(checks, "analysisSchemaValid", failures)
        _require_true(checks, "analysisCitationsValid", failures)

    conditions = set(coverage["audioConditions"])
    if "remote-echo" in conditions:
        _require_min(measurements, "aecEchoReductionDb", 0.000001, failures)
        _require_true(checks, "aecRenderReferenceActive", failures)
    if "double-talk" in conditions or "remote-echo" in conditions:
        _require_true(checks, "localDoubleTalkSpeechPreserved", failures)
    if "multiple-remote-speakers" in conditions:
        _require_true(checks, "multipleRemoteSpeakersSeparated", failures)

    routes = set(coverage["audioRoutes"])
    if "default-device-switch" in routes:
        _require_true(checks, "deviceReconnectSucceeded", failures)
        _require_equal_counts(measurements, "deviceSwitchCount", "deviceSwitchGapCount", failures)

    failures_covered = set(coverage["failureModes"])
    if failures_covered & {"network-loss", "provider-reconnect"}:
        _require_true(checks, "providerRecovered", failures)
        _require_equal_counts(measurements, "providerOutageCount", "providerReconnectGapCount", failures)
    if "backend-crash" in failures_covered:
        _require_max(measurements, "crashLostAudioSeconds", 30, failures)
        _require_true(checks, "existingChunksFinalizable", failures)
        _require_true(checks, "backendRecoverySucceeded", failures)
    if "shell-exit" in failures_covered:
        _require_true(checks, "shellCleanupSucceeded", failures)
    if "resume" in failures_covered:
        _require_true(checks, "resumeSucceeded", failures)
        _require_equal_counts(measurements, "resumeCount", "resumeGapCount", failures)
    if "corrupt-chunk" in failures_covered:
        _require_true(checks, "corruptChunkQuarantined", failures)
        _require_true(checks, "remainingChunksFinalized", failures)
        _require_min(measurements, "corruptChunkGapCount", 1, failures)
    if "disk-full" in failures_covered:
        _require_true(checks, "diskFullDetected", failures)
        _require_true(checks, "completedChunksPreserved", failures)
        _require_false(checks, "partialChunkPublished", failures)

    for scenario in coverage["outlookScenarios"]:
        if outlook_results.get(scenario) is not True:
            failures.append(f"outlookResults.{scenario} must be true")
    if coverage["outlookAccounts"] and "connect" not in coverage["outlookScenarios"]:
        _require_true(checks, "outlookAccountConnected", failures)
    if "offline" in coverage["outlookScenarios"]:
        _require_true(checks, "offlineMeetingCaptureAvailable", failures)

    soak = set(coverage["soakScenarios"])
    if "recording-60m" in soak:
        _require_min(measurements, "recordingDurationSeconds", 3600, failures)
        _require_exact(measurements, "unmarkedAudioLossCount", 0, failures)
        _require_equal_counts(
            measurements, "intentionalGapExpectedCount", "intentionalGapObservedCount", failures
        )
    if "stability-2h" in soak:
        _require_min(measurements, "stabilityDurationSeconds", 7200, failures)
        _require_true(checks, "stabilitySoakPassed", failures)

    areas = set(coverage["validationAreas"])
    if "canonical-transcript" in areas:
        _require_true(checks, "canonicalSegmentsChronological", failures)
        _require_true(checks, "canonicalSegmentsClickable", failures)
        _require_true(checks, "canonicalSegmentsAudioAligned", failures)
    if "analysis-citations" in areas:
        _require_true(checks, "analysisSchemaValid", failures)
        _require_true(checks, "analysisCitationsValid", failures)
    if "voiceprint-held-corpus" in areas:
        _require_exact(measurements, "voiceprintFalseHighConfidenceMatches", 0, failures)
        _require_true(checks, "ambiguousVoiceMatchesRemainAnonymous", failures)
    if "support-bundle-privacy" in areas:
        _require_exact(measurements, "supportBundleSensitiveFindingCount", 0, failures)
        for key in (
            "supportBundleAudioAbsent",
            "supportBundleTranscriptContentAbsent",
            "supportBundleOutlookSecretsAbsent",
            "supportBundleWebhookSecretsAbsent",
            "supportBundleVoiceprintsAbsent",
        ):
            _require_true(checks, key, failures)
    if "eu-voiceprint-privacy-review" in areas:
        _require_true(checks, "voiceprintPrivacyLegalReviewApproved", failures)
    if "automated-regression-suite" in areas:
        _require_min(measurements, "automatedTestsPassed", 1099, failures)
        _require_true(checks, "automatedRegressionSuitePassed", failures)
    if "signed-release" in areas:
        _require_true(checks, "releaseAssetsVerified", failures)
        build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
        if build.get("authenticodeValid") is not True:
            failures.append("build.authenticodeValid must be true for signed-release coverage")
        if build.get("updaterSignatureVerified") is not True:
            failures.append("build.updaterSignatureVerified must be true for signed-release coverage")
    return failures


def _build_acceptance_checks(
    payloads: list[dict[str, Any]], *, require_full_matrix: bool
) -> list[dict[str, Any]]:
    specs = (
        ("capture-start-under-3s", "measurements", "captureStartLatencyMs", lambda v: _number(v) and v <= 3000),
        ("live-interim-p95-under-2s", "measurements", "liveInterimP95Ms", lambda v: _number(v) and v <= 2000),
        ("aec-measurable-echo-reduction", "measurements", "aecEchoReductionDb", lambda v: _number(v) and v > 0),
        ("crash-loss-at-most-open-chunk", "measurements", "crashLostAudioSeconds", lambda v: _number(v) and v <= 30),
        ("no-unmarked-audio-loss", "measurements", "unmarkedAudioLossCount", lambda v: isinstance(v, int) and v == 0),
        ("no-false-high-confidence-voice-match", "measurements", "voiceprintFalseHighConfidenceMatches", lambda v: isinstance(v, int) and v == 0),
        ("support-bundle-no-sensitive-findings", "measurements", "supportBundleSensitiveFindingCount", lambda v: isinstance(v, int) and v == 0),
        ("60-minute-recording", "measurements", "recordingDurationSeconds", lambda v: _number(v) and v >= 3600),
        ("2-hour-stability-soak", "measurements", "stabilityDurationSeconds", lambda v: _number(v) and v >= 7200),
        ("existing-regression-suite", "measurements", "automatedTestsPassed", lambda v: _number(v) and v >= 1099),
    )
    checks: list[dict[str, Any]] = []
    for check_id, section, key, predicate in specs:
        values = [payload.get(section, {}).get(key) for payload in payloads if isinstance(payload.get(section), dict) and key in payload.get(section, {})]
        ok = bool(values) and all(predicate(value) for value in values)
        detail = f"{len(values)} measurement(s) present"
        if not values and not require_full_matrix:
            ok = True
            detail = "not present in partial matrix"
        checks.append({"id": check_id, "ok": ok, "detail": detail, "values": values})
    return checks


def _aggregate_coverage(payloads: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    result = {key: set() for key in REQUIRED_COVERAGE}
    for payload in payloads:
        coverage = payload.get("coverage")
        if not isinstance(coverage, dict):
            continue
        for category, allowed in ALLOWED_COVERAGE.items():
            values = coverage.get(category)
            if isinstance(values, list):
                result[category].update(item for item in values if isinstance(item, str) and item in allowed)
    return result


def _find_sensitive_content(value: Any) -> list[str]:
    failures: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key)
                child = f"{path}.{key_text}" if path else key_text
                normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
                if normalized_key in SENSITIVE_KEYS and item not in (None, "", *REDACTED_MARKERS):
                    failures.append(f"report contains forbidden sensitive field at {child}")
                walk(item, child)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            lowered = node.lower().replace("/", "\\")
            if "\\.\\pipe\\scriber-" in lowered:
                failures.append(f"report contains a raw Scriber pipe name at {path}")
            if "swd\\mmdevapi\\" in lowered or "swd#mmdevapi#" in lowered:
                failures.append(f"report contains a raw native endpoint ID at {path}")
            if re.search(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", node, re.IGNORECASE):
                failures.append(f"report contains a bearer token at {path}")

    walk(value, "")
    return failures


def _require_true(section: dict[str, Any], key: str, failures: list[str]) -> None:
    if section.get(key) is not True:
        failures.append(f"checks.{key} must be true")


def _require_false(section: dict[str, Any], key: str, failures: list[str]) -> None:
    if section.get(key) is not False:
        failures.append(f"checks.{key} must be false")


def _require_max(section: dict[str, Any], key: str, maximum: float, failures: list[str]) -> None:
    value = section.get(key)
    if not _number(value) or value > maximum:
        failures.append(f"measurements.{key} must be a number <= {maximum:g}")


def _require_min(section: dict[str, Any], key: str, minimum: float, failures: list[str]) -> None:
    value = section.get(key)
    if not _number(value) or value < minimum:
        failures.append(f"measurements.{key} must be a number >= {minimum:g}")


def _require_exact(section: dict[str, Any], key: str, expected: int, failures: list[str]) -> None:
    if section.get(key) != expected:
        failures.append(f"measurements.{key} must equal {expected}")


def _require_equal_counts(
    section: dict[str, Any], expected_key: str, observed_key: str, failures: list[str]
) -> None:
    expected = section.get(expected_key)
    observed = section.get(observed_key)
    if not isinstance(expected, int) or expected < 1:
        failures.append(f"measurements.{expected_key} must be an integer >= 1")
    if not isinstance(observed, int) or observed != expected:
        failures.append(f"measurements.{observed_key} must equal measurements.{expected_key}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate redacted real-world Meeting workspace release evidence.",
    )
    parser.add_argument("--input-dir", default="tmp/meeting-release-matrix")
    parser.add_argument("--report", action="append", default=[])
    parser.add_argument("--expected-app-version", default="")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--allow-unsigned-installer", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def write_output(payload: dict[str, Any], path: str) -> None:
    if not path:
        return
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    input_dir = Path(args.input_dir).expanduser().resolve()
    report_paths = [Path(item) for item in args.report] or None
    payload = validate_matrix(
        input_dir=input_dir,
        report_paths=report_paths,
        expected_app_version=str(args.expected_app_version or ""),
        require_full_matrix=not bool(args.allow_partial),
        require_signed_installer=not bool(args.allow_unsigned_installer),
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

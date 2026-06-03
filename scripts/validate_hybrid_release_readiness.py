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
        "requiredDependencies": ["scipy", "onnxruntime"],
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
    total_mb = summary.get("totalMb")
    if not isinstance(total_mb, (int, float)) or total_mb <= 0:
        failures.append("runtime dependency footprint summary.totalMb must be positive")

    dependencies = report.get("dependencies")
    if not isinstance(dependencies, dict):
        failures.append("runtime dependency footprint dependencies must be an object")
        dependencies = {}
    for dependency_name in details["requiredDependencies"]:
        dependency = dependencies.get(dependency_name)
        if not isinstance(dependency, dict):
            failures.append(f"runtime dependency footprint is missing dependency: {dependency_name}")
            continue
        if dependency.get("name") != dependency_name:
            failures.append(f"runtime dependency {dependency_name} name is invalid")
        if dependency.get("totalMb", 0) <= 0:
            failures.append(f"runtime dependency {dependency_name} totalMb must be positive")
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

    return ReadinessCheck("runtimeDependencyFootprint", not failures, failures, details)


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
        expected_authenticode_publisher=args.expected_authenticode_publisher,
        require_authenticode_timestamp=args.require_authenticode_timestamp,
        platform=args.platform,
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

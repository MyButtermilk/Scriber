from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from scripts.installed_youtube_video_matrix import (
        PREVET_CONTRACT,
        SCHEMA_VERSION,
        load_fixture,
        sha256_file,
        sha256_text,
        validate_prevet_evidence,
    )
    from scripts.smoke_quickjs_youtube_runtime import (
        FROZEN_PROBE_CONTRACT,
        PROBE_POLICY,
        _candidate_bundle,
        _canonical_json_bytes,
        _file_identity,
        _parse_video_id,
        _probe_environment,
        _run_bounded,
    )
except ModuleNotFoundError:
    from installed_youtube_video_matrix import (
        PREVET_CONTRACT,
        SCHEMA_VERSION,
        load_fixture,
        sha256_file,
        sha256_text,
        validate_prevet_evidence,
    )
    from smoke_quickjs_youtube_runtime import (
        FROZEN_PROBE_CONTRACT,
        PROBE_POLICY,
        _candidate_bundle,
        _canonical_json_bytes,
        _file_identity,
        _parse_video_id,
        _probe_environment,
        _run_bounded,
    )

ALLOWED_FAILURE_CODES = {
    "cleanup-unproven",
    "exact-runtime-probe-failed",
    "invalid-probe-response",
    "selected-caption-missing",
    "selected-format-missing",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _request(
    *,
    lane_id: str,
    marker: str,
    url: str,
    runtime: Path,
    probe_kind: str,
) -> bytes:
    request = {
        "requestContract": FROZEN_PROBE_CONTRACT,
        "schemaVersion": 1,
        "caseId": f"{lane_id}-{marker}",
        "family": lane_id,
        "url": url,
        "expectedVideoId": _parse_video_id(url),
        "runtimeKind": "quickjs",
        "runtimePath": str(runtime),
        "cacheMode": "cold",
    }
    if probe_kind == "audio-format":
        request["formatSelectionProbe"] = True
    elif probe_kind == "caption-route":
        request["captionSelectionProbe"] = True
    else:
        raise ValueError("unsupported installed matrix probe kind")
    return _canonical_json_bytes(request)


def _failed_row(lane_id: str, marker: str, url: str, code: str) -> dict[str, Any]:
    return {
        "laneId": lane_id,
        "selectionMarker": marker,
        "urlSha256": sha256_text(url),
        "status": "fail",
        "failureCode": code if code in ALLOWED_FAILURE_CODES else "invalid-probe-response",
        "observed": None,
    }


def _parse_row(
    *,
    lane_id: str,
    marker: str,
    url: str,
    result: Any,
    probe_kind: str,
) -> dict[str, Any]:
    if not result.cleanup_verified:
        return _failed_row(lane_id, marker, url, "cleanup-unproven")
    if result.status != "completed" or result.return_code != 0:
        return _failed_row(lane_id, marker, url, "exact-runtime-probe-failed")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return _failed_row(lane_id, marker, url, "invalid-probe-response")
    selected_field = "selectedFormat" if probe_kind == "audio-format" else "selectedCaption"
    selected = payload.get(selected_field) if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("probeContract") != FROZEN_PROBE_CONTRACT
        or payload.get("schemaVersion") != 1
        or payload.get("status") != "pass"
        or payload.get("policy") != PROBE_POLICY
        or not isinstance(selected, dict)
    ):
        return _failed_row(
            lane_id,
            marker,
            url,
            "selected-format-missing" if probe_kind == "audio-format" else "selected-caption-missing",
        )
    if probe_kind == "audio-format":
        observed = {
            "durationSeconds": selected.get("durationSeconds"),
            "routeKind": selected.get("routeKind"),
            "selectorIndex": selected.get("selectorIndex"),
            "selector": selected.get("selector"),
            "ext": selected.get("ext"),
            "container": selected.get("container"),
            "acodec": selected.get("acodec"),
            "protocol": selected.get("protocol"),
            "formatId": selected.get("formatId"),
        }
    else:
        observed = {
            "durationSeconds": selected.get("durationSeconds"),
            "routeKind": selected.get("routeKind"),
            "captionKind": selected.get("captionKind"),
            "captionFormat": selected.get("captionFormat"),
            "language": selected.get("language"),
            "cueCount": selected.get("cueCount"),
            "textChars": selected.get("textChars"),
        }
    return {
        "laneId": lane_id,
        "selectionMarker": marker,
        "urlSha256": sha256_text(url),
        "status": "pass",
        "failureCode": None,
        "observed": observed,
    }


def collect(args: argparse.Namespace) -> dict[str, Any]:
    fixture_path = Path(args.fixture).resolve()
    fixture = load_fixture(fixture_path)
    bundle = _candidate_bundle(Path(args.candidate_payload), Path(args.runtime))
    ffmpeg = bundle.root / "backend" / "tools" / "ffmpeg" / "ffmpeg.exe"
    if not ffmpeg.is_file():
        raise ValueError("candidate ffmpeg is missing")
    output = Path(args.output).resolve()
    try:
        output.relative_to(bundle.root)
    except ValueError:
        pass
    else:
        raise ValueError("pre-vetting output must remain outside the candidate payload")

    bindings = {
        "backend": bundle.identities["backend"].public(),
        "quickJsWrapper": bundle.identities["wrapper"].public(),
        "quickJsEngine": bundle.identities["engine"].public(),
        "ffmpeg": _file_identity(ffmpeg).public(),
        "runtimeManifest": bundle.identities["manifest"].public(),
    }
    rows: list[dict[str, Any]] = []
    scratch_parent = Path(args.scratch_root).resolve() if args.scratch_root else None
    if scratch_parent is not None and not scratch_parent.is_dir():
        raise ValueError("scratch root must be an existing directory")
    with tempfile.TemporaryDirectory(
        prefix="scriber-youtube-matrix-prevet-",
        dir=str(scratch_parent) if scratch_parent is not None else None,
    ) as temporary_root:
        root = Path(temporary_root)
        for lane in fixture["lanes"]:
            for marker in ("primary", "replacement"):
                candidate = lane[marker]
                workspace = root / f"{lane['laneId']}-{marker}"
                workspace.mkdir(mode=0o700, exist_ok=False)
                result = _run_bounded(
                    [str(bundle.backend), "--installer-youtube-holdout-probe"],
                    cwd=workspace,
                    env=_probe_environment(workspace),
                    stdin_bytes=_request(
                        lane_id=str(lane["laneId"]),
                        marker=marker,
                        url=str(candidate["url"]),
                        runtime=bundle.runtime,
                        probe_kind=str(lane["probeKind"]),
                    ),
                    timeout_seconds=float(args.timeout_seconds),
                )
                rows.append(
                    _parse_row(
                        lane_id=str(lane["laneId"]),
                        marker=marker,
                        url=str(candidate["url"]),
                        result=result,
                        probe_kind=str(lane["probeKind"]),
                    )
                )

    evidence = {
        "schemaVersion": SCHEMA_VERSION,
        "contract": PREVET_CONTRACT,
        "status": "pass",
        "fixtureSha256": sha256_file(fixture_path),
        "bindings": bindings,
        "executionPolicy": {
            "frozenBackendProbe": FROZEN_PROBE_CONTRACT,
            "audioFormatSelectors": "src.youtube_download._FORMAT_SELECTORS",
            "captionRoute": "src.youtube_download timed caption selection and parser",
            "mediaDownload": False,
            "captionPayloadFetch": True,
            "remoteComponents": False,
            "externalPlugins": False,
            "sequential": True,
            "timeoutSecondsPerCandidate": args.timeout_seconds,
        },
        "rows": rows,
    }
    failures = validate_prevet_evidence(
        fixture,
        evidence,
        fixture_sha256=sha256_file(fixture_path),
        candidate_payload=bundle.root,
    )
    if failures:
        evidence["status"] = "fail"
        evidence["failureCount"] = len(failures)
    else:
        evidence["failureCount"] = 0
    return evidence


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pre-vet the installed five-lane YouTube fixture.")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--candidate-payload", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scratch-root", default="")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not 10 <= args.timeout_seconds <= 300:
        print("pre-vetting timeout is outside the bounded range", file=sys.stderr)
        return 2
    try:
        payload = collect(args)
        _write_json(Path(args.output), payload)
    except Exception as exc:
        print(f"pre-vetting failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps({"status": payload["status"], "failureCount": payload["failureCount"]}))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

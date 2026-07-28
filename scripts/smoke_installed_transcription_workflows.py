from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_YOUTUBE_URL = "https://www.youtube.com/watch?v=0wEjbSYNUM8"
DEFAULT_FILE_TEXT = (
    "Scriber Workflow Test. Diese Audiodatei prueft die installierte Datei Transkription und Zusammenfassung."
)
FAILURE_PATTERNS = {
    "external-availability": (
        "age-restricted",
        "age restricted",
        "copyright",
        "forbidden",
        "geo-restricted",
        "geoblocked",
        "not available",
        "not available in your country",
        "private video",
        "removed",
        "sign in to confirm",
        "unavailable",
        "http 403",
        "http 404",
    ),
    "bundled-runtime": (
        "ejs",
        "javascript runtime",
        "quickjs",
        "qjs",
        "runtime manifest",
        "signature solving",
    ),
    "media-preparation": (
        "audio preparation",
        "codec",
        "container",
        "ffmpeg",
        "ffprobe",
        "media preparation",
    ),
    "extractor": (
        "download failed",
        "extractor",
        "format is not available",
        "requested format",
        "yt-dlp",
        "youtube download",
    ),
}
YOUTUBE_COMPLETION_EVENTS = {
    "audio-provider": "pipeline.transcription.completed",
    "captions-first": "youtube.captions.completed",
}


class WorkflowFailure(RuntimeError):
    def __init__(self, category: str, code: str) -> None:
        super().__init__(code)
        self.category = category
        self.code = code


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_path_sha256(value: str | Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(value))).replace("\\", "/")
    return sha256_text(normalized)


def classify_failure(value: Any) -> str:
    text = str(value or "").casefold()
    for category, patterns in FAILURE_PATTERNS.items():
        if not any(pattern in text for pattern in patterns):
            continue
        if category == "external-availability" and not any(
            boundary in text for boundary in ("download", "extractor", "video", "youtube")
        ):
            continue
        return category
    return "provider"


def parse_duration_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3) if value >= 0 else None
    parts = str(value or "").strip().split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return round(seconds, 3)


def inventory_source_assets(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    inventory: set[str] = set()
    try:
        for path in root.rglob("*"):
            inventory.add(os.path.normcase(str(path.resolve())))
    except OSError:
        return set()
    return inventory


def wait_for_source_asset_cleanup(root: Path, baseline: set[str], timeout_sec: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not (inventory_source_assets(root) - baseline):
            return True
        time.sleep(0.5)
    return not (inventory_source_assets(root) - baseline)


def ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class HttpClient:
    def __init__(self, base_url: str, token: str = "", timeout_sec: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_sec = timeout_sec

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.token:
            headers["X-Scriber-Token"] = self.token
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        data = None
        request_headers = self._headers(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec or self.timeout_sec) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc
        if not body:
            return {}
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{method} {path} did not return a JSON object")
        return decoded

    def upload_file(self, path: str, file_path: Path, *, timeout_sec: float | None = None) -> dict[str, Any]:
        boundary = f"scriber-smoke-{uuid.uuid4().hex}"
        file_bytes = file_path.read_bytes()
        filename = file_path.name
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode()
        )
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        request = urllib.request.Request(
            self.base_url + path,
            data=bytes(body),
            headers=self._headers(
                {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                }
            ),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec or self.timeout_sec) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"POST {path} failed with HTTP {exc.code}: {body_text}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"POST {path} did not return a JSON object")
        return payload


def synthesize_speech_wav(target: Path, text: str) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join(
        [
            "Add-Type -AssemblyName System.Speech",
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
            "$synth.Rate = 0",
            "$synth.Volume = 100",
            f"$synth.SetOutputToWaveFile({ps_single_quote(str(target))})",
            f"$synth.Speak({ps_single_quote(text)})",
            "$synth.SetOutputToNull()",
            "$synth.Dispose()",
        ]
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Windows SAPI speech synthesis failed: {completed.stderr.strip()}")
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError(f"Windows SAPI did not create a non-empty WAV file: {target}")
    return {
        "sizeBytes": target.stat().st_size,
        "textChars": len(text),
    }


def transcript_summary(detail: dict[str, Any]) -> dict[str, Any]:
    content = str(detail.get("content") or "")
    summary = str(detail.get("summary") or "")
    return {
        "type": str(detail.get("type") or ""),
        "status": str(detail.get("status") or ""),
        "durationSeconds": parse_duration_seconds(detail.get("duration")),
        "summaryStatus": str(detail.get("summaryStatus") or ""),
        "contentChars": len(content.strip()),
        "summaryChars": len(summary.strip()),
    }


def wait_for_workflow(
    client: HttpClient,
    transcript_id: str,
    *,
    timeout_sec: float,
    poll_sec: float,
    min_content_chars: int,
    min_summary_chars: int,
    require_summary: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    summarize_attempted = False
    last_detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_detail = client.request_json("GET", f"/api/transcripts/{urllib.parse.quote(transcript_id)}")
        status = str(last_detail.get("status") or "").lower()
        content = str(last_detail.get("content") or "").strip()
        summary = str(last_detail.get("summary") or "").strip()
        summary_status = str(last_detail.get("summaryStatus") or "").lower()

        if status == "failed":
            failure_context = " ".join(
                str(last_detail.get(field) or "") for field in ("step", "error", "message", "content", "summaryError")
            )
            raise WorkflowFailure(classify_failure(failure_context), "transcript_failed")

        content_ready = status == "completed" and len(content) >= min_content_chars
        if content_ready and not require_summary:
            return last_detail

        if content_ready and require_summary:
            if summary_status == "completed" and len(summary) >= min_summary_chars:
                return last_detail
            if not summarize_attempted and summary_status not in {"pending", "completed"}:
                summarize_attempted = True
                client.request_json(
                    "POST", f"/api/transcripts/{urllib.parse.quote(transcript_id)}/summarize", timeout_sec=timeout_sec
                )

        time.sleep(max(0.5, poll_sec))

    raise WorkflowFailure("provider", "workflow_timeout")


def wait_for_youtube_completion_event(
    client: HttpClient,
    *,
    execution_mode: str,
    timeout_sec: float = 15.0,
    poll_sec: float = 0.5,
) -> str:
    expected_event = YOUTUBE_COMPLETION_EVENTS.get(execution_mode)
    if expected_event is None:
        raise WorkflowFailure("bundled-runtime", "invalid_youtube_execution_mode")
    completion_events = set(YOUTUBE_COMPLETION_EVENTS.values())
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        payload = client.request_json("GET", "/api/runtime/logs?limit=2000")
        if payload.get("truncated") is True:
            raise WorkflowFailure("bundled-runtime", "youtube_route_log_window_truncated")
        items = payload.get("items")
        if not isinstance(items, list):
            raise WorkflowFailure("bundled-runtime", "youtube_route_log_payload_invalid")
        observed: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            context = item.get("context")
            if not isinstance(context, dict):
                continue
            event = context.get("event")
            if (
                event in completion_events
                and context.get("workflow") == "youtube"
                and context.get("stage") == "transcript_done"
                and context.get("outcome") == "success"
            ):
                observed.add(str(event))
        unexpected = observed - {expected_event}
        if unexpected:
            raise WorkflowFailure("bundled-runtime", "youtube_execution_route_mismatch")
        if expected_event in observed:
            return expected_event
        time.sleep(max(0.1, poll_sec))
    raise WorkflowFailure("bundled-runtime", "youtube_completion_event_missing")


def run_file_workflow(client: HttpClient, args: argparse.Namespace, work_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    wav_path = work_dir / "scriber-installed-file-workflow.wav"
    generated = synthesize_speech_wav(wav_path, args.file_text)
    response = client.upload_file(
        "/api/file/transcribe",
        wav_path,
        timeout_sec=args.request_timeout_sec,
    )
    transcript_id = str(response.get("id") or "")
    if not transcript_id:
        raise RuntimeError(f"File workflow did not return a transcript id: {response}")
    detail = wait_for_workflow(
        client,
        transcript_id,
        timeout_sec=args.file_timeout_sec,
        poll_sec=args.poll_sec,
        min_content_chars=args.min_content_chars,
        min_summary_chars=args.min_summary_chars,
        require_summary=args.require_summary,
    )
    return {
        "ok": True,
        "workflow": "file",
        "elapsedMs": round((time.monotonic() - started) * 1000, 3),
        "generatedAudio": generated,
        "transcript": transcript_summary(detail),
    }


def run_youtube_workflow(
    client: HttpClient,
    args: argparse.Namespace,
    runtime_data_dir: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    source_assets_root = runtime_data_dir / "downloads" / "youtube"
    baseline_source_assets = inventory_source_assets(source_assets_root)
    execution_mode = str(args.youtube_execution_mode or "")
    request_payload: dict[str, Any] = {
        "url": args.youtube_url,
        "title": args.youtube_title,
        "channelTitle": "Installed workflow smoke",
        "duration": "--:--",
    }
    if execution_mode:
        cleared = client.request_json("DELETE", "/api/runtime/logs")
        if cleared.get("ok") is not True:
            raise WorkflowFailure("bundled-runtime", "runtime_log_clear_failed")
        request_payload["preferCaptions"] = execution_mode == "captions-first"
    response = client.request_json(
        "POST",
        "/api/youtube/transcribe",
        payload=request_payload,
        timeout_sec=args.request_timeout_sec,
    )
    transcript_id = str(response.get("id") or "")
    if not transcript_id:
        raise RuntimeError(f"YouTube workflow did not return a transcript id: {response}")
    detail = wait_for_workflow(
        client,
        transcript_id,
        timeout_sec=args.youtube_timeout_sec,
        poll_sec=args.poll_sec,
        min_content_chars=args.min_content_chars,
        min_summary_chars=args.min_summary_chars,
        require_summary=args.require_summary,
    )
    transcript = transcript_summary(detail)
    completion_event = (
        wait_for_youtube_completion_event(
            client,
            execution_mode=execution_mode,
        )
        if execution_mode
        else None
    )
    duration_seconds = transcript["durationSeconds"]
    if args.youtube_duration_min >= 0 and (duration_seconds is None or duration_seconds < args.youtube_duration_min):
        raise WorkflowFailure("extractor", "duration_below_expected_band")
    if args.youtube_duration_max >= 0 and (duration_seconds is None or duration_seconds > args.youtube_duration_max):
        raise WorkflowFailure("extractor", "duration_above_expected_band")
    cleanup_verified = wait_for_source_asset_cleanup(
        source_assets_root,
        baseline_source_assets,
        timeout_sec=args.source_cleanup_timeout_sec,
    )
    if not cleanup_verified:
        raise WorkflowFailure("media-preparation", "source_asset_cleanup_failed")
    return {
        "ok": True,
        "workflow": "youtube",
        "elapsedMs": round((time.monotonic() - started) * 1000, 3),
        "urlSha256": sha256_text(args.youtube_url),
        "laneId": args.youtube_lane_id or None,
        "selectionMarker": args.youtube_selection_marker or None,
        "expectedExtractionLane": args.youtube_expected_extraction_lane or None,
        "executionMode": execution_mode or None,
        "completionEvent": completion_event,
        "sourceAssetCleanupVerified": cleanup_verified,
        "transcript": transcript,
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    token = os.getenv(args.token_env or "") if args.token_env else ""
    client = HttpClient(args.base_url, token=token or "", timeout_sec=args.request_timeout_sec)
    started = time.monotonic()
    output_path = Path(args.output).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else output_path.parent / "installed-workflow-media"
    work_dir.mkdir(parents=True, exist_ok=True)

    runtime = client.request_json("GET", "/api/runtime", timeout_sec=args.request_timeout_sec)
    runtime_data_dir = Path(str(runtime.get("dataDir") or "")).resolve()
    runtime_data_root_sha256 = normalized_path_sha256(runtime_data_dir)
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    if args.expected_data_root_sha256 and runtime_data_root_sha256 != args.expected_data_root_sha256:
        failures.append(
            {
                "workflow": "identity",
                "ok": False,
                "failureCategory": "bundled-runtime",
                "failureCode": "data_root_identity_mismatch",
            }
        )

    for name, runner in (
        ("file", lambda: run_file_workflow(client, args, work_dir)),
        ("youtube", lambda: run_youtube_workflow(client, args, runtime_data_dir)),
    ):
        if failures:
            break
        if name == "file" and args.skip_file:
            continue
        if name == "youtube" and args.skip_youtube:
            continue
        try:
            checks.append(runner())
        except WorkflowFailure as exc:
            failures.append(
                {
                    "workflow": name,
                    "ok": False,
                    "failureCategory": exc.category,
                    "failureCode": exc.code,
                }
            )
        except Exception as exc:
            failures.append(
                {
                    "workflow": name,
                    "ok": False,
                    "failureCategory": classify_failure(exc),
                    "failureCode": type(exc).__name__,
                }
            )

    payload = {
        "apiVersion": "1",
        "ok": not failures and bool(checks),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": {
            "runtimeMode": runtime.get("runtimeMode"),
            "launchKind": runtime.get("launchKind"),
            "dataRootSha256": runtime_data_root_sha256,
        },
        "identity": {
            "releaseTag": args.release_tag or None,
            "installerSha256": args.installer_sha256 or None,
            "dataRootSha256": args.expected_data_root_sha256 or runtime_data_root_sha256,
        },
        "requireSummary": bool(args.require_summary),
        "checks": checks,
        "failures": failures,
        "summary": {
            "totalChecks": len(checks) + len(failures),
            "passedChecks": len(checks),
            "failedChecks": len(failures),
            "durationMs": round((time.monotonic() - started) * 1000, 3),
        },
    }
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke installed Scriber file and YouTube transcription workflows.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-env", default="SCRIBER_SMOKE_SESSION_TOKEN")
    parser.add_argument("--output", default="tmp/installed-transcription-workflows-smoke.json")
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--youtube-url", default=DEFAULT_YOUTUBE_URL)
    parser.add_argument("--youtube-title", default="Installed YouTube workflow smoke")
    parser.add_argument("--youtube-lane-id", default="")
    parser.add_argument("--youtube-selection-marker", choices=("", "primary", "replacement"), default="")
    parser.add_argument("--youtube-expected-extraction-lane", default="")
    parser.add_argument("--youtube-execution-mode", choices=("", *YOUTUBE_COMPLETION_EVENTS), default="")
    parser.add_argument("--youtube-duration-min", type=float, default=-1)
    parser.add_argument("--youtube-duration-max", type=float, default=-1)
    parser.add_argument("--release-tag", default="")
    parser.add_argument("--installer-sha256", default="")
    parser.add_argument("--expected-data-root-sha256", default="")
    parser.add_argument("--file-text", default=DEFAULT_FILE_TEXT)
    parser.add_argument("--request-timeout-sec", type=float, default=60.0)
    parser.add_argument("--file-timeout-sec", type=float, default=240.0)
    parser.add_argument("--youtube-timeout-sec", type=float, default=420.0)
    parser.add_argument("--poll-sec", type=float, default=3.0)
    parser.add_argument("--source-cleanup-timeout-sec", type=float, default=30.0)
    parser.add_argument("--min-content-chars", type=int, default=20)
    parser.add_argument("--min-summary-chars", type=int, default=20)
    parser.add_argument("--skip-file", action="store_true")
    parser.add_argument("--skip-youtube", action="store_true")
    parser.add_argument("--no-require-summary", dest="require_summary", action="store_false")
    parser.set_defaults(require_summary=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    payload = run_smoke(args)
    output_path = Path(args.output).resolve()
    write_json(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

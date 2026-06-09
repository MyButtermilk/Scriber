from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_YOUTUBE_URL = "https://www.youtube.com/watch?v=0wEjbSYNUM8"
DEFAULT_FILE_TEXT = (
    "Scriber Workflow Test. Diese Audiodatei prueft die installierte "
    "Datei Transkription und Zusammenfassung."
)


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
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Windows SAPI speech synthesis failed: {completed.stderr.strip()}")
    if not target.exists() or target.stat().st_size <= 0:
        raise RuntimeError(f"Windows SAPI did not create a non-empty WAV file: {target}")
    return {
        "path": str(target),
        "sizeBytes": target.stat().st_size,
        "textChars": len(text),
    }


def transcript_summary(detail: dict[str, Any]) -> dict[str, Any]:
    content = str(detail.get("content") or "")
    summary = str(detail.get("summary") or "")
    return {
        "id": str(detail.get("id") or ""),
        "title": str(detail.get("title") or ""),
        "type": str(detail.get("type") or ""),
        "status": str(detail.get("status") or ""),
        "step": str(detail.get("step") or ""),
        "duration": str(detail.get("duration") or ""),
        "summaryStatus": str(detail.get("summaryStatus") or ""),
        "contentChars": len(content.strip()),
        "summaryChars": len(summary.strip()),
        "sourceUrl": str(detail.get("sourceUrl") or ""),
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
            raise RuntimeError(f"Transcript {transcript_id} failed: {transcript_summary(last_detail)}")

        content_ready = status == "completed" and len(content) >= min_content_chars
        if content_ready and not require_summary:
            return last_detail

        if content_ready and require_summary:
            if summary_status == "completed" and len(summary) >= min_summary_chars:
                return last_detail
            if not summarize_attempted and summary_status not in {"pending", "completed"}:
                summarize_attempted = True
                client.request_json("POST", f"/api/transcripts/{urllib.parse.quote(transcript_id)}/summarize", timeout_sec=timeout_sec)

        time.sleep(max(0.5, poll_sec))

    raise TimeoutError(f"Transcript {transcript_id} did not complete in {timeout_sec}s. Last detail: {transcript_summary(last_detail)}")


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


def run_youtube_workflow(client: HttpClient, args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    response = client.request_json(
        "POST",
        "/api/youtube/transcribe",
        payload={
            "url": args.youtube_url,
            "title": args.youtube_title,
            "channelTitle": "Installed workflow smoke",
            "duration": "--:--",
        },
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
    return {
        "ok": True,
        "workflow": "youtube",
        "elapsedMs": round((time.monotonic() - started) * 1000, 3),
        "youtubeUrl": args.youtube_url,
        "transcript": transcript_summary(detail),
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    token = os.getenv(args.token_env or "") if args.token_env else ""
    client = HttpClient(args.base_url, token=token or "", timeout_sec=args.request_timeout_sec)
    started = time.monotonic()
    output_path = Path(args.output).resolve()
    work_dir = Path(args.work_dir).resolve() if args.work_dir else output_path.parent / "installed-workflow-media"
    work_dir.mkdir(parents=True, exist_ok=True)

    runtime = client.request_json("GET", "/api/runtime", timeout_sec=args.request_timeout_sec)
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for name, runner in (
        ("file", lambda: run_file_workflow(client, args, work_dir)),
        ("youtube", lambda: run_youtube_workflow(client, args)),
    ):
        if name == "file" and args.skip_file:
            continue
        if name == "youtube" and args.skip_youtube:
            continue
        try:
            checks.append(runner())
        except Exception as exc:
            failures.append({"workflow": name, "ok": False, "error": str(exc)})

    payload = {
        "apiVersion": "1",
        "ok": not failures and bool(checks),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url.rstrip("/"),
        "runtime": {
            "runtimeMode": runtime.get("runtimeMode"),
            "launchKind": runtime.get("launchKind"),
            "dataDir": runtime.get("dataDir"),
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
    parser.add_argument("--file-text", default=DEFAULT_FILE_TEXT)
    parser.add_argument("--request-timeout-sec", type=float, default=60.0)
    parser.add_argument("--file-timeout-sec", type=float, default=240.0)
    parser.add_argument("--youtube-timeout-sec", type=float, default=420.0)
    parser.add_argument("--poll-sec", type=float, default=3.0)
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

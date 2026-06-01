from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SEGMENTS_BY_REQUIREMENT = {
    "hotkey_to_recording_state": "hotkey_received_to_mic_ready_ms",
    "hotkey_to_first_audio_frame": "hotkey_received_to_first_audio_frame_ms",
    "stop_to_text_injection": "stop_requested_to_first_paste_ms",
}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(((pct / 100.0) * len(ordered) + 0.999999) - 1)))
    return float(ordered[idx])


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "meanMs": 0.0, "p50Ms": 0.0, "p95Ms": 0.0, "maxMs": 0.0}
    return {
        "count": len(values),
        "meanMs": round(statistics.fmean(values), 3),
        "p50Ms": round(percentile(values, 50.0), 3),
        "p95Ms": round(percentile(values, 95.0), 3),
        "maxMs": round(max(values), 3),
    }


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class BackendClient:
    def __init__(self, base_url: str, token: str = "", timeout_sec: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout_sec = timeout_sec

    def request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Scriber-Token"] = self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload or "{}")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise HttpError(exc.code, payload) from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request_json("GET", path)

    def post(self, path: str) -> dict[str, Any]:
        return self.request_json("POST", path, {})


def wait_for_state(
    client: BackendClient,
    predicate: Any,
    *,
    timeout_sec: float,
    poll_interval_sec: float = 0.2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = client.get("/api/state")
        if predicate(last_state):
            return last_state
        if last_state.get("recordingState") == "failed":
            return last_state
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"Timed out waiting for state. Last state: {last_state}")


def latest_metric_for_session(client: BackendClient, session_id: str) -> dict[str, Any] | None:
    metrics = client.get("/api/metrics/hot-path?limit=200")
    for item in metrics.get("items", []) or []:
        if item.get("sessionId") == session_id:
            return item
    return None


def wait_for_metric(client: BackendClient, session_id: str, *, timeout_sec: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        metric = latest_metric_for_session(client, session_id)
        if metric:
            return metric
        time.sleep(0.5)
    return None


def run_one_iteration(client: BackendClient, args: argparse.Namespace, index: int) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "iteration": index,
        "ok": False,
        "sessionId": "",
        "segments": {},
        "error": "",
    }
    started = False
    try:
        state = client.post("/api/live-mic/start")
        started = True
        session_id = str(state.get("sessionId") or state.get("current", {}).get("id") or "")
        sample["sessionId"] = session_id

        state = wait_for_state(
            client,
            lambda value: value.get("recordingState") == "recording" or value.get("listening") is True,
            timeout_sec=args.start_timeout_sec,
        )
        if state.get("recordingState") == "failed":
            raise RuntimeError(str(state.get("status") or "Recording failed during startup."))
        if not session_id:
            session_id = str(state.get("sessionId") or state.get("current", {}).get("id") or "")
            sample["sessionId"] = session_id
        if not session_id:
            raise RuntimeError("Backend did not expose a live session id.")

        time.sleep(args.record_seconds)
        client.post("/api/live-mic/stop")
        started = False
        wait_for_state(
            client,
            lambda value: value.get("recordingState") == "idle" and not value.get("listening"),
            timeout_sec=args.stop_timeout_sec,
        )

        metric = wait_for_metric(client, session_id, timeout_sec=args.metric_timeout_sec)
        if not metric:
            raise RuntimeError(f"No hot-path metric appeared for session {session_id}.")
        segments = metric.get("segments") or {}
        sample["segments"] = segments
        sample["totalMs"] = metric.get("totalMs", 0.0)
        sample["ok"] = any(name in segments for name in SEGMENTS_BY_REQUIREMENT.values())
    except Exception as exc:
        sample["error"] = str(exc)
        if started:
            try:
                client.post("/api/live-mic/stop")
            except Exception:
                pass
    return sample


def build_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    requirements: dict[str, Any] = {}
    for requirement, segment_name in SEGMENTS_BY_REQUIREMENT.items():
        values = [
            float((sample.get("segments") or {}).get(segment_name))
            for sample in samples
            if segment_name in (sample.get("segments") or {})
        ]
        status = "measured" if values else "missing"
        if requirement == "stop_to_text_injection" and not values:
            status = "missing_text_injection"
        requirements[requirement] = {
            "status": status,
            "segment": segment_name,
            "durations": summarize(values),
        }

    return {
        "iterations": len(samples),
        "successfulSamples": sum(1 for sample in samples if sample.get("ok")),
        "requirements": requirements,
        "complete": all(item["status"] == "measured" for item in requirements.values()),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    client = BackendClient(args.base_url, token=args.token, timeout_sec=args.http_timeout_sec)
    health = client.get("/api/health")
    samples = [run_one_iteration(client, args, index) for index in range(1, args.iterations + 1)]
    summary = build_summary(samples)
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url,
        "health": {
            "apiVersion": health.get("apiVersion"),
            "runtimeMode": health.get("runtimeMode"),
            "pid": health.get("pid"),
        },
        "ok": summary["successfulSamples"] > 0,
        "summary": summary,
        "samples": samples,
    }


def build_validate_result(args: argparse.Namespace) -> dict[str, Any]:
    sample = {
        "iteration": 1,
        "ok": True,
        "sessionId": "validate-session",
        "segments": {
            "hotkey_received_to_mic_ready_ms": 120.0,
            "hotkey_received_to_first_audio_frame_ms": 180.0,
            "stop_requested_to_session_finished_ms": 90.0,
        },
        "validateOnly": True,
    }
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url,
        "health": {"apiVersion": "validate", "runtimeMode": "validate", "pid": 0},
        "ok": True,
        "summary": build_summary([sample]),
        "samples": [sample],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive live mic start/stop and collect hot-path metrics.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token", default="")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--record-seconds", type=float, default=2.0)
    parser.add_argument("--start-timeout-sec", type=float, default=20.0)
    parser.add_argument("--stop-timeout-sec", type=float, default=60.0)
    parser.add_argument("--metric-timeout-sec", type=float, default=10.0)
    parser.add_argument("--http-timeout-sec", type=float, default=10.0)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    args.iterations = max(1, int(args.iterations))
    args.record_seconds = max(0.1, float(args.record_seconds))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(urllib.parse.unquote(output_path)).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = build_validate_result(args) if args.validate_only else run_benchmark(args)
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

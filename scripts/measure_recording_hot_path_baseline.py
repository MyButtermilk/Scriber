from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
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
STOP_TO_TEXT_SEGMENT = "stop_requested_to_first_paste_ms"
TEXT_BEFORE_STOP_SEGMENT = "first_paste_to_stop_requested_ms"
PROVIDER_TRANSCRIPT_SEGMENT = "hotkey_received_to_first_final_token_ms"
AUDIBLE_AUDIO_SEGMENT = "hotkey_received_to_first_audible_audio_frame_ms"
TEXT_TARGET_WINDOW_FLAG = "--_text-target-window"


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


def iteration_text_target_path(raw_path: str, index: int, total_iterations: int) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if total_iterations <= 1:
        return path
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    return path.with_name(f"{stem}.iteration-{index}{suffix}")


def run_text_target_window(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Internal text target window for recording hot-path measurement.")
    parser.add_argument("--target-output", required=True)
    parser.add_argument("--target-title", default="Scriber Hot Path Text Target")
    args = parser.parse_args(argv)

    try:
        import tkinter as tk
    except Exception as exc:
        print(f"Could not import tkinter for text target window: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.target_output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")

    root = tk.Tk()
    root.title(args.target_title)
    root.geometry("760x320+80+80")
    root.attributes("-topmost", True)
    text = tk.Text(root, wrap="word", font=("Segoe UI", 12))
    text.pack(fill="both", expand=True)

    def save_text() -> None:
        try:
            output_path.write_text(text.get("1.0", "end-1c"), encoding="utf-8")
        except Exception:
            pass
        root.after(200, save_text)

    def focus_window() -> None:
        try:
            root.lift()
            text.focus_force()
        except Exception:
            pass
        root.after(500, focus_window)

    root.after(100, focus_window)
    root.after(200, save_text)
    root.mainloop()
    return 0


def launch_text_target(args: argparse.Namespace, index: int) -> tuple[dict[str, Any] | None, subprocess.Popen | None]:
    if not args.text_target_file:
        return None, None

    target_path = iteration_text_target_path(args.text_target_file, index, args.iterations)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            TEXT_TARGET_WINDOW_FLAG,
            "--target-output",
            str(target_path),
            "--target-title",
            args.text_target_title,
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(args.text_target_settle_sec)
    if proc.poll() is not None:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"Text target window exited early with code {proc.returncode}: {stderr}")

    return {
        "path": str(target_path),
        "pid": proc.pid,
        "title": args.text_target_title,
    }, proc


def terminate_process(proc: subprocess.Popen | None, *, timeout_sec: float = 2.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_sec)


def read_text_target_length(info: dict[str, Any] | None) -> int:
    if not info or not info.get("path"):
        return 0
    path = Path(str(info["path"]))
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace"))


def start_speech_prompt(args: argparse.Namespace) -> tuple[dict[str, Any] | None, subprocess.Popen | None]:
    text = (args.speech_prompt_text or "").strip()
    if not text:
        return None, None
    if sys.platform != "win32":
        return {"started": False, "error": "speech prompt is only supported on Windows"}, None

    env = os.environ.copy()
    env["SCRIBER_RECORDING_PROMPT_TEXT"] = text
    command = (
        "$voice = New-Object -ComObject SAPI.SpVoice; "
        "$voice.Speak([string]$env:SCRIBER_RECORDING_PROMPT_TEXT) | Out-Null"
    )
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {"started": True, "pid": proc.pid, "chars": len(text)}, proc


def requirement_values(
    samples: list[dict[str, Any]],
    requirement: str,
    segment_name: str,
) -> tuple[list[float], dict[str, Any]]:
    if requirement != "stop_to_text_injection":
        return [
            float((sample.get("segments") or {}).get(segment_name))
            for sample in samples
            if segment_name in (sample.get("segments") or {})
        ], {"sourceSegments": [segment_name]}

    values: list[float] = []
    after_stop_samples = 0
    already_injected_samples = 0
    provider_transcript_samples = 0
    provider_transcript_values: list[float] = []
    audible_audio_samples = 0
    audible_audio_values: list[float] = []
    for sample in samples:
        segments = sample.get("segments") or {}
        if AUDIBLE_AUDIO_SEGMENT in segments:
            audible_audio_samples += 1
            audible_audio_values.append(float(segments[AUDIBLE_AUDIO_SEGMENT]))
        if PROVIDER_TRANSCRIPT_SEGMENT in segments:
            provider_transcript_samples += 1
            provider_transcript_values.append(float(segments[PROVIDER_TRANSCRIPT_SEGMENT]))
        if STOP_TO_TEXT_SEGMENT in segments:
            values.append(float(segments[STOP_TO_TEXT_SEGMENT]))
            after_stop_samples += 1
        elif TEXT_BEFORE_STOP_SEGMENT in segments:
            # Real-time providers can inject text before the user stops recording.
            # In that case the stop-to-text wait is measured as zero, not missing.
            values.append(0.0)
            already_injected_samples += 1

    return values, {
        "sourceSegments": [STOP_TO_TEXT_SEGMENT, TEXT_BEFORE_STOP_SEGMENT],
        "diagnosticSegments": [PROVIDER_TRANSCRIPT_SEGMENT, AUDIBLE_AUDIO_SEGMENT],
        "afterStopInjectionSamples": after_stop_samples,
        "alreadyInjectedBeforeStopSamples": already_injected_samples,
        "audibleAudioSamples": audible_audio_samples,
        "audibleAudioDurations": summarize(audible_audio_values),
        "providerTranscriptSamples": provider_transcript_samples,
        "providerTranscriptDurations": summarize(provider_transcript_values),
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
    text_target_proc: subprocess.Popen | None = None
    speech_proc: subprocess.Popen | None = None
    try:
        text_target, text_target_proc = launch_text_target(args, index)
        if text_target:
            sample["textTarget"] = text_target

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

        record_started_at = time.monotonic()
        if args.speech_prompt_text:
            time.sleep(min(args.speech_prompt_delay_sec, args.record_seconds))
            speech_prompt, speech_proc = start_speech_prompt(args)
            sample["speechPrompt"] = speech_prompt
        elapsed = time.monotonic() - record_started_at
        time.sleep(max(0.0, args.record_seconds - elapsed))
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
        if "textTarget" in sample:
            sample["textTarget"]["capturedChars"] = read_text_target_length(sample["textTarget"])
        sample["ok"] = any(name in segments for name in SEGMENTS_BY_REQUIREMENT.values())
    except Exception as exc:
        sample["error"] = str(exc)
        if started:
            try:
                client.post("/api/live-mic/stop")
            except Exception:
                pass
    finally:
        if speech_proc is not None:
            try:
                speech_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                terminate_process(speech_proc)
        terminate_process(text_target_proc)
    return sample


def build_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    requirements: dict[str, Any] = {}
    for requirement, segment_name in SEGMENTS_BY_REQUIREMENT.items():
        values, details = requirement_values(samples, requirement, segment_name)
        status = "measured" if values else "missing"
        if requirement == "stop_to_text_injection" and not values:
            if details.get("providerTranscriptSamples", 0):
                status = "missing_injection_after_transcript"
            elif details.get("audibleAudioSamples", 0):
                status = "missing_provider_transcript"
            else:
                status = "missing_audible_audio"
        requirements[requirement] = {
            "status": status,
            "segment": segment_name,
            **details,
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
    try:
        audio_diagnostics: dict[str, Any] = client.get("/api/runtime/audio-diagnostics")
    except Exception as exc:
        audio_diagnostics = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
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
        "audioDiagnostics": audio_diagnostics,
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
            "hotkey_received_to_first_audible_audio_frame_ms": 190.0,
            "hotkey_received_to_first_final_token_ms": 240.0,
            "hotkey_received_to_first_paste_ms": 260.0,
            "first_paste_to_stop_requested_ms": 350.0,
            "stop_requested_to_session_finished_ms": 90.0,
        },
        "validateOnly": True,
    }
    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseUrl": args.base_url,
        "health": {"apiVersion": "validate", "runtimeMode": "validate", "pid": 0},
        "audioDiagnostics": {
            "apiVersion": "validate",
            "runtimeMode": "validate",
            "pid": 0,
            "featureFlags": {
                "audioEngine": "python",
                "requestedAudioEngine": "python",
                "rustAudioRequested": False,
                "rustAudioAvailable": False,
            },
            "provider": {
                "configured": "validate",
                "active": None,
                "sonioxMode": "realtime",
            },
            "microphone": {
                "configuredDevice": "default",
                "favoriteMic": "",
                "favoriteMicConfigured": False,
                "micAlwaysOn": False,
                "idlePrewarmActive": False,
            },
            "textInjection": {
                "method": "auto",
                "disabled": False,
                "pastePreDelayMs": 80,
                "pasteRestoreDelayMs": 1500,
            },
            "runtimeImports": {
                "onnxruntime": {"importable": True, "error": None},
                "pipecat.audio.vad.silero": {"importable": True, "error": None},
            },
        },
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
    parser.add_argument("--text-target-file", default="")
    parser.add_argument("--text-target-title", default="Scriber Hot Path Text Target")
    parser.add_argument("--text-target-settle-sec", type=float, default=1.0)
    parser.add_argument("--speech-prompt-text", default="")
    parser.add_argument("--speech-prompt-delay-sec", type=float, default=0.5)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    args.iterations = max(1, int(args.iterations))
    args.record_seconds = max(0.1, float(args.record_seconds))
    args.text_target_settle_sec = max(0.1, float(args.text_target_settle_sec))
    args.speech_prompt_delay_sec = max(0.0, float(args.speech_prompt_delay_sec))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(urllib.parse.unquote(output_path)).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if argv and argv[0] == TEXT_TARGET_WINDOW_FLAG:
        return run_text_target_window(argv[1:])
    args = parse_args(argv)
    result = build_validate_result(args) if args.validate_only else run_benchmark(args)
    write_result(result, args.output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

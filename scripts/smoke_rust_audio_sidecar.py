from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.runtime.audio_frame_pipe import (  # noqa: E402
    AUDIO_FRAME_FLAG_PREBUFFER,
    AUDIO_FRAME_HEADER_LEN,
    AUDIO_FRAME_VERSION,
    decode_audio_frame_header,
)


def hash_hint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def resolve_sidecar_exe(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    exe_name = "scriber-audio-sidecar.exe" if os.name == "nt" else "scriber-audio-sidecar"
    candidates = [
        REPO_ROOT / "Frontend" / "src-tauri" / "target" / "debug" / exe_name,
        REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / exe_name,
        REPO_ROOT
        / "Frontend"
        / "src-tauri"
        / "resources"
        / "audio-sidecar"
        / exe_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


class SidecarClient:
    def __init__(self, sidecar_exe: Path, mode: str) -> None:
        self.sidecar_exe = sidecar_exe
        self.mode = mode
        self.proc: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str] = queue.Queue()
        self._counter = 0

    def __enter__(self) -> "SidecarClient":
        env = os.environ.copy()
        if self.mode == "wasapi":
            env["SCRIBER_RUST_AUDIO_WASAPI_CAPTURE"] = "1"
            env.pop("SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE", None)
        elif self.mode == "synthetic":
            env["SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE"] = "1"
            env.pop("SCRIBER_RUST_AUDIO_WASAPI_CAPTURE", None)
        else:
            raise ValueError(f"unsupported mode: {self.mode}")

        self.proc = subprocess.Popen(
            [str(self.sidecar_exe), "--stdio"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self.call("shutdown", {}, timeout=2.0)
                except Exception:
                    pass
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            self.proc = None

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self._responses.put(line)

    def call(self, command: str, payload: dict[str, Any], *, timeout: float = 6.0) -> dict[str, Any]:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("sidecar process is not running")
        self._counter += 1
        request_id = f"rust-audio-smoke-{self._counter}"
        request = {
            "protocolVersion": "1",
            "requestId": request_id,
            "command": command,
            "payload": payload,
        }
        proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        line = self._responses.get(timeout=timeout)
        response = json.loads(line)
        if response.get("requestId") != request_id:
            raise RuntimeError(f"sidecar requestId mismatch: {response}")
        return response


def read_exact(reader: Any, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            raise EOFError(f"frame pipe ended with {remaining} byte(s) remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def open_pipe_for_read(pipe_path: str, timeout_sec: float = 6.0) -> Any:
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        try:
            return open(pipe_path, "rb", buffering=0)
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"could not open frame pipe: {last_error}")


def read_frames(
    pipe_path: str,
    *,
    duration_sec: float,
    max_frames: int,
) -> dict[str, Any]:
    opened_at = time.perf_counter()
    frames_read = 0
    prebuffer_frames_read = 0
    live_frames_read = 0
    prebuffer_after_live_count = 0
    bytes_read = 0
    sequence_gaps: list[dict[str, int]] = []
    first_frame_ms: float | None = None
    first_live_frame_ms: float | None = None
    first_live_sequence: int | None = None
    last_sequence: int | None = None
    last_timestamp_micros: int | None = None
    last_flags: int | None = None
    deadline = opened_at + max(0.05, duration_sec)

    with open_pipe_for_read(pipe_path) as reader:
        while frames_read < max_frames and time.perf_counter() <= deadline:
            header_bytes = read_exact(reader, AUDIO_FRAME_HEADER_LEN)
            header = decode_audio_frame_header(header_bytes)
            payload = read_exact(reader, header.payload_len)
            now = time.perf_counter()
            if first_frame_ms is None:
                first_frame_ms = (now - opened_at) * 1000.0
            is_prebuffer = bool(header.flags & AUDIO_FRAME_FLAG_PREBUFFER)
            if is_prebuffer:
                prebuffer_frames_read += 1
                if live_frames_read > 0:
                    prebuffer_after_live_count += 1
            else:
                live_frames_read += 1
                if first_live_frame_ms is None:
                    first_live_frame_ms = (now - opened_at) * 1000.0
                    first_live_sequence = int(header.sequence)
            if last_sequence is not None and header.sequence != last_sequence + 1:
                sequence_gaps.append(
                    {
                        "expected": last_sequence + 1,
                        "actual": int(header.sequence),
                    }
                )
            frames_read += 1
            bytes_read += len(header_bytes) + len(payload)
            last_sequence = int(header.sequence)
            last_timestamp_micros = int(header.timestamp_micros)
            last_flags = int(header.flags)

    return {
        "framesRead": frames_read,
        "prebufferFramesRead": prebuffer_frames_read,
        "liveFramesRead": live_frames_read,
        "prebufferAfterLiveCount": prebuffer_after_live_count,
        "bytesRead": bytes_read,
        "firstFrameReadMs": round(first_frame_ms, 3) if first_frame_ms is not None else None,
        "firstLiveFrameReadMs": (
            round(first_live_frame_ms, 3) if first_live_frame_ms is not None else None
        ),
        "firstLiveSequence": first_live_sequence,
        "lastSequence": last_sequence,
        "lastTimestampMicros": last_timestamp_micros,
        "lastFlags": last_flags,
        "sequenceGapCount": len(sequence_gaps),
        "sequenceGaps": sequence_gaps[:10],
    }


def redacted_capture_start_payload(payload: dict[str, Any]) -> dict[str, Any]:
    frame_pipe = str(payload.get("framePipe") or "")
    redacted = {key: value for key, value in payload.items() if key != "framePipe"}
    redacted["framePipeHash"] = hash_hint(frame_pipe)
    return redacted


def clamp_prebuffer_ms(value: int) -> int:
    return max(0, min(2000, int(value)))


def run_capture(
    client: SidecarClient,
    *,
    name: str,
    request_payload: dict[str, Any],
    duration_sec: float,
    max_frames: int,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    start_response = client.call("captureStart", request_payload)
    capture_start_ms = (time.perf_counter() - started_at) * 1000.0
    start_payload = start_response.get("payload") if isinstance(start_response, dict) else {}
    if not isinstance(start_payload, dict):
        start_payload = {}

    result: dict[str, Any] = {
        "name": name,
        "ok": bool(start_response.get("success")),
        "captureStartResponseMs": round(capture_start_ms, 3),
        "start": redacted_capture_start_payload(start_payload),
    }
    if not start_response.get("success"):
        result["errorCode"] = start_response.get("errorCode")
        result["fallbackReason"] = start_response.get("fallbackReason")
        return result

    stream_id = str(start_payload.get("streamId") or "")
    frame_pipe = str(start_payload.get("framePipe") or "")
    try:
        result["frames"] = read_frames(
            frame_pipe,
            duration_sec=duration_sec,
            max_frames=max_frames,
        )
    except Exception as exc:
        result["ok"] = False
        result["frameReadError"] = str(exc)
    finally:
        stop_started_at = time.perf_counter()
        stop_response = client.call("captureStop", {"streamId": stream_id}, timeout=6.0)
        stop_ms = (time.perf_counter() - stop_started_at) * 1000.0
        stop_payload = stop_response.get("payload") if isinstance(stop_response, dict) else {}
        if not isinstance(stop_payload, dict):
            stop_payload = {}
        result["captureStopResponseMs"] = round(stop_ms, 3)
        result["stop"] = stop_payload
        if not stop_response.get("success"):
            result["ok"] = False
            result["stopErrorCode"] = stop_response.get("errorCode")
            result["stopFallbackReason"] = stop_response.get("fallbackReason")

    result["ok"] = bool(
        result.get("ok")
        and result.get("frames", {}).get("framesRead", 0) > 0
        and result.get("stop", {}).get("stopped") is True
    )
    return result


def base_capture_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "sampleRate": args.sample_rate,
        "channels": args.channels,
        "blockSize": args.block_size,
        "devicePreference": "default",
        "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
        "frameProtocol": {
            "magic": "SAF1",
            "version": AUDIO_FRAME_VERSION,
            "headerBytes": AUDIO_FRAME_HEADER_LEN,
            "sampleFormat": "pcm_i16_le",
        },
    }


def build_plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "ok": True,
        "planOnly": True,
        "mode": args.mode,
        "requested": {
            "durationSec": args.duration_sec,
            "selectedDurationSec": args.selected_duration_sec,
            "sampleRate": args.sample_rate,
            "channels": args.channels,
            "blockSize": args.block_size,
            "maxFrames": args.max_frames,
            "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
            "skipSelectedHash": bool(args.skip_selected_hash),
        },
        "requirements": [
            "Build scriber-audio-sidecar first with cargo build --bin scriber-audio-sidecar.",
            "Run on Windows for WASAPI capture evidence.",
            "Use --duration-sec 600 for the 10-minute physical stability gate.",
            "Keep SCRIBER_AUDIO_ENGINE=python as default unless promotion gates pass.",
        ],
        "exampleCommand": (
            "python scripts/smoke_rust_audio_sidecar.py "
            "--mode wasapi --duration-sec 10 --output tmp/rust-audio-sidecar-smoke.json"
        ),
    }


def write_payload(payload: dict[str, Any], output: str = "") -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test the opt-in Rust audio sidecar frame-pipe path.",
    )
    parser.add_argument("--sidecar-exe", default="", help="Path to scriber-audio-sidecar(.exe).")
    parser.add_argument("--mode", choices=["wasapi", "synthetic"], default="wasapi")
    parser.add_argument("--duration-sec", type=float, default=1.0)
    parser.add_argument("--selected-duration-sec", type=float, default=0.5)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--block-size", type=int, default=160)
    parser.add_argument("--max-frames", type=int, default=250)
    parser.add_argument("--prebuffer-ms", type=int, default=0)
    parser.add_argument("--skip-selected-hash", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.plan_only:
        write_payload(build_plan_payload(args), args.output)
        return 0

    sidecar_exe = resolve_sidecar_exe(args.sidecar_exe)
    payload: dict[str, Any] = {
        "apiVersion": "1",
        "ok": False,
        "planOnly": False,
        "mode": args.mode,
        "sidecar": {
            "exe": str(sidecar_exe),
            "exists": sidecar_exe.is_file(),
            "sha256": (
                hashlib.sha256(sidecar_exe.read_bytes()).hexdigest()
                if sidecar_exe.is_file()
                else None
            ),
        },
        "requested": {
            "durationSec": args.duration_sec,
            "selectedDurationSec": args.selected_duration_sec,
            "sampleRate": args.sample_rate,
            "channels": args.channels,
            "blockSize": args.block_size,
            "maxFrames": args.max_frames,
            "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
            "skipSelectedHash": bool(args.skip_selected_hash),
        },
        "captures": [],
    }
    if not sidecar_exe.is_file():
        payload["error"] = f"sidecar executable not found: {sidecar_exe}"
        write_payload(payload, args.output)
        return 2

    with SidecarClient(sidecar_exe, args.mode) as client:
        first_payload = base_capture_payload(args)
        default_capture = run_capture(
            client,
            name="default",
            request_payload=first_payload,
            duration_sec=args.duration_sec,
            max_frames=args.max_frames,
        )
        payload["captures"].append(default_capture)

        endpoint_hash = str(default_capture.get("start", {}).get("nativeEndpointIdHash") or "")
        if args.mode == "wasapi" and endpoint_hash and not args.skip_selected_hash:
            selected_payload = base_capture_payload(args)
            selected_payload["devicePreference"] = "selected-hash-smoke"
            selected_payload["portAudioLabel"] = "selected hash smoke"
            selected_payload["nativeEndpointIdHash"] = endpoint_hash
            payload["captures"].append(
                run_capture(
                    client,
                    name="selected-native-endpoint-hash",
                    request_payload=selected_payload,
                    duration_sec=args.selected_duration_sec,
                    max_frames=args.max_frames,
                )
            )

    captures = payload["captures"]
    failed = [capture for capture in captures if not capture.get("ok")]
    selected = next(
        (capture for capture in captures if capture.get("name") == "selected-native-endpoint-hash"),
        None,
    )
    payload["summary"] = {
        "captureCount": len(captures),
        "failedCaptureCount": len(failed),
        "totalFramesRead": sum(
            int(capture.get("frames", {}).get("framesRead") or 0) for capture in captures
        ),
        "totalPrebufferFramesRead": sum(
            int(capture.get("frames", {}).get("prebufferFramesRead") or 0)
            for capture in captures
        ),
        "totalLiveFramesRead": sum(
            int(capture.get("frames", {}).get("liveFramesRead") or 0)
            for capture in captures
        ),
        "totalPrebufferAfterLiveCount": sum(
            int(capture.get("frames", {}).get("prebufferAfterLiveCount") or 0)
            for capture in captures
        ),
        "totalFramesWritten": sum(
            int(capture.get("stop", {}).get("framesWritten") or 0) for capture in captures
        ),
        "totalPrebufferFramesWritten": sum(
            int(capture.get("stop", {}).get("prebufferFramesWritten") or 0)
            for capture in captures
        ),
        "totalLiveFramesWritten": sum(
            int(capture.get("stop", {}).get("liveFramesWritten") or 0)
            for capture in captures
        ),
        "selectedHashVerified": bool(
            selected
            and selected.get("ok")
            and selected.get("start", {})
            .get("endpointSelection", {})
            .get("mode")
            == "nativeEndpointHash"
        ),
    }
    payload["ok"] = len(failed) == 0 and bool(captures)
    write_payload(payload, args.output)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

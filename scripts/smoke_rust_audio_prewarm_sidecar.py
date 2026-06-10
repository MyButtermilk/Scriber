from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_rust_audio_sidecar import (  # noqa: E402
    SidecarClient,
    clamp_prebuffer_ms,
    resolve_sidecar_exe,
)
from src.runtime.audio_frame_pipe import (  # noqa: E402
    AUDIO_FRAME_HEADER_LEN,
    AUDIO_FRAME_VERSION,
)


def base_prewarm_payload(args: argparse.Namespace) -> dict[str, Any]:
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


def validate_prewarm_metrics(
    prewarm: dict[str, Any],
    *,
    require_prebuffer: bool,
) -> list[str]:
    errors: list[str] = []
    start = prewarm.get("start")
    if not isinstance(start, dict):
        return ["start must be an object"]
    stop = prewarm.get("stop")
    if not isinstance(stop, dict):
        return ["stop must be an object"]

    prewarm_id = str(start.get("prewarmId") or "")
    if not prewarm_id:
        errors.append("start.prewarmId must be present")
    if stop.get("stopped") is not True:
        errors.append("stop.stopped must be true")
    if stop.get("prewarmId") != prewarm_id:
        errors.append("stop.prewarmId must match start.prewarmId")
    if stop.get("prewarmError") not in (None, ""):
        errors.append("stop.prewarmError must be empty")

    total_blocks = int(stop.get("totalBlocksObserved") or 0)
    total_frames = int(stop.get("totalAudioFramesObserved") or 0)
    buffered_blocks = int(stop.get("bufferedBlocks") or 0)
    buffered_frames = int(stop.get("bufferedAudioFrames") or 0)
    prebuffer_target = int(start.get("prebufferFrameTarget") or 0)
    if total_blocks <= 0:
        errors.append("totalBlocksObserved must be positive")
    if total_frames <= 0:
        errors.append("totalAudioFramesObserved must be positive")
    if require_prebuffer:
        if prebuffer_target <= 0:
            errors.append("prebufferFrameTarget must be positive when prebufferMs is requested")
        if buffered_blocks <= 0:
            errors.append("bufferedBlocks must be positive when prebufferMs is requested")
        if buffered_frames <= 0:
            errors.append("bufferedAudioFrames must be positive when prebufferMs is requested")
        if prebuffer_target > 0 and buffered_blocks > prebuffer_target:
            errors.append("bufferedBlocks must not exceed prebufferFrameTarget")
    return errors


def run_prewarm(
    client: SidecarClient,
    *,
    request_payload: dict[str, Any],
    duration_sec: float,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    start_response = client.call("prewarmStart", request_payload)
    start_response_ms = (time.perf_counter() - started_at) * 1000.0
    start_payload = start_response.get("payload") if isinstance(start_response, dict) else {}
    if not isinstance(start_payload, dict):
        start_payload = {}

    result: dict[str, Any] = {
        "ok": bool(start_response.get("success")),
        "prewarmStartResponseMs": round(start_response_ms, 3),
        "start": start_payload,
    }
    if not start_response.get("success"):
        result["errorCode"] = start_response.get("errorCode")
        result["fallbackReason"] = start_response.get("fallbackReason")
        return result

    prewarm_id = str(start_payload.get("prewarmId") or "")
    time.sleep(max(0.05, float(duration_sec)))
    stop_started_at = time.perf_counter()
    stop_response = client.call("prewarmStop", {"prewarmId": prewarm_id}, timeout=6.0)
    stop_response_ms = (time.perf_counter() - stop_started_at) * 1000.0
    stop_payload = stop_response.get("payload") if isinstance(stop_response, dict) else {}
    if not isinstance(stop_payload, dict):
        stop_payload = {}

    result["prewarmStopResponseMs"] = round(stop_response_ms, 3)
    result["stop"] = stop_payload
    if not stop_response.get("success"):
        result["ok"] = False
        result["stopErrorCode"] = stop_response.get("errorCode")
        result["stopFallbackReason"] = stop_response.get("fallbackReason")

    validation_errors = validate_prewarm_metrics(
        result,
        require_prebuffer=int(request_payload.get("prebufferMs") or 0) > 0,
    )
    if validation_errors:
        result["validationErrors"] = validation_errors
    result["ok"] = bool(result.get("ok") and not validation_errors)
    return result


def build_plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "ok": True,
        "planOnly": True,
        "mode": args.mode,
        "requested": {
            "durationSec": args.duration_sec,
            "sampleRate": args.sample_rate,
            "channels": args.channels,
            "blockSize": args.block_size,
            "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
        },
        "requirements": [
            "Build scriber-audio-sidecar first with cargo build --bin scriber-audio-sidecar.",
            (
                "The synthetic mode uses SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE=1 "
                "through the shared sidecar client."
            ),
            (
                "The wasapi mode uses SCRIBER_RUST_AUDIO_WASAPI_CAPTURE=1 and starts "
                "a real passive WASAPI idle capture stream."
            ),
            "This smoke does not yet prove buffered WASAPI audio adoption into active capture.",
            "Keep SCRIBER_AUDIO_ENGINE=python as default unless promotion gates pass.",
        ],
        "exampleCommand": (
            "python scripts/smoke_rust_audio_prewarm_sidecar.py "
            "--mode wasapi --duration-sec 2 --prebuffer-ms 400 "
            "--output tmp/rust-audio-prewarm-sidecar-smoke.json"
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
        description="Smoke-test the opt-in Rust audio prewarm sidecar lifecycle.",
    )
    parser.add_argument("--sidecar-exe", default="", help="Path to scriber-audio-sidecar(.exe).")
    parser.add_argument("--mode", choices=["wasapi", "synthetic"], default="synthetic")
    parser.add_argument("--duration-sec", type=float, default=1.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--block-size", type=int, default=160)
    parser.add_argument("--prebuffer-ms", type=int, default=400)
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
            "sampleRate": args.sample_rate,
            "channels": args.channels,
            "blockSize": args.block_size,
            "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
        },
    }
    if not sidecar_exe.is_file():
        payload["error"] = f"sidecar executable not found: {sidecar_exe}"
        write_payload(payload, args.output)
        return 2

    with SidecarClient(sidecar_exe, args.mode) as client:
        payload["prewarm"] = run_prewarm(
            client,
            request_payload=base_prewarm_payload(args),
            duration_sec=args.duration_sec,
        )

    prewarm = payload.get("prewarm")
    payload["summary"] = {
        "prewarmOk": bool(isinstance(prewarm, dict) and prewarm.get("ok")),
        "totalBlocksObserved": int(
            prewarm.get("stop", {}).get("totalBlocksObserved") or 0
        )
        if isinstance(prewarm, dict)
        else 0,
        "bufferedAudioFrames": int(
            prewarm.get("stop", {}).get("bufferedAudioFrames") or 0
        )
        if isinstance(prewarm, dict)
        else 0,
    }
    payload["ok"] = bool(isinstance(prewarm, dict) and prewarm.get("ok"))
    write_payload(payload, args.output)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

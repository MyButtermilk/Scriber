from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from src.config import Config  # noqa: E402
from src.mic_prewarm import RustAudioPrewarmManager  # noqa: E402
from src.microphone import RustPrototypeFrameSource  # noqa: E402


COMMAND_MAP = {
    "audioPrewarmStart": "prewarmStart",
    "audioPrewarmStop": "prewarmStop",
    "audioCaptureStart": "captureStart",
    "audioCaptureStop": "captureStop",
}


def hash_hint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def redact_prewarm_ids(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"prewarmId", "prewarm_id"}:
                redacted[f"{key}Hash"] = hash_hint(str(item or ""))
            else:
                redacted[key] = redact_prewarm_ids(item)
        return redacted
    if isinstance(value, list):
        return [redact_prewarm_ids(item) for item in value]
    return value


class ShellIpcSidecarAdapter:
    """Adapter that makes the real sidecar look like Tauri shell IPC."""

    def __init__(self, client: SidecarClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        sidecar_command = COMMAND_MAP.get(command)
        if not sidecar_command:
            raise RuntimeError(f"unsupported shell IPC command in smoke: {command}")
        request_payload = payload or {}
        response = self.client.call(
            sidecar_command,
            request_payload,
            timeout=max(0.25, float(timeout_seconds or 6.0)),
        )
        response_payload = response.get("payload")
        self.calls.append(
            {
                "command": command,
                "sidecarCommand": sidecar_command,
                "success": bool(response.get("success")),
                "errorCode": response.get("errorCode"),
                "fallbackReason": response.get("fallbackReason"),
                "request": redact_prewarm_ids(request_payload),
                "responseKeys": (
                    sorted(response_payload.keys()) if isinstance(response_payload, dict) else []
                ),
            }
        )
        return response


def configure_runtime(args: argparse.Namespace) -> None:
    os.environ["SCRIBER_AUDIO_ENGINE"] = "rust-prototype"
    os.environ["SCRIBER_MIC_ALWAYS_ON"] = "1"
    Config.MIC_ALWAYS_ON = True
    Config.SAMPLE_RATE = int(args.sample_rate)
    Config.CHANNELS = int(args.channels)
    Config.MIC_BLOCK_SIZE = int(args.block_size)
    Config.MIC_PREBUFFER_MS = clamp_prebuffer_ms(args.prebuffer_ms)
    Config.MIC_DEVICE = "default"
    if not args.honor_favorite_mic:
        Config.FAVORITE_MIC = ""


def validate_smoke(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("ok") is not True:
        errors.append("payload.ok must be true")
    manager_start = payload.get("managerStart")
    if not isinstance(manager_start, dict) or manager_start.get("active") is not True:
        errors.append("managerStart.active must be true")
    adoption = payload.get("managerAdoption")
    if not isinstance(adoption, dict) or not adoption.get("prewarmIdHash"):
        errors.append("managerAdoption.prewarmIdHash must be present")
    final_source = payload.get("sourceFinal")
    if not isinstance(final_source, dict):
        return errors + ["sourceFinal must be an object"]
    adopted = final_source.get("adoptedPrewarm")
    if not isinstance(adopted, dict) or adopted.get("adopted") is not True:
        errors.append("sourceFinal.adoptedPrewarm.adopted must be true")
    elif int(adopted.get("blocks") or 0) <= 0:
        errors.append("sourceFinal.adoptedPrewarm.blocks must be positive")
    if int(final_source.get("callbackCount") or 0) <= 0:
        errors.append("sourceFinal.callbackCount must be positive")
    if int(final_source.get("framePipePrebufferFramesRead") or 0) <= 0:
        errors.append("sourceFinal.framePipePrebufferFramesRead must be positive")
    if int(final_source.get("framePipeLiveFramesRead") or 0) <= 0:
        errors.append("sourceFinal.framePipeLiveFramesRead must be positive")
    if int(final_source.get("framePipePrebufferAfterLiveCount") or 0) != 0:
        errors.append("sourceFinal.framePipePrebufferAfterLiveCount must be 0")
    if int(final_source.get("framePipeSequenceErrorCount") or 0) != 0:
        errors.append("sourceFinal.framePipeSequenceErrorCount must be 0")
    if int(final_source.get("framePipeProtocolErrorCount") or 0) != 0:
        errors.append("sourceFinal.framePipeProtocolErrorCount must be 0")
    manager_resume = payload.get("managerResume")
    if isinstance(manager_resume, dict) and manager_resume.get("active") is not True:
        errors.append("managerResume.active must be true when resume is requested")
    return errors


def build_plan_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "apiVersion": "1",
        "ok": True,
        "planOnly": True,
        "mode": args.mode,
        "requested": requested_payload(args),
        "requirements": [
            "Build scriber-audio-sidecar first with cargo build --bin scriber-audio-sidecar.",
            (
                "Run with SCRIBER_AUDIO_ENGINE=rust-prototype semantics; this smoke "
                "uses RustAudioPrewarmManager plus RustPrototypeFrameSource."
            ),
            (
                "The synthetic mode proves app-lifecycle plumbing; the wasapi mode "
                "opens the real passive WASAPI prewarm and capture path."
            ),
            (
                "Promotion still requires long physical hardware runs and provider-backed "
                "transcription smokes."
            ),
        ],
        "exampleCommand": (
            "python scripts/smoke_rust_audio_app_prewarm.py "
            "--mode wasapi --duration-sec 2 --prewarm-duration-sec 1 "
            "--output tmp/rust-audio-app-prewarm-smoke.json"
        ),
    }


def requested_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "durationSec": args.duration_sec,
        "prewarmDurationSec": args.prewarm_duration_sec,
        "postResumeDurationSec": args.post_resume_duration_sec,
        "sampleRate": args.sample_rate,
        "channels": args.channels,
        "blockSize": args.block_size,
        "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
        "resumeAfterCapture": bool(args.resume_after_capture),
        "honorFavoriteMic": bool(args.honor_favorite_mic),
    }


def write_payload(payload: dict[str, Any], output: str = "") -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    configure_runtime(args)
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
        "requested": requested_payload(args),
    }
    if not sidecar_exe.is_file():
        payload["error"] = f"sidecar executable not found: {sidecar_exe}"
        return payload

    callbacks: list[dict[str, Any]] = []
    source: RustPrototypeFrameSource | None = None
    manager: RustAudioPrewarmManager | None = None
    with SidecarClient(sidecar_exe, args.mode) as client:
        adapter = ShellIpcSidecarAdapter(client)
        manager = RustAudioPrewarmManager(shell_call=adapter)
        try:
            started = manager.start_if_enabled()
            payload["managerStart"] = manager.diagnostic_snapshot()
            if not started:
                payload["error"] = "RustAudioPrewarmManager did not start"
                payload["ipcCalls"] = adapter.calls
                return payload

            time.sleep(max(0.05, float(args.prewarm_duration_sec)))
            adopted = manager.attach_active_capture(
                None,
                sample_rate=args.sample_rate,
                target_channels=args.channels,
                block_size=args.block_size,
                device="default",
            )
            payload["managerAdoption"] = redact_prewarm_ids(adopted or {})
            prewarm_id = str((adopted or {}).get("prewarmId") or "")
            if not prewarm_id:
                payload["error"] = "RustAudioPrewarmManager did not return prewarmId"
                payload["ipcCalls"] = adapter.calls
                return payload

            def callback(audio, frames, time_info, status) -> None:
                callbacks.append(
                    {
                        "frames": int(frames or 0),
                        "shape": list(getattr(audio, "shape", ())),
                        "engine": (
                            time_info.get("engine")
                            if isinstance(time_info, dict)
                            else None
                        ),
                        "status": str(status) if status else None,
                    }
                )

            source = RustPrototypeFrameSource(
                sample_rate=args.sample_rate,
                target_channels=args.channels,
                block_size=args.block_size,
                device="default",
                shell_call=adapter,
                first_frame_timeout_seconds=max(0.25, float(args.first_frame_timeout_sec)),
                prewarm_id=prewarm_id,
            )
            source.open(callback)
            source.start()
            payload["sourceStarted"] = source.diagnostic_snapshot()
            time.sleep(max(0.05, float(args.duration_sec)))
            source.stop(close=True)
            payload["sourceFinal"] = source.diagnostic_snapshot()
            manager.detach_active_capture(None)
            if args.resume_after_capture:
                resumed = manager.resume_after_active_capture()
                time.sleep(max(0.05, float(args.post_resume_duration_sec)))
                payload["managerResume"] = manager.diagnostic_snapshot()
                payload["managerResume"]["resumeReturned"] = bool(resumed)
                manager.stop(reason="smoke_complete")
            payload["managerFinal"] = manager.diagnostic_snapshot()
            payload["callbacks"] = callbacks[:5]
            payload["callbackCount"] = len(callbacks)
            payload["ipcCalls"] = adapter.calls
        finally:
            if source is not None and source.stream_id:
                try:
                    source.stop(close=True)
                except Exception:
                    pass
            if manager is not None:
                try:
                    manager.stop(reason="smoke_cleanup")
                except Exception:
                    pass

    final_source = payload.get("sourceFinal") if isinstance(payload.get("sourceFinal"), dict) else {}
    adopted_prewarm = final_source.get("adoptedPrewarm") if isinstance(final_source, dict) else {}
    if not isinstance(adopted_prewarm, dict):
        adopted_prewarm = {}
    payload["summary"] = {
        "callbackCount": int(payload.get("callbackCount") or 0),
        "adoptedPrewarmBlocks": int(adopted_prewarm.get("blocks") or 0),
        "prebufferFramesRead": int(final_source.get("framePipePrebufferFramesRead") or 0),
        "liveFramesRead": int(final_source.get("framePipeLiveFramesRead") or 0),
        "prebufferAfterLiveCount": int(final_source.get("framePipePrebufferAfterLiveCount") or 0),
        "sequenceErrorCount": int(final_source.get("framePipeSequenceErrorCount") or 0),
        "protocolErrorCount": int(final_source.get("framePipeProtocolErrorCount") or 0),
    }
    errors = validate_smoke({**payload, "ok": True})
    if errors:
        payload["validationErrors"] = errors
    payload["ok"] = not errors
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Smoke-test app-level Rust Always-On-Mic prewarm adoption using "
            "RustAudioPrewarmManager and RustPrototypeFrameSource."
        ),
    )
    parser.add_argument("--sidecar-exe", default="", help="Path to scriber-audio-sidecar(.exe).")
    parser.add_argument("--mode", choices=["wasapi", "synthetic"], default="synthetic")
    parser.add_argument("--duration-sec", type=float, default=0.5)
    parser.add_argument("--prewarm-duration-sec", type=float, default=0.5)
    parser.add_argument("--post-resume-duration-sec", type=float, default=0.2)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--block-size", type=int, default=160)
    parser.add_argument("--prebuffer-ms", type=int, default=400)
    parser.add_argument("--first-frame-timeout-sec", type=float, default=1.5)
    parser.add_argument(
        "--honor-favorite-mic",
        action="store_true",
        help=(
            "Use the configured favorite microphone. The default smoke ignores "
            "user favorites so the release gate exercises the stable WASAPI default path."
        ),
    )
    parser.add_argument("--no-resume-after-capture", dest="resume_after_capture", action="store_false")
    parser.set_defaults(resume_after_capture=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.plan_only:
        write_payload(build_plan_payload(args), args.output)
        return 0

    payload = run_smoke(args)
    write_payload(payload, args.output)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

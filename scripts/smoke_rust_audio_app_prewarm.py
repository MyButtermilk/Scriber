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
    "audioPrewarmStatus": "prewarmStatus",
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
    manager_pre_adoption_health = payload.get("managerPreAdoptionHealth")
    if not _valid_manager_health_snapshot(manager_pre_adoption_health):
        errors.append("managerPreAdoptionHealth must prove active audioPrewarmStatus")
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
    manager_post_resume_health = payload.get("managerPostResumeHealth")
    if isinstance(manager_resume, dict) and not _valid_manager_health_snapshot(manager_post_resume_health):
        errors.append("managerPostResumeHealth must prove active audioPrewarmStatus when resume is requested")
    requested = payload.get("requested") if isinstance(payload.get("requested"), dict) else {}
    requested_cycles = int(requested.get("captureCycles") or 1)
    cycles = payload.get("cycles")
    if requested_cycles > 1:
        if not isinstance(cycles, list):
            errors.append("cycles must be present when captureCycles > 1")
            cycles = []
        if len(cycles) != requested_cycles:
            errors.append("cycles length must match requested.captureCycles")
    if isinstance(cycles, list):
        for cycle in cycles:
            if not isinstance(cycle, dict):
                errors.append("cycle entries must be objects")
                continue
            prefix = f"cycle {cycle.get('index')}"
            cycle_adoption = cycle.get("managerAdoption")
            if not isinstance(cycle_adoption, dict) or not cycle_adoption.get("prewarmIdHash"):
                errors.append(f"{prefix} managerAdoption.prewarmIdHash must be present")
            cycle_source = cycle.get("sourceFinal")
            if not isinstance(cycle_source, dict):
                errors.append(f"{prefix} sourceFinal must be an object")
                continue
            errors.extend(validate_source_final(cycle_source, prefix))
            if requested.get("resumeAfterCapture") is True:
                cycle_resume = cycle.get("managerResume")
                if not isinstance(cycle_resume, dict) or cycle_resume.get("active") is not True:
                    errors.append(f"{prefix} managerResume.active must be true")
                cycle_health = cycle.get("managerPostResumeHealth")
                if not _valid_manager_health_snapshot(cycle_health):
                    errors.append(f"{prefix} managerPostResumeHealth must prove active audioPrewarmStatus")
    return errors


def validate_source_final(final_source: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    adopted = final_source.get("adoptedPrewarm")
    if not isinstance(adopted, dict) or adopted.get("adopted") is not True:
        errors.append(f"{prefix} sourceFinal.adoptedPrewarm.adopted must be true")
    elif int(adopted.get("blocks") or 0) <= 0:
        errors.append(f"{prefix} sourceFinal.adoptedPrewarm.blocks must be positive")
    if int(final_source.get("callbackCount") or 0) <= 0:
        errors.append(f"{prefix} sourceFinal.callbackCount must be positive")
    if int(final_source.get("framePipePrebufferFramesRead") or 0) <= 0:
        errors.append(f"{prefix} sourceFinal.framePipePrebufferFramesRead must be positive")
    if int(final_source.get("framePipeLiveFramesRead") or 0) <= 0:
        errors.append(f"{prefix} sourceFinal.framePipeLiveFramesRead must be positive")
    if int(final_source.get("framePipePrebufferAfterLiveCount") or 0) != 0:
        errors.append(f"{prefix} sourceFinal.framePipePrebufferAfterLiveCount must be 0")
    if int(final_source.get("framePipeSequenceErrorCount") or 0) != 0:
        errors.append(f"{prefix} sourceFinal.framePipeSequenceErrorCount must be 0")
    if int(final_source.get("framePipeProtocolErrorCount") or 0) != 0:
        errors.append(f"{prefix} sourceFinal.framePipeProtocolErrorCount must be 0")
    return errors


def _valid_manager_health_snapshot(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("active") is not True:
        return False
    if value.get("lastHealthCheckActive") is not True:
        return False
    if not isinstance(value.get("lastHealthResponseMs"), (int, float)):
        return False
    try:
        if int(value.get("healthRestartCount") or 0) != 0:
            return False
    except (TypeError, ValueError):
        return False
    status = value.get("lastStatus")
    if not isinstance(status, dict):
        return False
    if status.get("active") is not True:
        return False
    return bool(status.get("prewarmIdHash") or value.get("prewarmIdHash"))


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
        "captureCycles": args.capture_cycles,
        "sampleRate": args.sample_rate,
        "channels": args.channels,
        "blockSize": args.block_size,
        "prebufferMs": clamp_prebuffer_ms(args.prebuffer_ms),
        "resumeAfterCapture": bool(args.resume_after_capture),
        "honorFavoriteMic": bool(args.honor_favorite_mic),
    }


def summarize_source(final_source: dict[str, Any], callback_count: int) -> dict[str, int]:
    adopted_prewarm = final_source.get("adoptedPrewarm")
    if not isinstance(adopted_prewarm, dict):
        adopted_prewarm = {}
    return {
        "callbackCount": int(callback_count or 0),
        "adoptedPrewarmBlocks": int(adopted_prewarm.get("blocks") or 0),
        "prebufferFramesRead": int(final_source.get("framePipePrebufferFramesRead") or 0),
        "liveFramesRead": int(final_source.get("framePipeLiveFramesRead") or 0),
        "prebufferAfterLiveCount": int(final_source.get("framePipePrebufferAfterLiveCount") or 0),
        "sequenceErrorCount": int(final_source.get("framePipeSequenceErrorCount") or 0),
        "protocolErrorCount": int(final_source.get("framePipeProtocolErrorCount") or 0),
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
    if int(args.capture_cycles) > 1 and not args.resume_after_capture:
        payload["error"] = "captureCycles > 1 requires resumeAfterCapture"
        return payload

    all_callbacks: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
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
            for cycle_index in range(1, int(args.capture_cycles) + 1):
                cycle: dict[str, Any] = {"index": cycle_index}
                health_reason = (
                    "smoke_pre_adoption"
                    if cycle_index == 1
                    else f"smoke_pre_adoption_cycle_{cycle_index}"
                )
                cycle["managerPreAdoptionHealthReturned"] = bool(
                    manager.ensure_healthy(reason=health_reason)
                )
                cycle["managerPreAdoptionHealth"] = manager.diagnostic_snapshot()
                if cycle_index == 1:
                    payload["managerPreAdoptionHealthReturned"] = cycle[
                        "managerPreAdoptionHealthReturned"
                    ]
                    payload["managerPreAdoptionHealth"] = cycle[
                        "managerPreAdoptionHealth"
                    ]
                adopted = manager.attach_active_capture(
                    None,
                    sample_rate=args.sample_rate,
                    target_channels=args.channels,
                    block_size=args.block_size,
                    device="default",
                )
                cycle["managerAdoption"] = redact_prewarm_ids(adopted or {})
                payload["managerAdoption"] = cycle["managerAdoption"]
                prewarm_id = str((adopted or {}).get("prewarmId") or "")
                if not prewarm_id:
                    payload["error"] = "RustAudioPrewarmManager did not return prewarmId"
                    payload["cycles"] = cycles + [cycle]
                    payload["ipcCalls"] = adapter.calls
                    return payload

                callbacks: list[dict[str, Any]] = []

                def callback(audio, frames, time_info, status) -> None:
                    callback_info = {
                        "frames": int(frames or 0),
                        "shape": list(getattr(audio, "shape", ())),
                        "engine": (
                            time_info.get("engine")
                            if isinstance(time_info, dict)
                            else None
                        ),
                        "status": str(status) if status else None,
                    }
                    callbacks.append(callback_info)
                    all_callbacks.append({**callback_info, "cycle": cycle_index})

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
                cycle["sourceStarted"] = source.diagnostic_snapshot()
                payload["sourceStarted"] = cycle["sourceStarted"]
                time.sleep(max(0.05, float(args.duration_sec)))
                source.stop(close=True)
                cycle["sourceFinal"] = source.diagnostic_snapshot()
                payload["sourceFinal"] = cycle["sourceFinal"]
                source = None
                manager.detach_active_capture(None)
                cycle["callbacks"] = callbacks[:5]
                cycle["callbackCount"] = len(callbacks)
                cycle["summary"] = summarize_source(cycle["sourceFinal"], len(callbacks))
                if args.resume_after_capture:
                    resumed = manager.resume_after_active_capture()
                    time.sleep(max(0.05, float(args.post_resume_duration_sec)))
                    cycle["managerResume"] = manager.diagnostic_snapshot()
                    cycle["managerResume"]["resumeReturned"] = bool(resumed)
                    post_reason = (
                        "smoke_post_resume"
                        if cycle_index == int(args.capture_cycles)
                        else f"smoke_post_resume_cycle_{cycle_index}"
                    )
                    cycle["managerPostResumeHealthReturned"] = bool(
                        manager.ensure_healthy(reason=post_reason)
                    )
                    cycle["managerPostResumeHealth"] = manager.diagnostic_snapshot()
                    if cycle_index == int(args.capture_cycles):
                        payload["managerResume"] = cycle["managerResume"]
                        payload["managerPostResumeHealthReturned"] = cycle[
                            "managerPostResumeHealthReturned"
                        ]
                        payload["managerPostResumeHealth"] = cycle[
                            "managerPostResumeHealth"
                        ]
                cycles.append(cycle)
            if args.resume_after_capture:
                manager.stop(reason="smoke_complete")
            payload["managerFinal"] = manager.diagnostic_snapshot()
            payload["cycles"] = cycles
            payload["callbacks"] = all_callbacks[:5]
            payload["callbackCount"] = len(all_callbacks)
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
    cycle_summaries = [
        cycle.get("summary")
        for cycle in cycles
        if isinstance(cycle.get("summary"), dict)
    ]
    payload["summary"] = {
        "callbackCount": int(payload.get("callbackCount") or 0),
        "captureCycleCount": len(cycles),
        "adoptedPrewarmBlocks": sum(int(item.get("adoptedPrewarmBlocks") or 0) for item in cycle_summaries),
        "prebufferFramesRead": sum(int(item.get("prebufferFramesRead") or 0) for item in cycle_summaries),
        "liveFramesRead": sum(int(item.get("liveFramesRead") or 0) for item in cycle_summaries),
        "prebufferAfterLiveCount": sum(int(item.get("prebufferAfterLiveCount") or 0) for item in cycle_summaries),
        "sequenceErrorCount": sum(int(item.get("sequenceErrorCount") or 0) for item in cycle_summaries),
        "protocolErrorCount": sum(int(item.get("protocolErrorCount") or 0) for item in cycle_summaries),
        "lastCycle": summarize_source(final_source, int((cycles[-1] if cycles else {}).get("callbackCount") or 0)),
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
    parser.add_argument(
        "--capture-cycles",
        type=int,
        default=1,
        help="Number of prewarm adoption -> capture -> stop -> resume cycles to run.",
    )
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
    args = parser.parse_args(argv)
    args.capture_cycles = max(1, int(args.capture_cycles))
    return args


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

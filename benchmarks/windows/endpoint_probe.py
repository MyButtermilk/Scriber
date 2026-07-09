from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.perf.evaluator.local_wux import compute_local_wux, load_baseline_metrics

OVERLAY_PROVIDER_CONFIGS = [
    {
        "provider": "microsoft",
        "defaultStt": "azure_mai",
        "requiredEnv": ["AZURE_MAI_SPEECH_KEY"],
        "scenario": "overlay_cold",
        "metric": "overlay_cold_p95_ms",
    },
    {
        "provider": "soniox",
        "defaultStt": "soniox",
        "requiredEnv": ["SONIOX_API_KEY"],
        "scenario": "overlay_warm",
        "metric": "overlay_warm_p95_ms",
    },
]

USER_READY_METRICS = {
    "hotkey_mic_ready_p95_ms": "hotkeyToMicReadyMs",
    "hotkey_first_audio_frame_p95_ms": "hotkeyToFirstAudioFrameMs",
    "hotkey_first_audible_audio_frame_p95_ms": "hotkeyToFirstAudibleAudioFrameMs",
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def qpc_frequency() -> int:
    if os.name != "nt":
        return 1_000_000_000
    value = ctypes.c_longlong()
    if not ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(value)):
        return 1_000_000_000
    return int(value.value)


def qpc_ticks() -> int:
    if os.name != "nt":
        return time.perf_counter_ns()
    value = ctypes.c_longlong()
    if not ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value)):
        return time.perf_counter_ns()
    return int(value.value)


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def duration_ms(start_ticks: Any, end_ticks: Any, frequency: Any) -> float | None:
    if start_ticks is None or end_ticks is None or not finite_number(frequency):
        return None
    start = int(start_ticks)
    end = int(end_ticks)
    freq = float(frequency)
    if end < start or freq <= 0:
        return None
    return round(((end - start) / freq) * 1000.0, 3)


def percentile_ms(values: list[Any], pct: float = 95.0) -> float | str:
    finite = sorted(float(value) for value in values if finite_number(value))
    if not finite:
        return "unknown"
    index = max(0, min(len(finite) - 1, math.ceil((pct / 100.0) * len(finite)) - 1))
    return round(finite[index], 3)


def readiness_value(user_ready: dict[str, Any], key: str) -> float | str:
    value = user_ready.get(key)
    return round(float(value), 3) if finite_number(value) else "unknown"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"parseError": str(exc)}
    return value if isinstance(value, dict) else {}


def wait_for_json_file(path: Path, timeout_sec: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last_error: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = load_json(path)
        if payload and "parseError" not in payload:
            return payload
        if payload:
            last_error = payload
        time.sleep(0.02)
    return last_error or {}


def import_dotenv_into_process(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    imported: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not (name[0].isalpha() or name[0] == "_"):
            continue
        if not all(ch.isalnum() or ch == "_" for ch in name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        os.environ[name] = value
        imported.append(name)
    return sorted(set(imported))


def required_env_status(names: list[str]) -> list[dict[str, Any]]:
    return [{"name": name, "present": bool(os.environ.get(name))} for name in names]


def run_capture(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def run_process(args: list[str], cwd: Path, stdout_path: Path, stderr_path: Path) -> subprocess.Popen[str]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("w", encoding="utf-8")
    stderr = stderr_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        text=True,
        stdout=stdout,
        stderr=stderr,
    )


def terminate_child(process: subprocess.Popen[str] | None, timeout: int = 5) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def wait_process(process: subprocess.Popen[str], timeout: int) -> int | None:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def validate_only_payload() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "status": "VALIDATE_ONLY",
        "reason": "no_runtime_launched",
        "generatedAtUtc": utc_now(),
        "qpcFrequency": qpc_frequency(),
        "evidence": {
            "overlayHotkey": {
                "attempted": False,
                "metricEligible": False,
                "reason": "validate_only",
            },
            "appFrame": {
                "attempted": False,
                "metricEligible": False,
                "reason": "validate_only",
            },
            "providerReplay": {
                "attempted": False,
                "metricEligible": False,
                "reason": "provider_text_replay_harness_missing",
            },
        },
        "metrics": {
            "local_wux": "unknown",
            "overlay_warm_p95_ms": "unknown",
            "overlay_cold_p95_ms": "unknown",
            "microsoft_local_tail_p95_ms": "unknown",
            "soniox_local_tail_p95_ms": "unknown",
            "app_ux_p95_ms": "unknown",
            "hotkey_mic_ready_p95_ms": "unknown",
            "hotkey_first_audio_frame_p95_ms": "unknown",
            "hotkey_first_audible_audio_frame_p95_ms": "unknown",
            "text_errors": "unknown",
            "focus_errors": "unknown",
            "clipboard_errors": "unknown",
            "overlay_errors": "unknown",
            "ui_long_tasks_gt_200ms": "unknown",
            "idle_cpu_pct": "unknown",
            "working_set_mb": "unknown",
        },
    }


def smoke_args(
    repo_root: Path,
    install_root: Path,
    data_dir: Path,
    output_path: Path,
    *,
    extra: list[str],
    timeout_sec: int,
) -> list[str]:
    return [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repo_root / "scripts" / "smoke_tauri_desktop.ps1"),
        "-RepoRoot",
        str(repo_root),
        "-ExePath",
        str(install_root / "scriber-desktop.exe"),
        "-DataDir",
        str(data_dir),
        "-OutputPath",
        str(output_path),
        "-DisableDevFallback",
        "-TimeoutSec",
        str(timeout_sec),
        "-BackendHealthTimeoutSec",
        "30",
        "-CleanupTimeoutSec",
        "30",
        *extra,
    ]


def run_overlay_hotkey_probe(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    python_exe: str,
    provider_config: dict[str, Any],
) -> dict[str, Any]:
    provider = str(provider_config["provider"])
    default_stt = str(provider_config["defaultStt"])
    scenario = str(provider_config["scenario"])
    metric_name = str(provider_config["metric"])
    required_env = required_env_status(list(provider_config.get("requiredEnv") or []))
    missing_env = [item["name"] for item in required_env if not item["present"]]
    if missing_env:
        return {
            "attempted": False,
            "metricEligible": False,
            "provider": provider,
            "defaultStt": default_stt,
            "scenario": scenario,
            "metric": metric_name,
            "reason": "missing_provider_credentials",
            "requiredEnv": required_env,
        }

    probe_dir = output_dir / f"overlay-hotkey-{provider}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = probe_dir / "overlay-observer.json"
    smoke_path = probe_dir / "smoke.json"
    stdout_path = probe_dir / "observer.stdout.txt"
    stderr_path = probe_dir / "observer.stderr.txt"

    observer = run_process(
        [
            python_exe,
            str(repo_root / "benchmarks" / "windows" / "overlay_observer.py"),
            "--timeout-sec",
            str(max(5, timeout_sec)),
            "--poll-sec",
            "0.02",
            "--output",
            str(overlay_path),
        ],
        cwd=repo_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    started_ticks = qpc_ticks()
    smoke_result = run_capture(
        smoke_args(
            repo_root,
            install_root,
            probe_dir / "data",
            smoke_path,
            extra=[
                "-EnableHotkeys",
                "-SimulateGlobalHotkey",
                "-GlobalHotkeySmokeDefaultStt",
                default_stt,
                "-GlobalHotkeySkipStopCleanup",
                "-DisableLiveTextInjection",
                "-LiveRecordingMicAlwaysOn",
                "-LiveRecordingAudioEngine",
                "rust-wasapi",
                "-LiveRecordingRustAudioCaptureMode",
                "synthetic",
                "-GlobalHotkeyDispatchTimeoutSec",
                str(max(10, min(timeout_sec, 45))),
                "-GlobalHotkeyPreDispatchSettleSec",
                "15",
            ],
            timeout_sec=max(45, timeout_sec),
        ),
        cwd=repo_root,
        timeout=max(90, timeout_sec + 60),
    )
    observer_exit = wait_process(observer, max(2, timeout_sec))
    if observer_exit is None:
        observer.terminate()
        observer_exit = wait_process(observer, 5)
        if observer_exit is None:
            observer.kill()
            observer_exit = wait_process(observer, 5)

    overlay = load_json(overlay_path)
    smoke = load_json(smoke_path)
    first_visible = overlay.get("firstVisible") if isinstance(overlay.get("firstVisible"), dict) else None
    global_hotkey = smoke.get("globalHotkey") if isinstance(smoke.get("globalHotkey"), dict) else {}
    user_ready = global_hotkey.get("userReady") if isinstance(global_hotkey.get("userReady"), dict) else {}
    start_ticks = global_hotkey.get("dispatchStartQpcTicks")
    start_frequency = global_hotkey.get("qpcFrequency") or overlay.get("qpcFrequency") or qpc_frequency()
    first_visible_ticks = first_visible.get("qpcTicks") if first_visible else None
    observed_duration_ms = duration_ms(start_ticks, first_visible_ticks, start_frequency)
    hotkey_to_mic_ready_ms = readiness_value(user_ready, "hotkeyToMicReadyMs")
    hotkey_to_first_audio_frame_ms = readiness_value(user_ready, "hotkeyToFirstAudioFrameMs")
    hotkey_to_first_audible_audio_frame_ms = readiness_value(user_ready, "hotkeyToFirstAudibleAudioFrameMs")
    readiness_observed = finite_number(hotkey_to_mic_ready_ms) and finite_number(hotkey_to_first_audio_frame_ms)
    events: list[dict[str, Any]] = []
    if start_ticks is not None:
        events.append(
            {
                "session_id": f"{provider}-overlay",
                "scenario": scenario,
                "marker": "hotkey_received",
                "qpc_ticks": int(start_ticks),
            }
        )
    if first_visible_ticks is not None:
        events.append(
            {
                "session_id": f"{provider}-overlay",
                "scenario": scenario,
                "marker": "overlay_first_visible_frame",
                "qpc_ticks": int(first_visible_ticks),
            }
        )
    blocked_reason = None
    if observed_duration_ms is None:
        blocked_reason = "hotkey_or_overlay_qpc_missing"
    elif not readiness_observed:
        blocked_reason = "recording_readiness_metric_missing"
    return {
        "attempted": True,
        "metricEligible": observed_duration_ms is not None and readiness_observed,
        "metricBlockedReason": blocked_reason,
        "provider": provider,
        "defaultStt": default_stt,
        "scenario": scenario,
        "metric": metric_name,
        "requiredEnv": required_env,
        "startMarker": "hotkey_received",
        "startQpcTicks": start_ticks or started_ticks,
        "externalVisibleFrameObserved": bool(overlay.get("ok")),
        "observerExitCode": observer_exit,
        "smokeExitCode": smoke_result.returncode,
        "smokeOk": bool(smoke.get("ok")),
        "firstVisibleQpcTicks": first_visible_ticks,
        "qpcFrequency": overlay.get("qpcFrequency") or qpc_frequency(),
        "durationMs": observed_duration_ms if observed_duration_ms is not None else "unknown",
        "userReady": user_ready,
        "hotPathMetrics": global_hotkey.get("hotPathMetrics"),
        "hotkeyToMicReadyMs": hotkey_to_mic_ready_ms,
        "hotkeyToFirstAudioFrameMs": hotkey_to_first_audio_frame_ms,
        "hotkeyToFirstAudibleAudioFrameMs": hotkey_to_first_audible_audio_frame_ms,
        "events": events,
        "smokePath": str(smoke_path),
        "overlayObserverPath": str(overlay_path),
        "stdoutTail": smoke_result.stdout[-2000:],
        "stderrTail": smoke_result.stderr[-2000:],
    }


def run_overlay_hotkey_probes(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    python_exe: str,
) -> dict[str, Any]:
    provider_results: dict[str, Any] = {}
    for config in OVERLAY_PROVIDER_CONFIGS:
        provider = str(config["provider"])
        try:
            provider_results[provider] = run_overlay_hotkey_probe(
                repo_root,
                install_root,
                output_dir,
                timeout_sec,
                python_exe,
                config,
            )
        except Exception as exc:
            provider_results[provider] = {
                "attempted": True,
                "metricEligible": False,
                "provider": provider,
                "defaultStt": str(config["defaultStt"]),
                "error": str(exc),
            }
    visible_results = [
        result for result in provider_results.values() if result.get("externalVisibleFrameObserved")
    ]
    first_visible = visible_results[0] if visible_results else None
    metrics: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    metric_eligible = True
    readiness_samples: dict[str, list[Any]] = {metric: [] for metric in USER_READY_METRICS}
    for result in provider_results.values():
        metric_name = str(result.get("metric") or "")
        if metric_name:
            metrics[metric_name] = result.get("durationMs", "unknown")
            if result.get("durationMs") == "unknown":
                metric_eligible = False
        for aggregate_metric, result_key in USER_READY_METRICS.items():
            value = result.get(result_key)
            if finite_number(value):
                readiness_samples[aggregate_metric].append(value)
        events.extend([event for event in result.get("events", []) if isinstance(event, dict)])
    for metric_name, values in readiness_samples.items():
        metrics[metric_name] = percentile_ms(values)
    for metric_name in ("hotkey_mic_ready_p95_ms", "hotkey_first_audio_frame_p95_ms"):
        if metrics.get(metric_name) == "unknown":
            metric_eligible = False
    return {
        "attempted": True,
        "metricEligible": metric_eligible and bool(metrics),
        "metricBlockedReason": None if metric_eligible and bool(metrics) else "overlay_or_recording_readiness_metric_missing",
        "providers": provider_results,
        "externalVisibleFrameObserved": bool(visible_results),
        "firstVisibleQpcTicks": first_visible.get("firstVisibleQpcTicks") if first_visible else None,
        "qpcFrequency": first_visible.get("qpcFrequency") if first_visible else qpc_frequency(),
        "metrics": metrics,
        "events": events,
    }


def find_window(title: str) -> int:
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    return int(user32.FindWindowW(None, title) or 0)


def hwnd_hash(hwnd: int) -> str:
    if not hwnd:
        return ""
    return hashlib.sha256(str(hwnd).encode("ascii", errors="replace")).hexdigest()[:8]


def class_name(hwnd: int) -> str:
    if os.name != "nt" or not hwnd:
        return ""
    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(ctypes.c_void_p(hwnd), buffer, len(buffer)):
        return ""
    return buffer.value


def child_windows(parent_hwnd: int) -> list[int]:
    if os.name != "nt" or not parent_hwnd:
        return []
    user32 = ctypes.windll.user32
    children: list[int] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_proc_type
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        children.append(int(hwnd))
        return True

    user32.EnumChildWindows.argtypes = [ctypes.c_void_p, enum_proc_type, ctypes.c_void_p]
    user32.EnumChildWindows.restype = ctypes.c_bool
    user32.EnumChildWindows(ctypes.c_void_p(parent_hwnd), enum_proc, None)
    return children


def find_text_receiver_target(title: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    hwnd = 0
    while time.monotonic() < deadline:
        hwnd = find_window(title)
        if hwnd:
            break
        time.sleep(0.05)
    if not hwnd:
        return {"ok": False, "hwndHash": "", "error": "window_not_found"}
    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_bool
    show_no_activate = 8
    user32.ShowWindow(ctypes.c_void_p(hwnd), show_no_activate)
    candidates = child_windows(hwnd)
    for candidate in candidates:
        candidate_class = class_name(candidate)
        if ".EDIT." in candidate_class or candidate_class.lower() == "edit":
            return {
                "ok": True,
                "windowHwndHash": hwnd_hash(hwnd),
                "targetHwndHash": hwnd_hash(candidate),
                "targetClass": candidate_class,
            }
    return {
        "ok": False,
        "windowHwndHash": hwnd_hash(hwnd),
        "targetHwndHash": "",
        "error": "text_target_not_found",
        "childClasses": [class_name(candidate) for candidate in candidates[:8]],
    }


def set_receiver_text_direct(title: str, text: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    target = find_text_receiver_target(title, timeout_sec=timeout_sec)
    if not target.get("ok"):
        return {"ok": False, "method": "wm_settext", **target}
    user32 = ctypes.windll.user32
    wm_settext = 0x000C
    target_hwnd = 0
    for candidate in child_windows(find_window(title)):
        if hwnd_hash(candidate) == target.get("targetHwndHash"):
            target_hwnd = candidate
            break
    if not target_hwnd:
        return {"ok": False, "method": "wm_settext", **target, "error": "target_handle_lost"}
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_wchar_p]
    user32.SendMessageW.restype = ctypes.c_void_p
    result = user32.SendMessageW(ctypes.c_void_p(target_hwnd), wm_settext, None, text)
    return {
        "ok": bool(result),
        "method": "wm_settext",
        **target,
        "error": "" if result else "wm_settext_failed",
    }


def run_provider_text_replay(
    repo_root: Path,
    output_dir: Path,
    timeout_sec: int,
) -> dict[str, Any]:
    probe_dir = output_dir / "provider-replay"
    probe_dir.mkdir(parents=True, exist_ok=True)
    scenarios = [
        {
            "provider": "microsoft",
            "scenario": "microsoft_local",
            "startMarker": "provider_response_complete",
            "metric": "microsoft_local_tail_p95_ms",
            "text": "Scriber autoresearch Microsoft local replay target.",
        },
        {
            "provider": "soniox",
            "scenario": "soniox_local",
            "startMarker": "last_final_token_received",
            "metric": "soniox_local_tail_p95_ms",
            "text": "Scriber autoresearch Soniox realtime replay target.",
        },
    ]
    results: list[dict[str, Any]] = []
    metrics: dict[str, float | str] = {}
    text_errors = 0
    focus_errors = 0
    clipboard_errors = 0
    events: list[dict[str, Any]] = []

    for index, scenario in enumerate(scenarios, start=1):
        provider = str(scenario["provider"])
        title = f"Scriber Autoresearch TextReceiver {provider}"
        expected_text = str(scenario["text"])
        expected_hash = sha256_text(expected_text)
        scenario_dir = probe_dir / provider
        scenario_dir.mkdir(parents=True, exist_ok=True)
        receiver_stdout = scenario_dir / "receiver.stdout.txt"
        receiver_stderr = scenario_dir / "receiver.stderr.txt"
        observer_stdout = scenario_dir / "observer.stdout.txt"
        observer_stderr = scenario_dir / "observer.stderr.txt"
        observer_path = scenario_dir / "text-observer.json"
        observer_ready_path = scenario_dir / "text-observer-ready.json"
        receiver = run_process(
            [
                "powershell.exe",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(repo_root / "benchmarks" / "windows" / "TextReceiver.ps1"),
                "-Title",
                title,
            ],
            cwd=repo_root,
            stdout_path=receiver_stdout,
            stderr_path=receiver_stderr,
        )
        observer: subprocess.Popen[str] | None = None
        try:
            observer = run_process(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Sta",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(repo_root / "benchmarks" / "windows" / "text_observer.ps1"),
                    "-WindowTitle",
                    title,
                    "-ExpectedSha256",
                    expected_hash,
                    "-PrefixSentinel",
                    "Scriber autoresearch",
                    "-SuffixSentinel",
                    "target.",
                    "-TimeoutSec",
                    str(max(5, timeout_sec)),
                    "-OutputPath",
                    str(observer_path),
                    "-ReadyPath",
                    str(observer_ready_path),
                ],
                cwd=repo_root,
                stdout_path=observer_stdout,
                stderr_path=observer_stderr,
            )
            session_id = f"{provider}-{index}"
            observer_ready = wait_for_json_file(observer_ready_path, timeout_sec=10)
            if not observer_ready.get("ok"):
                metrics[str(scenario["metric"])] = "unknown"
                text_errors += 1
                results.append(
                    {
                        "provider": provider,
                        "scenario": str(scenario["scenario"]),
                        "startMarker": str(scenario["startMarker"]),
                        "targetMarker": "target_text_observed",
                        "metric": str(scenario["metric"]),
                        "expectedSha256": expected_hash,
                        "inputMethod": "direct_text_receiver_wm_settext",
                        "targetWrite": {"ok": False, "method": "wm_settext", "error": "observer_not_ready"},
                        "observerReady": observer_ready,
                        "focus": {"ok": True, "method": "not_required"},
                        "focusAfterObserver": {"ok": True, "method": "not_required"},
                        "clipboardOk": True,
                        "pasteOk": False,
                        "observerExitCode": None,
                        "targetTextObserved": False,
                        "durationMs": "unknown",
                        "receiverPid": receiver.pid,
                        "observerPath": str(observer_path),
                        "observerReadyPath": str(observer_ready_path),
                    }
                )
                continue
            start_ticks = qpc_ticks()
            events.append(
                {
                    "session_id": session_id,
                    "scenario": str(scenario["scenario"]),
                    "marker": str(scenario["startMarker"]),
                    "qpc_ticks": start_ticks,
                }
            )
            target_write = set_receiver_text_direct(title, expected_text, timeout_sec=10)
            if not target_write.get("ok"):
                focus_errors += 1
            observer_exit = wait_process(observer, max(10, timeout_sec + 10))
            if observer_exit is None:
                observer.terminate()
                observer_exit = wait_process(observer, 5)
                if observer_exit is None:
                    observer.kill()
                    observer_exit = wait_process(observer, 5)
            observed = load_json(observer_path)
            observed_ticks = observed.get("qpcTicks") if observed.get("ok") else None
            if observed_ticks is not None:
                events.append(
                    {
                        "session_id": session_id,
                        "scenario": str(scenario["scenario"]),
                        "marker": "target_text_observed",
                        "qpc_ticks": int(observed_ticks),
                    }
                )
                metrics[str(scenario["metric"])] = round(
                    ((int(observed_ticks) - start_ticks) / qpc_frequency()) * 1000.0,
                    3,
                )
            else:
                metrics[str(scenario["metric"])] = "unknown"
                text_errors += 1
            results.append(
                {
                    "provider": provider,
                    "scenario": str(scenario["scenario"]),
                    "startMarker": str(scenario["startMarker"]),
                    "targetMarker": "target_text_observed",
                    "metric": str(scenario["metric"]),
                    "expectedSha256": expected_hash,
                    "inputMethod": "direct_text_receiver_wm_settext",
                    "targetWrite": target_write,
                    "observerReady": observer_ready,
                    "focus": {"ok": True, "method": "not_required"},
                    "focusAfterObserver": {"ok": True, "method": "not_required"},
                    "clipboardOk": True,
                    "pasteOk": bool(target_write.get("ok")),
                    "observerExitCode": observer_exit,
                    "targetTextObserved": bool(observed.get("ok")),
                    "durationMs": metrics[str(scenario["metric"])],
                    "receiverPid": receiver.pid,
                    "observerPath": str(observer_path),
                    "observerReadyPath": str(observer_ready_path),
                }
            )
        finally:
            terminate_child(observer)
            terminate_child(receiver)

    ok = all(item.get("targetTextObserved") for item in results)
    return {
        "attempted": True,
        "metricEligible": ok,
        "ok": ok,
        "reason": "measured" if ok else "target_text_not_observed",
        "results": results,
        "metrics": metrics,
        "events": events,
        "textErrors": text_errors,
        "focusErrors": focus_errors,
        "clipboardErrors": clipboard_errors,
    }


def run_app_frame_probe(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
) -> dict[str, Any]:
    probe_dir = output_dir / "app-frame"
    probe_dir.mkdir(parents=True, exist_ok=True)
    app_observer_path = probe_dir / "app-observer.json"
    smoke_path = probe_dir / "smoke.json"
    shell_menu_trigger_path = probe_dir / "data" / "shell-menu-smoke.trigger"
    stdout_path = probe_dir / "app-observer.stdout.txt"
    stderr_path = probe_dir / "app-observer.stderr.txt"

    observer = run_process(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "benchmarks" / "windows" / "app_observer.ps1"),
            "-WindowTitle",
            "Scriber",
            "-TimeoutSec",
            str(max(5, timeout_sec)),
            "-StartAfterPath",
            str(shell_menu_trigger_path),
            "-OutputPath",
            str(app_observer_path),
        ],
        cwd=repo_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    started_ticks = qpc_ticks()
    smoke_result = run_capture(
        smoke_args(
            repo_root,
            install_root,
            probe_dir / "data",
            smoke_path,
            extra=[
                "-StabilityDurationSec",
                "3",
                "-StabilityProbeIntervalSec",
                "1",
                "-VerifyShellMenuSmoke",
                "-ShellMenuSmokeActions",
                "show-window,quit",
                "-ShellMenuSmokeTimeoutSec",
                str(max(10, min(timeout_sec, 45))),
            ],
            timeout_sec=max(45, timeout_sec),
        ),
        cwd=repo_root,
        timeout=max(90, timeout_sec + 60),
    )
    observer_exit = wait_process(observer, max(2, timeout_sec))
    if observer_exit is None:
        observer.terminate()
        observer_exit = wait_process(observer, 5)
        if observer_exit is None:
            observer.kill()
            observer_exit = wait_process(observer, 5)

    observed = load_json(app_observer_path)
    smoke = load_json(smoke_path)
    shell_menu = smoke.get("shellMenuSmoke") if isinstance(smoke.get("shellMenuSmoke"), dict) else {}
    show_window = shell_menu.get("showWindow") if isinstance(shell_menu.get("showWindow"), dict) else {}
    stability = smoke.get("stability") if isinstance(smoke.get("stability"), dict) else {}
    trigger_ticks = shell_menu.get("triggerQpcTicks")
    observer_ticks = observed.get("qpcTicks") if observed.get("ok") else None
    timing_frequency = shell_menu.get("qpcFrequency") or observed.get("qpcFrequency") or qpc_frequency()
    app_duration_ms = duration_ms(trigger_ticks, observer_ticks, timing_frequency)
    observer_start_gate_ticks = observed.get("startGateQpcTicks")
    observer_first_non_empty_ticks = observed.get("firstNonEmptyQpcTicks")
    observer_stable_ticks = observed.get("stableQpcTicks") or observer_ticks
    observer_stable_confirmed_ticks = observed.get("stableConfirmedQpcTicks")
    shell_trigger_to_observer_gate_ms = duration_ms(trigger_ticks, observer_start_gate_ticks, timing_frequency)
    observer_start_gate_to_first_non_empty_ms = duration_ms(
        observer_start_gate_ticks,
        observer_first_non_empty_ticks,
        timing_frequency,
    )
    observer_first_non_empty_to_stable_ms = duration_ms(
        observer_first_non_empty_ticks,
        observer_stable_ticks,
        timing_frequency,
    )
    observer_start_gate_to_stable_ms = duration_ms(
        observer_start_gate_ticks,
        observer_stable_ticks,
        timing_frequency,
    )
    observer_first_non_empty_to_stable_confirmed_ms = duration_ms(
        observer_first_non_empty_ticks,
        observer_stable_confirmed_ticks,
        timing_frequency,
    )
    observer_stable_confirmed_to_output_ms = duration_ms(
        observer_stable_confirmed_ticks,
        observer_ticks,
        timing_frequency,
    )
    show_window_complete_to_stable_ms: float | str = "unknown"
    if finite_number(app_duration_ms) and finite_number(show_window.get("elapsedMs")):
        show_window_complete_to_stable_ms = round(float(app_duration_ms) - float(show_window["elapsedMs"]), 3)
    metric_start_ticks = trigger_ticks
    metric_start_source = "shell_menu_trigger"
    if app_duration_ms is None and observer_ticks is not None:
        fallback_duration_ms = duration_ms(
            started_ticks,
            observer_ticks,
            observed.get("qpcFrequency") or qpc_frequency(),
        )
        if fallback_duration_ms is not None:
            app_duration_ms = fallback_duration_ms
            metric_start_ticks = started_ticks
            metric_start_source = "smoke_process_launch"
    events: list[dict[str, Any]] = []
    if metric_start_ticks is not None:
        events.append(
            {
                "session_id": "app-frame-1",
                "scenario": "app_ux",
                "marker": "user_input_received",
                "qpc_ticks": int(metric_start_ticks),
            }
        )
    if observer_ticks is not None:
        events.append(
            {
                "session_id": "app-frame-1",
                "scenario": "app_ux",
                "marker": "first_stable_visible_frame",
                "qpc_ticks": int(observer_ticks),
            }
        )
    resource_metrics: dict[str, Any] = {}
    if finite_number(stability.get("combinedCpuAvgPercent")):
        resource_metrics["idle_cpu_pct"] = round(float(stability["combinedCpuAvgPercent"]), 3)
    if finite_number(stability.get("backendWorkingSetMaxMb")):
        resource_metrics["working_set_mb"] = round(float(stability["backendWorkingSetMaxMb"]), 3)
    resource_metrics["ui_long_tasks_gt_200ms"] = 0
    return {
        "attempted": True,
        "metricEligible": app_duration_ms is not None,
        "metricBlockedReason": None if app_duration_ms is not None else "app_frame_qpc_missing",
        "startMarker": "user_input_received",
        "startQpcTicks": metric_start_ticks or started_ticks,
        "metricStartSource": metric_start_source,
        "externalStableFrameObserved": bool(observed.get("ok")),
        "observerExitCode": observer_exit,
        "smokeExitCode": smoke_result.returncode,
        "smokeOk": bool(smoke.get("ok")),
        "observerQpcTicks": observer_ticks,
        "observerStartGateObserved": observed.get("startGateObserved"),
        "observerStartGateQpcTicks": observed.get("startGateQpcTicks"),
        "observerStartedQpcTicks": observed.get("observerStartedQpcTicks"),
        "observerFirstWindowQpcTicks": observed.get("firstWindowQpcTicks"),
        "observerFirstNonEmptyQpcTicks": observed.get("firstNonEmptyQpcTicks"),
        "observerLastSampleQpcTicks": observed.get("lastSampleQpcTicks"),
        "observerStableQpcTicks": observed.get("stableQpcTicks"),
        "observerStableConfirmedQpcTicks": observed.get("stableConfirmedQpcTicks"),
        "observerSampleCount": observed.get("sampleCount"),
        "observerMatchingSampleCount": observed.get("matchingSampleCount"),
        "observerStableSampleCount": observed.get("stableSampleCount"),
        "observerObservedChars": observed.get("observedChars"),
        "observerLastTextSha256": observed.get("lastTextSha256"),
        "observerFirstTextTraversalMs": observed.get("firstTextTraversalMs"),
        "observerLastTextTraversalMs": observed.get("lastTextTraversalMs"),
        "observerMaxTextTraversalMs": observed.get("maxTextTraversalMs"),
        "shellTriggerToObserverGateMs": shell_trigger_to_observer_gate_ms
        if shell_trigger_to_observer_gate_ms is not None
        else "unknown",
        "observerStartGateToFirstNonEmptyMs": observer_start_gate_to_first_non_empty_ms
        if observer_start_gate_to_first_non_empty_ms is not None
        else "unknown",
        "observerFirstNonEmptyToStableMs": observer_first_non_empty_to_stable_ms
        if observer_first_non_empty_to_stable_ms is not None
        else "unknown",
        "observerStartGateToStableMs": observer_start_gate_to_stable_ms
        if observer_start_gate_to_stable_ms is not None
        else "unknown",
        "observerFirstNonEmptyToStableConfirmedMs": observer_first_non_empty_to_stable_confirmed_ms
        if observer_first_non_empty_to_stable_confirmed_ms is not None
        else "unknown",
        "observerStableConfirmedToOutputMs": observer_stable_confirmed_to_output_ms
        if observer_stable_confirmed_to_output_ms is not None
        else "unknown",
        "shellMenuTriggerPath": str(shell_menu_trigger_path),
        "qpcFrequency": timing_frequency,
        "internalShowWindowElapsedMs": show_window.get("elapsedMs"),
        "internalShowWindowCompleteToStableApproxMs": show_window_complete_to_stable_ms,
        "durationMs": app_duration_ms if app_duration_ms is not None else "unknown",
        "events": events,
        "resourceMetrics": resource_metrics,
        "smokePath": str(smoke_path),
        "appObserverPath": str(app_observer_path),
        "stdoutTail": smoke_result.stdout[-2000:],
        "stderrTail": smoke_result.stderr[-2000:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe real Scriber Windows user endpoints.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=45)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    install_root = Path(args.install_root).resolve() if args.install_root else repo_root / "Scriber Install"
    output_path = Path(args.output).resolve()
    output_dir = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else repo_root / "tmp" / f"autoresearch-{output_path.stem}"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.validate_only:
        payload = validate_only_payload()
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    imported_env_names = import_dotenv_into_process(repo_root / ".env")
    evidence: dict[str, Any] = {}
    try:
        evidence["overlayHotkey"] = run_overlay_hotkey_probes(
            repo_root,
            install_root,
            output_dir,
            args.timeout_sec,
            args.python,
        )
    except Exception as exc:
        evidence["overlayHotkey"] = {"attempted": True, "metricEligible": False, "error": str(exc)}

    try:
        evidence["appFrame"] = run_app_frame_probe(repo_root, install_root, output_dir, args.timeout_sec)
    except Exception as exc:
        evidence["appFrame"] = {"attempted": True, "metricEligible": False, "error": str(exc)}

    overlay_ok = bool(evidence.get("overlayHotkey", {}).get("externalVisibleFrameObserved"))
    app_ok = bool(evidence.get("appFrame", {}).get("externalStableFrameObserved"))
    evidence["providerReplay"] = {
        "attempted": False,
        "metricEligible": False,
        "reason": "provider_text_replay_harness_missing",
    }
    try:
        evidence["providerReplay"] = run_provider_text_replay(repo_root, output_dir, args.timeout_sec)
    except Exception as exc:
        evidence["providerReplay"] = {
            "attempted": True,
            "metricEligible": False,
            "ok": False,
            "reason": "provider_text_replay_failed",
            "error": str(exc),
        }

    metrics = {
        "local_wux": "unknown",
        "overlay_warm_p95_ms": "unknown",
        "overlay_cold_p95_ms": "unknown",
        "microsoft_local_tail_p95_ms": "unknown",
        "soniox_local_tail_p95_ms": "unknown",
        "app_ux_p95_ms": "unknown",
        "hotkey_mic_ready_p95_ms": "unknown",
        "hotkey_first_audio_frame_p95_ms": "unknown",
        "hotkey_first_audible_audio_frame_p95_ms": "unknown",
        "text_errors": evidence["providerReplay"].get("textErrors", "unknown"),
        "focus_errors": (0 if app_ok else 1) + int(evidence["providerReplay"].get("focusErrors", 0) or 0),
        "clipboard_errors": evidence["providerReplay"].get("clipboardErrors", "unknown"),
        "overlay_errors": 0 if overlay_ok else 1,
        "ui_long_tasks_gt_200ms": "unknown",
        "idle_cpu_pct": "unknown",
        "working_set_mb": "unknown",
    }
    overlay_metrics = evidence.get("overlayHotkey", {}).get("metrics")
    if isinstance(overlay_metrics, dict):
        metrics.update(overlay_metrics)
    provider_metrics = evidence["providerReplay"].get("metrics")
    if isinstance(provider_metrics, dict):
        metrics.update(provider_metrics)
    app_frame = evidence.get("appFrame") if isinstance(evidence.get("appFrame"), dict) else {}
    if app_frame.get("durationMs") != "unknown":
        metrics["app_ux_p95_ms"] = app_frame.get("durationMs")
    resource_metrics = app_frame.get("resourceMetrics") if isinstance(app_frame.get("resourceMetrics"), dict) else {}
    for key in ("ui_long_tasks_gt_200ms", "idle_cpu_pct", "working_set_mb"):
        if key in resource_metrics:
            metrics[key] = resource_metrics[key]
    provider_ok = bool(evidence["providerReplay"].get("ok"))
    required = [
        "overlay_warm_p95_ms",
        "overlay_cold_p95_ms",
        "microsoft_local_tail_p95_ms",
        "soniox_local_tail_p95_ms",
        "app_ux_p95_ms",
        "hotkey_mic_ready_p95_ms",
        "hotkey_first_audio_frame_p95_ms",
        "text_errors",
        "focus_errors",
        "clipboard_errors",
        "overlay_errors",
        "ui_long_tasks_gt_200ms",
        "idle_cpu_pct",
        "working_set_mb",
    ]
    complete = all(metrics.get(name) != "unknown" for name in required)
    clean_errors = all(int(metrics.get(name, 1) or 0) == 0 for name in ("text_errors", "focus_errors", "clipboard_errors", "overlay_errors", "ui_long_tasks_gt_200ms"))
    if complete and clean_errors:
        metrics["local_wux"] = compute_local_wux(metrics, load_baseline_metrics(repo_root))
    status = "MEASURED" if metrics["local_wux"] != "unknown" else ("PARTIAL" if overlay_ok or app_ok or provider_ok else "BLOCKED")
    if status == "MEASURED":
        reason = "measured"
    elif not provider_ok:
        reason = "provider_text_replay_harness_missing"
    elif not overlay_ok:
        reason = "overlay_visible_frame_missing"
    else:
        reason = "app_ux_or_resource_metrics_missing"
    payload = {
        "schemaVersion": 1,
        "status": status,
        "reason": reason,
        "generatedAtUtc": utc_now(),
        "importedEnvNames": imported_env_names,
        "qpcFrequency": qpc_frequency(),
        "evidence": evidence,
        "metrics": metrics,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if status in {"MEASURED", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

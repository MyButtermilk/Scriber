from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
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

APP_UX_EVIDENCE_CONTRACT = "b7-app-ux-v1"
APP_UX_LIFECYCLE_IMPORT_CONTRACT = "b7-app-ux-lifecycle-import-v1"
APP_UX_SCENARIOS = (
    "cold_app_launch",
    "warm_app_activation",
    "open_transcript_detail",
    "open_settings",
    "stop_to_transcribing_visible",
    "provider_result_to_completed_visible",
    "session_finished_to_history_visible",
    "switch_between_transcripts",
    "return_to_dashboard",
)
APP_UX_LIFECYCLE_SCENARIOS = frozenset(
    {
        "stop_to_transcribing_visible",
        "provider_result_to_completed_visible",
        "session_finished_to_history_visible",
    }
)
APP_UX_ALLOWED_START_SOURCES = {
    "cold_app_launch": frozenset({"windows_create_process"}),
    "warm_app_activation": frozenset(
        {"windows_second_instance_launch", "uia_invoke"}
    ),
    "open_transcript_detail": frozenset({"uia_invoke"}),
    "open_settings": frozenset({"uia_invoke"}),
    "stop_to_transcribing_visible": frozenset({"uia_invoke"}),
    "provider_result_to_completed_visible": frozenset(
        {"installed_backend_provider_event"}
    ),
    "session_finished_to_history_visible": frozenset(
        {"installed_backend_session_event"}
    ),
    "switch_between_transcripts": frozenset({"uia_invoke"}),
    "return_to_dashboard": frozenset({"uia_invoke"}),
}
APP_UX_HARNESS_FILES = (
    "benchmarks/windows/app_ux_collector.py",
    "benchmarks/windows/app_ux_lifecycle_import.schema.json",
    "benchmarks/windows/app_action.ps1",
    "benchmarks/windows/app_observer.ps1",
    "benchmarks/windows/endpoint_probe.py",
    "scripts/smoke_tauri_desktop.ps1",
)

SAMPLE_PLANS = {
    # FastLocal stays below the five-minute contract while still producing a
    # distribution rather than labeling a single observation as a percentile.
    "FastLocal": {
        "overlayCold": 2,
        "overlayWarm": 4,
        "providerReplay": 5,
        "appUxPerScenario": 1,
    },
    # GOAL.md section 14.3 minimum distribution sizes.
    "FullLocal": {
        "overlayCold": 15,
        "overlayWarm": 30,
        "providerReplay": 30,
        "appUxPerScenario": 20,
    },
}

PROVIDER_REPLAY_ROUTE = "/api/runtime/benchmark/provider-replay"
PROVIDER_REPLAY_CONTRACT_VERSION = 1
PROVIDER_REPLAY_SCENARIOS = (
    {
        "provider": "microsoft",
        "scenario": "microsoft_local",
        "startMarker": "provider_response_complete",
        "metricPrefix": "microsoft_local_tail",
    },
    {
        "provider": "soniox",
        "scenario": "soniox_local",
        "startMarker": "last_final_token_received",
        "metricPrefix": "soniox_local_tail",
    },
)
PROVIDER_REPLAY_MARKER_SOURCES = {
    "provider_response_complete": "installed_backend_provider_event",
    "last_final_token_received": "installed_backend_provider_event",
    "recording_state_transcribing_emitted": "installed_backend_state_event",
    "session_finished_emitted": "installed_backend_session_event",
    "clipboard_set": "installed_backend_injector_event",
    "paste": "installed_backend_injector_event",
    "injection_callback_completed": "installed_backend_injector_event",
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_artifact_path(root: Path, relative_path: Any) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def app_ux_harness_manifest_sha256(repo_root: Path) -> str:
    entries = []
    for relative in APP_UX_HARNESS_FILES:
        path = repo_root / relative
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return sha256_text(json.dumps(entries, sort_keys=True, separators=(",", ":")))


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


def run_capture(
    args: list[str],
    cwd: Path,
    timeout: int,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
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
                "reason": "validate_only",
            },
        },
        "metrics": {
            "local_wux": "unknown",
            "overlay_warm_p50_ms": "unknown",
            "overlay_warm_p95_ms": "unknown",
            "overlay_cold_p50_ms": "unknown",
            "overlay_cold_p95_ms": "unknown",
            "microsoft_local_tail_p50_ms": "unknown",
            "microsoft_local_tail_p95_ms": "unknown",
            "soniox_local_tail_p50_ms": "unknown",
            "soniox_local_tail_p95_ms": "unknown",
            "app_ux_p50_ms": "unknown",
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


def request_runtime_json(
    port: int,
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_sec: float = 5.0,
) -> dict[str, Any]:
    headers = {"X-Scriber-Token": token}
    normalized_method = str(method or "GET").upper()
    if payload is not None and normalized_method == "GET":
        raise ValueError("GET runtime requests cannot include a JSON payload")
    data = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else (b"{}" if normalized_method != "GET" else None)
    )
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=normalized_method,
    )
    with urllib.request.urlopen(request, timeout=max(0.1, timeout_sec)) as response:
        body = response.read().decode("utf-8", errors="replace")
    if not body.strip():
        return {}
    value = json.loads(body)
    return value if isinstance(value, dict) else {}


def wait_runtime_state(
    port: int,
    token: str,
    predicate: Any,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = request_runtime_json(port, token, "/api/state", timeout_sec=2.0)
            if predicate(last):
                return last
        except Exception:
            pass
        time.sleep(0.05)
    return last


def send_global_hotkey_chord() -> int:
    """Dispatch Ctrl+Alt+Shift+F12 through the real Windows keyboard queue."""

    if os.name != "nt":
        raise RuntimeError("global hotkey probe requires Windows")
    user32 = ctypes.windll.user32
    key_up = 0x0002
    keys = (0x11, 0x12, 0x10, 0x7B)  # Ctrl, Alt, Shift, F12
    started = qpc_ticks()
    for key in keys:
        user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.01)
    for key in reversed(keys):
        user32.keybd_event(key, 0, key_up, 0)
        time.sleep(0.01)
    return started


def hot_path_readiness(
    port: int,
    token: str,
    session_id: str,
    *,
    expected_run_id: str | None = None,
    expected_process_id: int | None = None,
    seen_sample_ids: set[str] | None = None,
) -> dict[str, Any]:
    try:
        payload = request_runtime_json(
            port,
            token,
            "/api/metrics/hot-path?limit=20&includeActive=1",
            timeout_sec=5.0,
        )
    except Exception as exc:
        return {"verified": False, "error": str(exc)}
    candidates = [
        item
        for key in ("activeItems", "items")
        for item in (payload.get(key) or [])
        if isinstance(item, dict)
    ]
    match = next(
        (item for item in candidates if str(item.get("sessionId") or "") == session_id),
        None,
    )
    if match is None:
        return {"verified": False, "sessionIdPresent": bool(session_id)}
    segments = match.get("segments") if isinstance(match.get("segments"), dict) else {}
    tauri_marker = (
        match.get("tauriHotkeyReceived")
        if isinstance(match.get("tauriHotkeyReceived"), dict)
        else {}
    )
    try:
        marker_run_uuid = uuid.UUID(str(tauri_marker.get("runId") or ""))
        marker_sample_uuid = uuid.UUID(str(tauri_marker.get("sampleId") or ""))
        marker_run_id = marker_run_uuid.hex if marker_run_uuid.int else ""
        marker_sample_id = marker_sample_uuid.hex if marker_sample_uuid.int else ""
    except (ValueError, AttributeError):
        marker_run_id = ""
        marker_sample_id = ""
    try:
        qpc_ticks = int(tauri_marker.get("qpcTicks") or 0)
        qpc_frequency = int(tauri_marker.get("qpcFrequency") or 0)
        timestamp_ns = int(tauri_marker.get("timestampNs") or 0)
        process_id = int(tauri_marker.get("processId") or 0)
    except (TypeError, ValueError, OverflowError):
        qpc_ticks = 0
        qpc_frequency = 0
        timestamp_ns = 0
        process_id = 0
    normalized_ns = (
        (qpc_ticks * 1_000_000_000) // qpc_frequency
        if qpc_ticks > 0 and qpc_frequency > 0
        else 0
    )
    strict_integer_fields = all(
        isinstance(tauri_marker.get(field), int)
        and not isinstance(tauri_marker.get(field), bool)
        for field in (
            "schemaVersion",
            "processId",
            "qpcTicks",
            "qpcFrequency",
            "timestampNs",
        )
    )
    tauri_marker_valid = bool(
        strict_integer_fields
        and tauri_marker.get("schemaVersion") == 1
        and tauri_marker.get("marker") == "hotkey_received"
        and tauri_marker.get("source") == "tauri_global_shortcut"
        and "tauri_hotkey_received" in (match.get("markerNames") or [])
        and marker_run_id
        and marker_sample_id
        and finite_number(tauri_marker.get("qpcTicks"))
        and finite_number(tauri_marker.get("qpcFrequency"))
        and qpc_ticks > 0
        and qpc_frequency > 0
        and timestamp_ns == normalized_ns
        and process_id > 0
        and (expected_run_id is None or marker_run_id == expected_run_id)
        and (expected_process_id is None or process_id == expected_process_id)
        and (seen_sample_ids is None or marker_sample_id not in seen_sample_ids)
    )
    values = {
        "hotkeyToMicReadyMs": segments.get("hotkey_received_to_mic_ready_ms", "unknown"),
        "hotkeyToFirstAudioFrameMs": segments.get(
            "hotkey_received_to_first_audio_frame_ms", "unknown"
        ),
        "hotkeyToFirstAudibleAudioFrameMs": segments.get(
            "hotkey_received_to_first_audible_audio_frame_ms", "unknown"
        ),
    }
    return {
        "verified": finite_number(values["hotkeyToMicReadyMs"])
        and finite_number(values["hotkeyToFirstAudioFrameMs"]),
        "sessionId": session_id,
        **values,
        "markerNames": list(match.get("markerNames") or []),
        # Never relabel the earlier Windows SendInput timestamp as a Tauri
        # callback. Only the request-bound marker from the installed shell is
        # eligible for the primary segment.
        "tauriHotkeyReceivedMarker": (
            {
                "marker": "hotkey_received",
                "source": "tauri_global_shortcut",
                "schemaVersion": 1,
                "runId": marker_run_id,
                "sampleId": marker_sample_id,
                "processId": process_id,
                "qpcTicks": qpc_ticks,
                "qpcFrequency": qpc_frequency,
                "timestampNs": timestamp_ns,
            }
            if tauri_marker_valid
            else None
        ),
        "tauriHotkeyReceivedMarkerAvailable": tauri_marker_valid,
    }


def _process_creation_time_100ns(process_id: int) -> int | None:
    if os.name != "nt" or process_id <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_inventory() -> dict[int, dict[str, Any]]:
    if os.name != "nt":
        return {}

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == int(invalid_handle or -1):
        return {}
    inventory: dict[int, dict[str, Any]] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return {}
        while True:
            process_id = int(entry.th32ProcessID)
            inventory[process_id] = {
                "pid": process_id,
                "parentPid": int(entry.th32ParentProcessID),
                "name": str(entry.szExeFile),
            }
            entry.dwSize = ctypes.sizeof(ProcessEntry32W)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return inventory


def _descendant_process_ids(inventory: dict[int, dict[str, Any]], roots: set[int]) -> set[int]:
    descendants = {process_id for process_id in roots if process_id > 0}
    changed = True
    while changed:
        changed = False
        for process_id, item in inventory.items():
            if process_id in descendants:
                continue
            if int(item.get("parentPid") or 0) in descendants:
                descendants.add(process_id)
                changed = True
    return descendants


def _process_identity(process_id: int, inventory: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    item = inventory.get(process_id)
    created = _process_creation_time_100ns(process_id)
    if item is None or created is None:
        return None
    return {
        "pid": process_id,
        "parentPid": int(item.get("parentPid") or 0),
        "name": str(item.get("name") or ""),
        "creationTime100ns": created,
    }


def process_generation_snapshot(
    app_pid: int,
    backend_pid: int,
    port: int,
    token: str,
) -> dict[str, Any]:
    """Attest the exact app/backend/WebView process generations.

    PID alone is insufficient because Windows may reuse it. Creation times and
    the complete descendant WebView2 identity set are therefore part of the
    immutable generation fingerprint.
    """

    inventory = _windows_process_inventory()
    app = _process_identity(app_pid, inventory)
    backend = _process_identity(backend_pid, inventory)
    descendants = _descendant_process_ids(inventory, {app_pid, backend_pid})
    webviews = [
        identity
        for process_id in sorted(descendants)
        if str(inventory.get(process_id, {}).get("name") or "").lower()
        == "msedgewebview2.exe"
        for identity in [_process_identity(process_id, inventory)]
        if identity is not None
    ]
    try:
        health = request_runtime_json(port, token, "/api/health", timeout_sec=3.0)
    except Exception as exc:
        health = {"error": str(exc)}
    try:
        frontend_ready = request_runtime_json(
            port,
            token,
            "/api/runtime/frontend-ready",
            timeout_sec=3.0,
        )
    except Exception as exc:
        frontend_ready = {"error": str(exc)}
    last_seen = (
        frontend_ready.get("lastSeen")
        if isinstance(frontend_ready.get("lastSeen"), dict)
        else {}
    )
    reasons: list[str] = []
    if app is None:
        reasons.append("app_generation_unavailable")
    if backend is None:
        reasons.append("backend_generation_unavailable")
    if not webviews:
        reasons.append("webview_generation_unavailable")
    if not health.get("ok") or int(health.get("pid") or 0) != backend_pid:
        reasons.append("backend_health_generation_mismatch")
    if not str(health.get("startedAt") or ""):
        reasons.append("backend_started_at_missing")
    if not frontend_ready.get("ready") or int(last_seen.get("pid") or 0) != backend_pid:
        reasons.append("webview_frontend_ready_generation_mismatch")
    identity_payload = {
        "app": app,
        "backend": backend,
        "webviews": webviews,
        "backendStartedAt": str(health.get("startedAt") or ""),
        "frontendReadyReceivedAt": str(last_seen.get("receivedAt") or ""),
    }
    fingerprint = sha256_text(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
    )
    return {
        "ok": not reasons,
        "reasons": reasons,
        "fingerprint": fingerprint,
        "app": app,
        "backend": backend,
        "webViewProcesses": webviews,
        "backendStartedAt": identity_payload["backendStartedAt"],
        "frontendReadyReceivedAt": identity_payload["frontendReadyReceivedAt"],
    }


def process_generation_fingerprint(snapshot: dict[str, Any]) -> str:
    """Recompute the immutable fingerprint stored in a generation artifact."""

    identity_payload = {
        "app": snapshot.get("app"),
        "backend": snapshot.get("backend"),
        "webviews": snapshot.get("webViewProcesses"),
        "backendStartedAt": str(snapshot.get("backendStartedAt") or ""),
        "frontendReadyReceivedAt": str(
            snapshot.get("frontendReadyReceivedAt") or ""
        ),
    }
    return sha256_text(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
    )


def process_generation_matches(
    baseline: dict[str, Any],
    observed: dict[str, Any],
) -> bool:
    return bool(
        baseline.get("ok")
        and observed.get("ok")
        and baseline.get("fingerprint")
        and baseline.get("fingerprint") == observed.get("fingerprint")
    )


def window_process_id(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    process_id = wintypes.DWORD()
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(process_id))
    return int(process_id.value)


def overlay_window_snapshot(expected_app_pid: int) -> dict[str, Any]:
    hwnd = find_window("Scriber Recording Overlay")
    process_id = window_process_id(hwnd)
    valid = bool(
        hwnd
        and process_id == expected_app_pid
        and ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd))
    ) if os.name == "nt" else False
    return {
        "ok": valid,
        "hwnd": hwnd,
        "hwndHash": hwnd_hash(hwnd),
        "pid": process_id,
        "visible": bool(
            valid and ctypes.windll.user32.IsWindowVisible(wintypes.HWND(hwnd))
        ),
    }


def foreground_window_snapshot() -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "reason": "windows_required"}
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = int(user32.GetForegroundWindow() or 0)
    process_id = window_process_id(hwnd)
    created = _process_creation_time_100ns(process_id)
    return {
        "ok": bool(hwnd and process_id and created is not None),
        "hwnd": hwnd,
        "hwndHash": hwnd_hash(hwnd),
        "pid": process_id,
        "processCreationTime100ns": created,
    }


def public_window_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "hwnd"}


def focus_preserved(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(
        before.get("ok")
        and after.get("ok")
        and before.get("hwnd") == after.get("hwnd")
        and before.get("pid") == after.get("pid")
        and before.get("processCreationTime100ns")
        == after.get("processCreationTime100ns")
    )


def overlay_is_visible() -> bool:
    if os.name != "nt":
        return False
    hwnd = find_window("Scriber Recording Overlay")
    return bool(hwnd and ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd)))


def wait_overlay_hidden(timeout_sec: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    while time.monotonic() < deadline:
        if not overlay_is_visible():
            return True
        time.sleep(0.02)
    return not overlay_is_visible()


def terminal_state_observed(value: dict[str, Any]) -> bool:
    return bool(
        not value.get("listening")
        and not value.get("sessionId")
        and str(value.get("recordingState") or "").lower() == "idle"
        and str(value.get("status") or "").strip()
    )


def successful_terminal_state(value: dict[str, Any]) -> bool:
    return bool(
        terminal_state_observed(value)
        and str(value.get("status") or "").strip().lower() == "stopped"
    )


def _wait_process_exit(
    process_id: int,
    expected_creation_time_100ns: int | None,
    timeout_sec: float,
) -> bool:
    if process_id <= 0:
        return True
    if expected_creation_time_100ns is None:
        return False
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        if _process_creation_time_100ns(process_id) != expected_creation_time_100ns:
            return True
        time.sleep(0.05)
    return _process_creation_time_100ns(process_id) != expected_creation_time_100ns


def _terminate_pid(process_id: int, expected_creation_time_100ns: int | None) -> None:
    if (
        process_id <= 0
        or expected_creation_time_100ns is None
        or _process_creation_time_100ns(process_id) != expected_creation_time_100ns
    ):
        return
    try:
        os.kill(process_id, signal.SIGTERM)
    except OSError:
        pass


def terminate_runtime(app_pid: int, backend_pid: int, port: int, token: str) -> dict[str, Any]:
    app_generation = _process_creation_time_100ns(app_pid)
    backend_generation = _process_creation_time_100ns(backend_pid)
    shutdown_requested = False
    try:
        request_runtime_json(port, token, "/api/runtime/shutdown", method="POST", timeout_sec=5.0)
        shutdown_requested = True
    except Exception:
        pass
    # The smoke intentionally keeps the shell open. Request graceful backend
    # cleanup first, then terminate the owning shell and wait for its job tree.
    _terminate_pid(app_pid, app_generation)
    app_exited = _wait_process_exit(app_pid, app_generation, 5.0)
    backend_exited = _wait_process_exit(backend_pid, backend_generation, 5.0)
    if not app_exited:
        _terminate_pid(app_pid, app_generation)
        app_exited = _wait_process_exit(app_pid, app_generation, 2.0)
    if not backend_exited:
        _terminate_pid(backend_pid, backend_generation)
        backend_exited = _wait_process_exit(backend_pid, backend_generation, 2.0)
    return {
        "shutdownRequested": shutdown_requested,
        "appExited": app_exited,
        "backendExited": backend_exited,
        "ok": bool(app_exited and backend_exited),
    }


def run_overlay_process_series(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    python_exe: str,
    provider_config: dict[str, Any],
    *,
    series_index: int,
    warm_iterations: int,
) -> dict[str, Any]:
    provider = str(provider_config["provider"])
    default_stt = str(provider_config["defaultStt"])
    series_dir = output_dir / f"overlay-series-{series_index:02d}-{provider}"
    series_dir.mkdir(parents=True, exist_ok=True)
    smoke_path = series_dir / "smoke.json"
    token = uuid.uuid4().hex
    benchmark_run_id = uuid.uuid4().hex
    child_env = os.environ.copy()
    # The warm contract is explicitly same-process without idle prewarm. Do
    # not inherit a developer machine's Always-On setting into the child.
    child_env["SCRIBER_MIC_ALWAYS_ON"] = "0"
    child_env["SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID"] = benchmark_run_id
    smoke_result = run_capture(
        smoke_args(
            repo_root,
            install_root,
            series_dir / "data",
            smoke_path,
            extra=[
                "-KeepAppOpen",
                "-EnableHotkeys",
                "-VerifyGlobalHotkeyRegistration",
                "-GlobalHotkeySmokeDefaultStt",
                default_stt,
                "-DisableLiveTextInjection",
                "-LiveRecordingAudioEngine",
                "rust-wasapi",
                "-LiveRecordingRustAudioCaptureMode",
                "synthetic",
                "-SessionToken",
                token,
            ],
            timeout_sec=max(45, timeout_sec),
        ),
        cwd=repo_root,
        timeout=max(90, timeout_sec + 60),
        env=child_env,
    )
    smoke = load_json(smoke_path)
    app_pid = int(smoke.get("appPid") or 0)
    backend_pid = int(smoke.get("backendPid") or 0)
    port = int(smoke.get("backendPort") or 0)
    samples: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": False,
        "provider": provider,
        "defaultStt": default_stt,
        "seriesIndex": series_index,
        "sameProcessWarmContract": True,
        "micAlwaysOn": False,
        "micAlwaysOnChildEnv": child_env.get("SCRIBER_MIC_ALWAYS_ON"),
        "benchmarkRunId": benchmark_run_id,
        "requiredEnv": required_env_status(list(provider_config.get("requiredEnv") or [])),
        "samples": samples,
        "events": events,
        "smokePath": str(smoke_path),
        "smokeExitCode": smoke_result.returncode,
    }
    try:
        if not smoke.get("ok") or not app_pid or not backend_pid or not port:
            result.update(
                {
                    "reason": "runtime_start_failed",
                    "smoke": smoke,
                }
            )
            return result

        try:
            audio_diagnostics = request_runtime_json(
                port,
                token,
                "/api/runtime/audio-diagnostics",
                timeout_sec=5.0,
            )
        except Exception as exc:
            audio_diagnostics = {"error": str(exc)}
        microphone_diagnostics = (
            audio_diagnostics.get("microphone")
            if isinstance(audio_diagnostics.get("microphone"), dict)
            else {}
        )
        mic_always_on_attestation = {
            "ok": microphone_diagnostics.get("micAlwaysOn") is False,
            "childEnv": child_env.get("SCRIBER_MIC_ALWAYS_ON"),
            "runtimeValue": microphone_diagnostics.get("micAlwaysOn", "unknown"),
        }
        result["micAlwaysOnAttestation"] = mic_always_on_attestation
        if not mic_always_on_attestation["ok"]:
            result["reason"] = "mic_always_on_not_false"
            return result

        baseline_generation = process_generation_snapshot(
            app_pid,
            backend_pid,
            port,
            token,
        )
        result["processGenerationBaseline"] = baseline_generation
        if not baseline_generation.get("ok"):
            result["reason"] = "runtime_process_generation_unavailable"
            return result

        initial_overlay = overlay_window_snapshot(app_pid)
        result["overlayTarget"] = public_window_snapshot(initial_overlay)
        if not initial_overlay.get("ok") or initial_overlay.get("visible"):
            result["reason"] = "expected_hidden_overlay_target_unavailable"
            return result
        expected_overlay_hwnd = int(initial_overlay["hwnd"])

        prior_terminal_success = True
        completed_sessions = 0
        seen_tauri_sample_ids: set[str] = set()
        for cycle in range(0, warm_iterations + 1):
            if cycle > 0 and not prior_terminal_success:
                result["reason"] = "prior_session_not_successful"
                break
            scenario = "overlay_cold" if cycle == 0 else "overlay_warm"
            iteration_dir = series_dir / f"cycle-{cycle:02d}-{scenario}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            observer_path = iteration_dir / "overlay-observer.json"
            observer_ready_path = iteration_dir / "overlay-observer-ready.json"
            if not wait_overlay_hidden(5.0):
                result["reason"] = "overlay_not_hidden_before_sample"
                break
            overlay_before = overlay_window_snapshot(app_pid)
            overlay_target_matches = bool(
                overlay_before.get("ok")
                and not overlay_before.get("visible")
                and int(overlay_before.get("hwnd") or 0) == expected_overlay_hwnd
            )
            generation_before = process_generation_snapshot(
                app_pid,
                backend_pid,
                port,
                token,
            )
            generation_before_matches = process_generation_matches(
                baseline_generation,
                generation_before,
            )
            if not overlay_target_matches or not generation_before_matches:
                result["reason"] = (
                    "overlay_target_generation_changed"
                    if not overlay_target_matches
                    else "runtime_process_generation_changed_before_sample"
                )
                break
            observer = run_process(
                [
                    python_exe,
                    str(repo_root / "benchmarks" / "windows" / "overlay_observer.py"),
                    "--timeout-sec",
                    str(max(5, timeout_sec)),
                    "--poll-sec",
                    "0.01",
                    "--expected-pid",
                    str(app_pid),
                    "--expected-hwnd",
                    str(expected_overlay_hwnd),
                    "--ready-output",
                    str(observer_ready_path),
                    "--output",
                    str(observer_path),
                ],
                cwd=repo_root,
                stdout_path=iteration_dir / "observer.stdout.txt",
                stderr_path=iteration_dir / "observer.stderr.txt",
            )
            try:
                observer_ready = wait_for_json_file(observer_ready_path, timeout_sec=5.0)
                focus_before = foreground_window_snapshot()
                if not observer_ready.get("ok") or not focus_before.get("ok"):
                    samples.append(
                        {
                            "scenario": scenario,
                            "seriesIndex": series_index,
                            "cycle": cycle,
                            "provider": provider,
                            "sameProcessAsCold": cycle > 0,
                            "priorSessionSuccessful": prior_terminal_success,
                            "completedSessionsBeforeSample": completed_sessions,
                            "actualWindowsHotkey": False,
                            "micAlwaysOn": False,
                            "durationMs": "unknown",
                            "metricEligible": False,
                            "metricBlockedReason": (
                                "overlay_observer_not_ready"
                                if not observer_ready.get("ok")
                                else "foreground_focus_unavailable_before_sample"
                            ),
                            "observerReady": observer_ready,
                            "focusBefore": public_window_snapshot(focus_before),
                            "processGenerationBefore": generation_before,
                            "processGenerationBeforeMatches": generation_before_matches,
                            "observerPath": str(observer_path),
                            "observerReadyPath": str(observer_ready_path),
                        }
                    )
                    result["reason"] = samples[-1]["metricBlockedReason"]
                    break

                windows_dispatch_ticks = send_global_hotkey_chord()
                state = wait_runtime_state(
                    port,
                    token,
                    lambda value: bool(value.get("listening")) and bool(value.get("sessionId")),
                    timeout_sec,
                )
                observer_exit = wait_process(observer, max(5, timeout_sec + 5))
                observed = load_json(observer_path)
                focus_after = foreground_window_snapshot()
                first_visible = (
                    observed.get("firstVisible")
                    if isinstance(observed.get("firstVisible"), dict)
                    else {}
                )
                visible_ticks = first_visible.get("qpcTicks")
                observer_frequency = int(observed.get("qpcFrequency") or qpc_frequency())
                windows_dispatch_duration = duration_ms(
                    windows_dispatch_ticks,
                    visible_ticks,
                    observer_frequency,
                )
                session_id = str(state.get("sessionId") or "")
                readiness = hot_path_readiness(
                    port,
                    token,
                    session_id,
                    expected_run_id=benchmark_run_id,
                    expected_process_id=app_pid,
                    seen_sample_ids=seen_tauri_sample_ids,
                )
                tauri_marker = (
                    readiness.get("tauriHotkeyReceivedMarker")
                    if isinstance(readiness.get("tauriHotkeyReceivedMarker"), dict)
                    else {}
                )
                tauri_marker_usable = bool(
                    tauri_marker
                    and int(tauri_marker.get("qpcFrequency") or 0) == observer_frequency
                )
                if tauri_marker_usable:
                    seen_tauri_sample_ids.add(str(tauri_marker["sampleId"]))
                tauri_start_ticks = (
                    int(tauri_marker["qpcTicks"])
                    if tauri_marker_usable
                    else None
                )
                measured = duration_ms(
                    tauri_start_ticks,
                    visible_ticks,
                    observer_frequency,
                )
                sample_id = f"overlay-{series_index}-{cycle}"
                events.append(
                    {
                        "session_id": sample_id,
                        "scenario": scenario,
                        "marker": "windows_hotkey_dispatch_started",
                        "qpc_ticks": windows_dispatch_ticks,
                    }
                )
                if tauri_start_ticks is not None:
                    events.append(
                        {
                            "session_id": sample_id,
                            "scenario": scenario,
                            "marker": "hotkey_received",
                            "qpc_ticks": tauri_start_ticks,
                        }
                    )
                if visible_ticks is not None:
                    events.append(
                        {
                            "session_id": sample_id,
                            "scenario": scenario,
                            "marker": "overlay_first_visible_frame",
                            "qpc_ticks": int(visible_ticks),
                        }
                    )
                # The second real hotkey ends the session. A warm measurement is
                # eligible only after this prior session reaches a successful
                # terminal state (idle + Stopped), never FAILED/Error.
                if bool(state.get("listening")):
                    send_global_hotkey_chord()
                terminal = wait_runtime_state(
                    port,
                    token,
                    terminal_state_observed,
                    max(20, timeout_sec),
                )
                overlay_hidden_after = wait_overlay_hidden(5.0)
                terminal_success = successful_terminal_state(terminal)
                overlay_after = overlay_window_snapshot(app_pid)
                overlay_after_matches = bool(
                    overlay_after.get("ok")
                    and int(overlay_after.get("hwnd") or 0) == expected_overlay_hwnd
                )
                generation_after = process_generation_snapshot(
                    app_pid,
                    backend_pid,
                    port,
                    token,
                )
                generation_after_matches = process_generation_matches(
                    baseline_generation,
                    generation_after,
                )
                preserved_focus = focus_preserved(focus_before, focus_after)
                visible_target_matches = bool(
                    observed.get("ok")
                    and first_visible.get("pid") == app_pid
                    and first_visible.get("hwndHash") == hwnd_hash(expected_overlay_hwnd)
                )
                blocked_reasons: list[str] = []
                if measured is None:
                    blocked_reasons.append("tauri_hotkey_received_marker_unavailable")
                if not observed.get("ok") or not visible_target_matches:
                    blocked_reasons.append("expected_overlay_visible_frame_missing")
                if not preserved_focus:
                    blocked_reasons.append("foreground_focus_changed")
                if not terminal_success:
                    blocked_reasons.append("session_terminal_not_successful")
                if not overlay_hidden_after:
                    blocked_reasons.append("overlay_not_hidden_after_session")
                if not overlay_after_matches:
                    blocked_reasons.append("overlay_target_generation_changed")
                if not generation_after_matches:
                    blocked_reasons.append("runtime_process_generation_changed_after_sample")
                sample = {
                    "scenario": scenario,
                    "seriesIndex": series_index,
                    "cycle": cycle,
                    "provider": provider,
                    "sameProcessAsCold": cycle > 0,
                    "priorSessionSuccessful": prior_terminal_success,
                    "completedSessionsBeforeSample": completed_sessions,
                    "actualWindowsHotkey": True,
                    "micAlwaysOn": False,
                    "durationMs": measured if measured is not None else "unknown",
                    "windowsDispatchToVisibleMs": (
                        windows_dispatch_duration
                        if windows_dispatch_duration is not None
                        else "unknown"
                    ),
                    "primaryStartMarker": (
                        "hotkey_received" if tauri_start_ticks is not None else "unknown"
                    ),
                    "tauriMarkerRunIdMatched": bool(
                        tauri_marker
                        and tauri_marker.get("runId") == benchmark_run_id
                    ),
                    "tauriMarkerSampleId": tauri_marker.get("sampleId") or None,
                    "tauriMarkerProcessIdMatched": bool(
                        tauri_marker
                        and int(tauri_marker.get("processId") or 0) == app_pid
                    ),
                    "windowsDispatchDiagnosticOnly": True,
                    "metricEligible": not blocked_reasons,
                    "metricBlockedReason": blocked_reasons[0] if blocked_reasons else None,
                    "metricBlockedReasons": blocked_reasons,
                    "externalVisibleFrameObserved": bool(observed.get("ok")),
                    "expectedOverlayTargetObserved": visible_target_matches,
                    "observerReady": observer_ready,
                    "observerExitCode": observer_exit,
                    "sessionIdObserved": bool(session_id),
                    "sessionCompleted": terminal_state_observed(terminal),
                    "terminalSuccessful": terminal_success,
                    "terminalState": terminal,
                    "readiness": readiness,
                    "focusBefore": public_window_snapshot(focus_before),
                    "focusAfter": public_window_snapshot(focus_after),
                    "focusPreserved": preserved_focus,
                    "overlayBefore": public_window_snapshot(overlay_before),
                    "overlayAfter": public_window_snapshot(overlay_after),
                    "processGenerationBefore": generation_before,
                    "processGenerationBeforeMatches": generation_before_matches,
                    "processGenerationAfter": generation_after,
                    "processGenerationAfterMatches": generation_after_matches,
                    "observerPath": str(observer_path),
                    "observerReadyPath": str(observer_ready_path),
                }
                samples.append(sample)
                prior_terminal_success = terminal_success
                if terminal_success:
                    completed_sessions += 1
                if not terminal_success:
                    result["reason"] = "session_terminal_not_successful"
                    break
            finally:
                terminate_child(observer)
        expected_count = warm_iterations + 1
        all_samples_eligible = bool(len(samples) == expected_count) and all(
            item.get("metricEligible") for item in samples
        )
        result.update(
            {
                "ok": all_samples_eligible,
                "reason": (
                    None
                    if all_samples_eligible
                    else result.get("reason")
                    or next(
                        (
                            str(item.get("metricBlockedReason"))
                            for item in samples
                            if item.get("metricBlockedReason")
                        ),
                        "insufficient_eligible_overlay_samples",
                    )
                ),
                "requestedSamples": expected_count,
                "completedSessions": completed_sessions,
            }
        )
        return result
    finally:
        cleanup = terminate_runtime(app_pid, backend_pid, port, token)
        result["cleanup"] = cleanup
        if not cleanup.get("ok"):
            result["ok"] = False
            result["reason"] = "runtime_cleanup_failed"


def run_overlay_hotkey_probes(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    python_exe: str,
    *,
    cold_samples: int,
    warm_samples: int,
) -> dict[str, Any]:
    series_results: list[dict[str, Any]] = []
    remaining_warm = max(0, warm_samples)
    for index in range(max(1, cold_samples)):
        remaining_series = max(1, cold_samples - index)
        warm_for_series = min(
            remaining_warm,
            math.ceil(remaining_warm / remaining_series) if remaining_warm else 0,
        )
        config = OVERLAY_PROVIDER_CONFIGS[index % len(OVERLAY_PROVIDER_CONFIGS)]
        try:
            result = run_overlay_process_series(
                repo_root,
                install_root,
                output_dir,
                timeout_sec,
                python_exe,
                config,
                series_index=index + 1,
                warm_iterations=warm_for_series,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "seriesIndex": index + 1,
                "provider": str(config["provider"]),
                "error": str(exc),
                "samples": [],
                "events": [],
            }
        series_results.append(result)
        remaining_warm -= sum(
            1 for item in result.get("samples", []) if item.get("scenario") == "overlay_warm"
        )

    samples = [
        item
        for result in series_results
        for item in result.get("samples", [])
        if isinstance(item, dict)
    ]
    events = [
        event
        for result in series_results
        for event in result.get("events", [])
        if isinstance(event, dict)
    ]
    cold_values = [
        item.get("durationMs") for item in samples if item.get("scenario") == "overlay_cold"
    ]
    warm_values = [
        item.get("durationMs") for item in samples if item.get("scenario") == "overlay_warm"
    ]
    readiness_samples: dict[str, list[Any]] = {metric: [] for metric in USER_READY_METRICS}
    for item in samples:
        readiness = item.get("readiness") if isinstance(item.get("readiness"), dict) else {}
        for metric, source_key in USER_READY_METRICS.items():
            readiness_samples[metric].append(readiness.get(source_key))
    metrics: dict[str, Any] = {
        "overlay_cold_p50_ms": percentile_ms(cold_values, 50.0),
        "overlay_cold_p95_ms": percentile_ms(cold_values, 95.0),
        "overlay_cold_sample_count": sum(finite_number(value) for value in cold_values),
        "overlay_warm_p50_ms": percentile_ms(warm_values, 50.0),
        "overlay_warm_p95_ms": percentile_ms(warm_values, 95.0),
        "overlay_warm_sample_count": sum(finite_number(value) for value in warm_values),
    }
    for metric, values in readiness_samples.items():
        metrics[metric] = percentile_ms(values, 95.0)
    counts_ok = (
        metrics["overlay_cold_sample_count"] >= cold_samples
        and metrics["overlay_warm_sample_count"] >= warm_samples
    )
    sample_blocked_reasons = [
        str(reason)
        for item in samples
        for reason in (item.get("metricBlockedReasons") or [])
        if reason
    ]
    if "tauri_hotkey_received_marker_unavailable" in sample_blocked_reasons:
        metric_blocked_reason = "tauri_hotkey_received_marker_unavailable"
    elif not counts_ok:
        metric_blocked_reason = "insufficient_overlay_samples"
    elif not all(result.get("ok") for result in series_results):
        metric_blocked_reason = "overlay_series_failed_closed"
    else:
        metric_blocked_reason = None
    return {
        "attempted": True,
        "metricEligible": counts_ok and all(result.get("ok") for result in series_results),
        "metricBlockedReason": metric_blocked_reason,
        "sameProcessWarmContract": True,
        "requestedSamples": {"cold": cold_samples, "warm": warm_samples},
        "series": series_results,
        "samples": samples,
        "externalVisibleFrameObserved": bool(samples)
        and all(item.get("externalVisibleFrameObserved") for item in samples),
        "qpcFrequency": qpc_frequency(),
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


def active_window_title() -> str:
    if os.name != "nt":
        return ""
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return ""
    length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
    buffer = ctypes.create_unicode_buffer(max(2, length + 1))
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, len(buffer))
    return buffer.value


def focus_receiver_window(title: str, timeout_sec: float = 2.0) -> dict[str, Any]:
    """Focus the real editable receiver through the normal Windows input path."""

    if os.name != "nt":
        return {"ok": False, "reason": "not_windows"}
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    deadline = time.monotonic() + max(0.1, timeout_sec)
    while time.monotonic() < deadline:
        hwnd = find_window(title)
        target = find_text_receiver_target(title, timeout_sec=0.1)
        if hwnd and target.get("ok"):
            current_thread = int(kernel32.GetCurrentThreadId())
            target_thread = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None))
            foreground = int(user32.GetForegroundWindow() or 0)
            foreground_thread = (
                int(user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None))
                if foreground
                else 0
            )
            attached: list[int] = []
            for thread_id in {target_thread, foreground_thread}:
                if thread_id and thread_id != current_thread:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached.append(thread_id)
            try:
                user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
                user32.BringWindowToTop(ctypes.c_void_p(hwnd))
                user32.SetActiveWindow(ctypes.c_void_p(hwnd))
                user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                for candidate in child_windows(hwnd):
                    if hwnd_hash(candidate) == target.get("targetHwndHash"):
                        user32.SetFocus(ctypes.c_void_p(candidate))
                        break
            finally:
                for thread_id in attached:
                    user32.AttachThreadInput(current_thread, thread_id, False)
            if active_window_title() == title:
                return {
                    "ok": True,
                    "method": "foreground_window_and_edit_focus",
                    "windowHwndHash": hwnd_hash(hwnd),
                    "targetHwndHash": target.get("targetHwndHash", ""),
                }
        time.sleep(0.05)
    return {
        "ok": False,
        "method": "foreground_window_and_edit_focus",
        "reason": "foreground_focus_not_observed",
        "activeTitleSha256": sha256_text(active_window_title()),
    }


def _canonical_non_nil_uuid(value: Any) -> str:
    try:
        parsed = uuid.UUID(str(value or ""))
    except (AttributeError, TypeError, ValueError):
        return ""
    return parsed.hex if parsed.int else ""


def provider_replay_target_generation_sha256(
    process_id: int,
    creation_time_100ns: int,
) -> str:
    if process_id <= 0 or creation_time_100ns <= 0:
        return ""
    material = (
        f"provider-replay-target-v1\0{process_id}\0{creation_time_100ns}"
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def provider_replay_process_generation_sha256(
    process_snapshot: dict[str, Any],
) -> str:
    app = process_snapshot.get("app") if isinstance(process_snapshot.get("app"), dict) else {}
    backend = (
        process_snapshot.get("backend")
        if isinstance(process_snapshot.get("backend"), dict)
        else {}
    )
    try:
        app_pid = int(app.get("pid") or 0)
        app_creation = int(app.get("creationTime100ns") or 0)
        backend_pid = int(backend.get("pid") or 0)
        backend_creation = int(backend.get("creationTime100ns") or 0)
        backend_parent = int(backend.get("parentPid") or 0)
    except (TypeError, ValueError, OverflowError):
        return ""
    if (
        app_pid <= 0
        or app_creation <= 0
        or backend_pid <= 0
        or backend_creation <= 0
        or backend_parent != app_pid
    ):
        return ""
    material = (
        "scriber-provider-replay-process-v1\0"
        f"{backend_pid}\0{backend_creation}\0{app_pid}\0{app_creation}"
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _provider_replay_target_attestation(
    *,
    title: str,
    receiver_pid: int,
) -> dict[str, Any]:
    window_hwnd = find_window(title)
    window_pid = window_process_id(window_hwnd)
    receiver_creation = _process_creation_time_100ns(receiver_pid)
    foreground = foreground_window_snapshot()
    ok = bool(
        window_hwnd
        and window_pid == receiver_pid
        and receiver_creation is not None
        and foreground.get("ok")
        and int(foreground.get("pid") or 0) == receiver_pid
        and int(foreground.get("processCreationTime100ns") or 0)
        == receiver_creation
    )
    return {
        "ok": ok,
        "processId": receiver_pid,
        "creationTime100ns": receiver_creation,
        "windowPidMatches": window_pid == receiver_pid,
        "foregroundGenerationMatches": bool(
            foreground.get("ok")
            and int(foreground.get("pid") or 0) == receiver_pid
            and int(foreground.get("processCreationTime100ns") or 0)
            == int(receiver_creation or 0)
        ),
        "windowHwndHash": hwnd_hash(window_hwnd),
        "targetGenerationSha256": provider_replay_target_generation_sha256(
            receiver_pid,
            int(receiver_creation or 0),
        ),
    }


def wait_provider_replay_status(
    port: int,
    token: str,
    run_id: str,
    sample_id: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = request_runtime_json(
                port,
                token,
                f"{PROVIDER_REPLAY_ROUTE}/{sample_id}?runId={run_id}",
                timeout_sec=2.0,
            )
        except Exception as exc:
            last = {"requestError": type(exc).__name__}
        if str(last.get("state") or "") in {
            "completed",
            "failed",
            "unsupported",
        }:
            return last
        time.sleep(0.02)
    return last


def validate_provider_replay_sample(
    *,
    provider: str,
    run_id: str,
    start_marker: str,
    prepared: dict[str, Any],
    armed: dict[str, Any],
    completed: dict[str, Any],
    observed: dict[str, Any],
    observer_ready: dict[str, Any],
    observer_exit_code: int | None,
    expected_process_generation_sha256: str,
    expected_target_generation_sha256: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    expected_run = _canonical_non_nil_uuid(run_id)
    sample_id = _canonical_non_nil_uuid(prepared.get("sampleId"))
    session_id = _canonical_non_nil_uuid(armed.get("sessionId"))
    fixture_text = prepared.get("fixtureText")
    fixture_sha256 = str(prepared.get("fixtureTextSha256") or "")
    fixture_length = prepared.get("fixtureTextLength")

    if not expected_run:
        reasons.append("run_id_invalid")
    if not sample_id:
        reasons.append("sample_id_invalid")
    if not session_id:
        reasons.append("session_id_invalid")
    if not isinstance(fixture_text, str) or not fixture_text or len(fixture_text) > 1024:
        reasons.append("fixture_text_invalid")
        fixture_text = ""
    if not _is_sha256(fixture_sha256) or fixture_sha256 != sha256_text(fixture_text):
        reasons.append("fixture_hash_invalid")
    if (
        not isinstance(fixture_length, int)
        or isinstance(fixture_length, bool)
        or fixture_length != len(fixture_text)
    ):
        reasons.append("fixture_length_invalid")

    phase_contracts = (
        ("prepared", prepared, "prepared"),
        ("armed", armed, "armed"),
        ("completed", completed, "completed"),
    )
    for phase, payload, state in phase_contracts:
        if payload.get("contractVersion") != PROVIDER_REPLAY_CONTRACT_VERSION:
            reasons.append(f"{phase}_contract_version_invalid")
        if _canonical_non_nil_uuid(payload.get("runId")) != expected_run:
            reasons.append(f"{phase}_run_id_mismatch")
        if _canonical_non_nil_uuid(payload.get("sampleId")) != sample_id:
            reasons.append(f"{phase}_sample_id_mismatch")
        if payload.get("provider") != provider:
            reasons.append(f"{phase}_provider_mismatch")
        if payload.get("state") != state:
            reasons.append(f"{phase}_state_invalid")
        if payload.get("fixtureText") != fixture_text:
            reasons.append(f"{phase}_fixture_text_mismatch")
        if payload.get("fixtureTextSha256") != fixture_sha256:
            reasons.append(f"{phase}_fixture_hash_mismatch")
        if payload.get("fixtureTextLength") != fixture_length:
            reasons.append(f"{phase}_fixture_length_mismatch")
        if payload.get("processGenerationFingerprint") != expected_process_generation_sha256:
            reasons.append(f"{phase}_process_generation_mismatch")

    if prepared.get("sessionId") is not None:
        reasons.append("prepared_session_must_be_null")
    if prepared.get("targetGenerationSha256") is not None:
        reasons.append("prepared_target_generation_must_be_null")
    if _canonical_non_nil_uuid(completed.get("sessionId")) != session_id:
        reasons.append("completed_session_id_mismatch")
    for phase, payload in (("armed", armed), ("completed", completed)):
        if payload.get("targetGenerationSha256") != expected_target_generation_sha256:
            reasons.append(f"{phase}_target_generation_mismatch")
    if completed.get("errorCode") is not None:
        reasons.append("completed_error_code_present")

    raw_markers = completed.get("markers")
    markers: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_markers, list):
        reasons.append("markers_not_list")
        raw_markers = []
    for item in raw_markers:
        if not isinstance(item, dict):
            reasons.append("marker_not_object")
            continue
        marker_name = str(item.get("marker") or "")
        if marker_name in markers:
            reasons.append(f"marker_duplicate:{marker_name}")
            continue
        markers[marker_name] = item
        expected_source = PROVIDER_REPLAY_MARKER_SOURCES.get(marker_name)
        if expected_source is None:
            reasons.append(f"marker_unknown:{marker_name}")
        elif item.get("source") != expected_source:
            reasons.append(f"marker_source_invalid:{marker_name}")
        if item.get("ok") is not True or item.get("apiVersion") != 1:
            reasons.append(f"marker_contract_invalid:{marker_name}")
        if _canonical_non_nil_uuid(item.get("runId")) != expected_run:
            reasons.append(f"marker_run_mismatch:{marker_name}")
        if _canonical_non_nil_uuid(item.get("sampleId")) != sample_id:
            reasons.append(f"marker_sample_mismatch:{marker_name}")
        if _canonical_non_nil_uuid(item.get("sessionId")) != session_id:
            reasons.append(f"marker_session_mismatch:{marker_name}")
        if item.get("processGenerationFingerprint") != expected_process_generation_sha256:
            reasons.append(f"marker_process_generation_mismatch:{marker_name}")
        for field in ("qpcTicks", "qpcFrequency"):
            value = item.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                reasons.append(f"marker_{field}_invalid:{marker_name}")

    required_markers = {
        start_marker,
        "clipboard_set",
        "paste",
        "injection_callback_completed",
        "session_finished_emitted",
    }
    if provider == "microsoft":
        required_markers.add("recording_state_transcribing_emitted")
    for marker_name in sorted(required_markers):
        if marker_name not in markers:
            reasons.append(f"marker_missing:{marker_name}")
    for marker_name in sorted(set(markers) - required_markers):
        reasons.append(f"marker_unexpected:{marker_name}")

    if prepared.get("markers") != []:
        reasons.append("prepared_markers_not_empty")
    if armed.get("markers") != []:
        reasons.append("armed_markers_not_empty")

    ordered_markers = (
        start_marker,
        "clipboard_set",
        "paste",
        "injection_callback_completed",
        "session_finished_emitted",
    )
    ordered_ticks = [
        int(markers.get(name, {}).get("qpcTicks") or 0)
        for name in ordered_markers
    ]
    if all(ordered_ticks) and ordered_ticks != sorted(ordered_ticks):
        reasons.append("marker_order_invalid")

    start = markers.get(start_marker, {})
    start_ticks = start.get("qpcTicks")
    start_frequency = start.get("qpcFrequency")
    if isinstance(start_frequency, int) and not isinstance(start_frequency, bool):
        for marker_name, marker in markers.items():
            if marker.get("qpcFrequency") != start_frequency:
                reasons.append(f"marker_qpc_frequency_mismatch:{marker_name}")
    observed_ticks = observed.get("qpcTicks")
    observed_frequency = observed.get("qpcFrequency")
    if observer_exit_code != 0:
        reasons.append("observer_exit_invalid")
    if observer_ready.get("ok") is not True or observer_ready.get("endpoint") != "target_text_observer_ready":
        reasons.append("observer_ready_invalid")
    if observed.get("ok") is not True or observed.get("endpoint") != "target_text_observed":
        reasons.append("target_text_not_observed")
    if observed.get("expectedSha256") != fixture_sha256:
        reasons.append("observer_expected_hash_mismatch")
    if observed.get("observedSha256") != fixture_sha256:
        reasons.append("observer_hash_mismatch")
    expected_utf16_length = len(fixture_text.encode("utf-16-le")) // 2
    if observed.get("observedChars") != expected_utf16_length:
        reasons.append("observer_length_mismatch")
    if (
        not isinstance(observed_ticks, int)
        or isinstance(observed_ticks, bool)
        or observed_ticks <= 0
        or not isinstance(observed_frequency, int)
        or isinstance(observed_frequency, bool)
        or observed_frequency <= 0
        or observed_frequency != start_frequency
    ):
        reasons.append("observer_qpc_invalid")

    measured = duration_ms(start_ticks, observed_ticks, start_frequency)
    if measured is None:
        reasons.append("provider_to_visible_duration_invalid")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "runId": expected_run,
        "sampleId": sample_id,
        "sessionId": session_id,
        "fixtureTextSha256": fixture_sha256,
        "fixtureTextLength": fixture_length,
        "markerNames": list(markers),
        "startMarker": start_marker,
        "startQpcTicks": start_ticks,
        "endQpcTicks": observed_ticks,
        "qpcFrequency": start_frequency,
        "durationMs": measured if measured is not None else "unknown",
    }


def run_provider_text_replay(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    iterations: int,
) -> dict[str, Any]:
    probe_dir = output_dir / "provider-replay"
    probe_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    series_results: list[dict[str, Any]] = []
    metrics: dict[str, float | str] = {}
    text_errors = 0
    focus_errors = 0
    clipboard_errors = 0
    events: list[dict[str, Any]] = []
    requested_iterations = max(1, iterations)

    for scenario in PROVIDER_REPLAY_SCENARIOS:
        provider = str(scenario["provider"])
        scenario_name = str(scenario["scenario"])
        start_marker = str(scenario["startMarker"])
        scenario_dir = probe_dir / provider
        scenario_dir.mkdir(parents=True, exist_ok=True)
        smoke_path = scenario_dir / "smoke.json"
        token = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        child_env = os.environ.copy()
        child_env.update(
            {
                "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID": run_id,
                "SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL": "1",
                "SCRIBER_MIC_ALWAYS_ON": "0",
                "SCRIBER_MIC_DEVICE": "default",
                "SCRIBER_FAVORITE_MIC": "",
                "SCRIBER_DISABLE_TEXT_INJECTION": "0",
                "SCRIBER_AUTO_SUMMARIZE": "0",
            }
        )
        smoke = {}
        smoke_exit_code: int | None = None
        app_pid = 0
        backend_pid = 0
        port = 0
        durations: list[float] = []
        series_reason = ""
        process_baseline: dict[str, Any] = {}
        expected_process_fingerprint = ""
        cleanup: dict[str, Any] = {}

        try:
            smoke_result = run_capture(
                smoke_args(
                    repo_root,
                    install_root,
                    scenario_dir / "data",
                    smoke_path,
                    extra=[
                        "-KeepAppOpen",
                        "-OccupyDefaultPort",
                        "-LiveRecordingAudioEngine",
                        "rust-wasapi",
                        "-LiveRecordingRustAudioCaptureMode",
                        "synthetic",
                        "-SessionToken",
                        token,
                    ],
                    timeout_sec=max(45, timeout_sec),
                ),
                cwd=repo_root,
                timeout=max(90, timeout_sec + 60),
                env=child_env,
            )
            smoke_exit_code = smoke_result.returncode
            smoke = load_json(smoke_path)
            app_pid = int(smoke.get("appPid") or 0)
            backend_pid = int(smoke.get("backendPid") or 0)
            port = int(smoke.get("backendPort") or 0)
            if (
                smoke_result.returncode != 0
                or not smoke.get("ok")
                or app_pid <= 0
                or backend_pid <= 0
                or port <= 0
            ):
                series_reason = "runtime_start_failed"
            else:
                process_baseline = process_generation_snapshot(
                    app_pid,
                    backend_pid,
                    port,
                    token,
                )
                expected_process_fingerprint = (
                    provider_replay_process_generation_sha256(process_baseline)
                )
                if not process_baseline.get("ok") or not expected_process_fingerprint:
                    series_reason = "runtime_process_generation_unavailable"

            for iteration in range(1, requested_iterations + 1):
                if series_reason:
                    break
                title = f"Scriber B7 TextReceiver {provider} {iteration}"
                iteration_dir = scenario_dir / f"iteration-{iteration:02d}"
                iteration_dir.mkdir(parents=True, exist_ok=True)
                receiver_stdout = iteration_dir / "receiver.stdout.txt"
                receiver_stderr = iteration_dir / "receiver.stderr.txt"
                observer_stdout = iteration_dir / "observer.stdout.txt"
                observer_stderr = iteration_dir / "observer.stderr.txt"
                observer_path = iteration_dir / "text-observer.json"
                observer_ready_path = iteration_dir / "text-observer-ready.json"
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
                prepared: dict[str, Any] = {}
                armed: dict[str, Any] = {}
                completed: dict[str, Any] = {}
                observer_ready: dict[str, Any] = {}
                observed: dict[str, Any] = {}
                focus: dict[str, Any] = {}
                target_attestation: dict[str, Any] = {}
                target_after: dict[str, Any] = {}
                terminal: dict[str, Any] = {}
                generation_after: dict[str, Any] = {}
                validation: dict[str, Any] = {
                    "ok": False,
                    "reasons": ["sample_not_started"],
                    "durationMs": "unknown",
                }
                observer_exit: int | None = None
                sample_error = ""
                try:
                    prepared = request_runtime_json(
                        port,
                        token,
                        f"{PROVIDER_REPLAY_ROUTE}/prepare",
                        method="POST",
                        payload={
                            "schemaVersion": PROVIDER_REPLAY_CONTRACT_VERSION,
                            "runId": run_id,
                            "provider": provider,
                        },
                        timeout_sec=5.0,
                    )
                    fixture_text = prepared.get("fixtureText")
                    fixture_hash = str(prepared.get("fixtureTextSha256") or "")
                    if (
                        prepared.get("state") != "prepared"
                        or prepared.get("provider") != provider
                        or _canonical_non_nil_uuid(prepared.get("runId")) != run_id
                        or not _canonical_non_nil_uuid(prepared.get("sampleId"))
                        or not isinstance(fixture_text, str)
                        or not fixture_text
                        or fixture_hash != sha256_text(fixture_text)
                        or prepared.get("fixtureTextLength") != len(fixture_text)
                        or prepared.get("processGenerationFingerprint")
                        != expected_process_fingerprint
                    ):
                        raise RuntimeError("prepare_contract_invalid")
                    sample_id = _canonical_non_nil_uuid(prepared.get("sampleId"))

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
                            fixture_hash,
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
                    observer_ready = wait_for_json_file(observer_ready_path, timeout_sec=10)
                    focus = focus_receiver_window(title)
                    target_attestation = _provider_replay_target_attestation(
                        title=title,
                        receiver_pid=receiver.pid,
                    )
                    if not observer_ready.get("ok") or not focus.get("ok") or not target_attestation.get("ok"):
                        raise RuntimeError("receiver_focus_or_generation_unavailable")
                    if hwnd_hash(int(observer_ready.get("targetHwnd") or 0)) != focus.get("targetHwndHash"):
                        raise RuntimeError("observer_target_mismatch")

                    armed = request_runtime_json(
                        port,
                        token,
                        f"{PROVIDER_REPLAY_ROUTE}/{sample_id}/arm",
                        method="POST",
                        payload={
                            "schemaVersion": PROVIDER_REPLAY_CONTRACT_VERSION,
                            "runId": run_id,
                            "targetProcessId": receiver.pid,
                            "targetCreationTime100ns": int(
                                target_attestation["creationTime100ns"]
                            ),
                        },
                        timeout_sec=10.0,
                    )
                    completed = wait_provider_replay_status(
                        port,
                        token,
                        run_id,
                        sample_id,
                        max(20, timeout_sec),
                    )
                    observer_exit = wait_process(observer, max(10, timeout_sec + 10))
                    observed = load_json(observer_path)
                    terminal = wait_runtime_state(
                        port,
                        token,
                        successful_terminal_state,
                        max(20, timeout_sec),
                    )
                    generation_after = process_generation_snapshot(
                        app_pid,
                        backend_pid,
                        port,
                        token,
                    )
                    target_after = _provider_replay_target_attestation(
                        title=title,
                        receiver_pid=receiver.pid,
                    )
                    validation = validate_provider_replay_sample(
                        provider=provider,
                        run_id=run_id,
                        start_marker=start_marker,
                        prepared=prepared,
                        armed=armed,
                        completed=completed,
                        observed=observed,
                        observer_ready=observer_ready,
                        observer_exit_code=observer_exit,
                        expected_process_generation_sha256=(
                            expected_process_fingerprint
                        ),
                        expected_target_generation_sha256=str(
                            target_attestation.get("targetGenerationSha256") or ""
                        ),
                    )
                    if not successful_terminal_state(terminal):
                        validation["reasons"].append("runtime_terminal_state_invalid")
                    if not process_generation_matches(process_baseline, generation_after):
                        validation["reasons"].append("runtime_generation_changed")
                    if (
                        not target_after.get("ok")
                        or target_after.get("targetGenerationSha256")
                        != target_attestation.get("targetGenerationSha256")
                    ):
                        validation["reasons"].append("receiver_generation_changed")
                    validation["ok"] = not validation["reasons"]
                except Exception as exc:
                    sample_error = str(exc)[:240]
                    validation = {
                        "ok": False,
                        "reasons": [sample_error or type(exc).__name__],
                        "durationMs": "unknown",
                    }
                finally:
                    terminate_child(observer)
                    terminate_child(receiver)

                marker_names = set(validation.get("markerNames") or [])
                clipboard_ok = "clipboard_set" in marker_names
                paste_ok = bool(
                    {"paste", "injection_callback_completed"}.issubset(marker_names)
                )
                target_text_observed = bool(
                    observed.get("ok")
                    and observed.get("observedSha256")
                    == prepared.get("fixtureTextSha256")
                )
                if not target_text_observed:
                    text_errors += 1
                if not focus.get("ok") or not target_after.get("ok"):
                    focus_errors += 1
                if not clipboard_ok:
                    clipboard_errors += 1
                measured = validation.get("durationMs", "unknown")
                if finite_number(measured) and validation.get("ok"):
                    durations.append(float(measured))
                    events.extend(
                        [
                            {
                                "session_id": validation.get("sessionId"),
                                "scenario": scenario_name,
                                "marker": start_marker,
                                "qpc_ticks": int(validation["startQpcTicks"]),
                            },
                            {
                                "session_id": validation.get("sessionId"),
                                "scenario": scenario_name,
                                "marker": "target_text_observed",
                                "qpc_ticks": int(validation["endQpcTicks"]),
                            },
                        ]
                    )
                sample_result = {
                    "provider": provider,
                    "iteration": iteration,
                    "scenario": scenario_name,
                    "runId": validation.get("runId", run_id),
                    "sampleId": validation.get("sampleId"),
                    "sessionId": validation.get("sessionId"),
                    "startMarker": start_marker,
                    "targetMarker": "target_text_observed",
                    "expectedSha256": prepared.get("fixtureTextSha256"),
                    "fixtureTextLength": prepared.get("fixtureTextLength"),
                    "inputMethod": "installed_backend_provider_replay",
                    "processGenerationFingerprint": expected_process_fingerprint,
                    "targetGenerationSha256": target_attestation.get(
                        "targetGenerationSha256"
                    ),
                    "markerNames": validation.get("markerNames", []),
                    "observerReady": observer_ready,
                    "focus": focus,
                    "targetAttestation": target_attestation,
                    "targetAfter": target_after,
                    "terminalState": terminal,
                    "generationAfterMatches": process_generation_matches(
                        process_baseline,
                        generation_after,
                    ),
                    "clipboardOk": clipboard_ok,
                    "pasteOk": paste_ok,
                    "observerExitCode": observer_exit,
                    "targetTextObserved": target_text_observed,
                    "durationMs": measured,
                    "metricEligible": bool(validation.get("ok")),
                    "reasons": list(validation.get("reasons") or []),
                    "error": sample_error,
                    "receiverPid": receiver.pid,
                    "observerPath": str(observer_path),
                    "observerReadyPath": str(observer_ready_path),
                }
                results.append(sample_result)
                if not sample_result["metricEligible"]:
                    series_reason = "provider_replay_sample_failed_closed"
                    break
        except Exception as exc:
            series_reason = series_reason or f"provider_series_exception:{type(exc).__name__}"
        finally:
            cleanup = terminate_runtime(app_pid, backend_pid, port, token)

        metric_prefix = str(scenario["metricPrefix"])
        metrics[f"{metric_prefix}_p50_ms"] = percentile_ms(durations, 50.0)
        metrics[f"{metric_prefix}_p95_ms"] = percentile_ms(durations, 95.0)
        metrics[f"{metric_prefix}_sample_count"] = len(durations)
        provider_samples = [item for item in results if item.get("provider") == provider]
        series_ok = bool(
            not series_reason
            and len(provider_samples) == requested_iterations
            and len(durations) == requested_iterations
            and all(item.get("metricEligible") for item in provider_samples)
            and cleanup.get("ok")
        )
        series_results.append(
            {
                "provider": provider,
                "runId": run_id,
                "smokePath": str(smoke_path),
                "smokeExitCode": smoke_exit_code,
                "appPid": app_pid,
                "backendPid": backend_pid,
                "backendPort": port,
                "processGenerationFingerprint": expected_process_fingerprint,
                "requestedSamples": requested_iterations,
                "measuredSamples": len(durations),
                "runtimeStarted": bool(smoke.get("ok")),
                "cleanup": cleanup,
                "ok": series_ok,
                "reason": "measured" if series_ok else (series_reason or "cleanup_failed"),
            }
        )

    expected_sample_count = requested_iterations * len(PROVIDER_REPLAY_SCENARIOS)
    ok = bool(
        len(results) == expected_sample_count
        and all(item.get("metricEligible") for item in results)
        and all(item.get("ok") for item in series_results)
    )
    return {
        "attempted": True,
        "metricEligible": ok,
        "ok": ok,
        "reason": "measured" if ok else "installed_provider_replay_failed_closed",
        "installedRuntimeOnly": True,
        "requestedSamplesPerProvider": requested_iterations,
        "series": series_results,
        "results": results,
        "metrics": metrics,
        "events": events,
        "textErrors": text_errors,
        "focusErrors": focus_errors,
        "clipboardErrors": clipboard_errors,
    }


def _validate_bound_artifact(
    artifact: Any,
    artifact_root: Path,
    hash_cache: dict[Path, str],
) -> tuple[bool, str | None]:
    if not isinstance(artifact, dict):
        return False, "artifact_binding_missing"
    expected_sha256 = artifact.get("sha256")
    if not _is_sha256(expected_sha256):
        return False, "artifact_sha256_invalid"
    path = _safe_artifact_path(artifact_root, artifact.get("path"))
    if path is None:
        return False, "artifact_path_outside_evidence_root"
    if not path.is_file():
        return False, "artifact_file_missing"
    actual_sha256 = hash_cache.get(path)
    if actual_sha256 is None:
        actual_sha256 = sha256_file(path)
        hash_cache[path] = actual_sha256
    if actual_sha256 != expected_sha256:
        return False, "artifact_sha256_mismatch"
    return True, None


def _load_bound_json_artifact(
    artifact: Any,
    artifact_root: Path,
    hash_cache: dict[Path, str],
) -> tuple[dict[str, Any], str | None]:
    valid, reason = _validate_bound_artifact(artifact, artifact_root, hash_cache)
    if not valid:
        return {}, reason
    path = _safe_artifact_path(artifact_root, artifact.get("path"))
    if path is None:
        return {}, "artifact_path_outside_evidence_root"
    payload = load_json(path)
    if not payload or "parseError" in payload:
        return {}, "artifact_json_invalid"
    return payload, None


def _validate_app_ux_sample(
    sample: Any,
    *,
    artifact_root: Path,
    run_id: str,
    installed_exe_sha256: str,
    harness_manifest_sha256: str,
    hash_cache: dict[Path, str],
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(sample, dict):
        return None, ["sample_not_object"]
    scenario = sample.get("scenario")
    if scenario not in APP_UX_SCENARIOS:
        reasons.append("scenario_unknown")
    iteration = sample.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        reasons.append("iteration_invalid")
    if sample.get("runId") != run_id:
        reasons.append("sample_run_id_mismatch")
    sample_id = str(sample.get("sampleId") or "")
    try:
        sample_uuid = uuid.UUID(sample_id)
        if sample_uuid.int == 0:
            raise ValueError("nil UUID")
        sample_id = sample_uuid.hex
    except (ValueError, AttributeError):
        reasons.append("sample_id_invalid")
        sample_id = ""
    if sample.get("installedExeSha256") != installed_exe_sha256:
        reasons.append("sample_installed_exe_mismatch")
    if sample.get("harnessManifestSha256") != harness_manifest_sha256:
        reasons.append("sample_harness_manifest_mismatch")
    generation = sample.get("processGenerationFingerprint")
    if not _is_sha256(generation):
        reasons.append("process_generation_fingerprint_invalid")

    start = sample.get("start") if isinstance(sample.get("start"), dict) else {}
    stable = (
        sample.get("stableFrame")
        if isinstance(sample.get("stableFrame"), dict)
        else {}
    )
    expected_start_markers = {
        "cold_app_launch": "user_input_received",
        "warm_app_activation": "user_input_received",
        "open_transcript_detail": "user_input_received",
        "open_settings": "user_input_received",
        "stop_to_transcribing_visible": "user_input_received",
        "provider_result_to_completed_visible": "provider_response_complete",
        "session_finished_to_history_visible": "session_finished_emitted",
        "switch_between_transcripts": "user_input_received",
        "return_to_dashboard": "user_input_received",
    }
    if scenario in expected_start_markers and start.get("marker") != expected_start_markers[scenario]:
        reasons.append("start_marker_invalid")
    if scenario in APP_UX_ALLOWED_START_SOURCES and start.get("source") not in APP_UX_ALLOWED_START_SOURCES[scenario]:
        reasons.append("start_source_invalid")
    start_ticks = start.get("qpcTicks")
    start_frequency = start.get("qpcFrequency")
    if not isinstance(start_ticks, int) or isinstance(start_ticks, bool) or start_ticks <= 0:
        reasons.append("start_qpc_invalid")
    if (
        not isinstance(start_frequency, int)
        or isinstance(start_frequency, bool)
        or start_frequency <= 0
    ):
        reasons.append("start_qpc_frequency_invalid")
    if start.get("processGenerationFingerprint") != generation:
        reasons.append("start_process_generation_mismatch")
    start_artifact, artifact_reason = _load_bound_json_artifact(
        start.get("artifact"), artifact_root, hash_cache
    )
    if artifact_reason:
        reasons.append(f"start_{artifact_reason}")
    elif not (
        start_artifact.get("ok") is True
        and start_artifact.get("endpoint") == start.get("marker")
        and start_artifact.get("source") == start.get("source")
        and start_artifact.get("qpcTicks") == start_ticks
        and start_artifact.get("qpcFrequency") == start_frequency
    ):
        reasons.append("start_artifact_payload_mismatch")

    if stable.get("marker") != "first_stable_visible_frame":
        reasons.append("stable_frame_marker_invalid")
    if stable.get("source") != "windows_uia":
        reasons.append("stable_frame_source_invalid")
    if stable.get("processGenerationFingerprint") != generation:
        reasons.append("stable_frame_process_generation_mismatch")
    if (
        not isinstance(stable.get("windowProcessId"), int)
        or isinstance(stable.get("windowProcessId"), bool)
        or stable.get("windowProcessId", 0) <= 0
    ):
        reasons.append("stable_frame_window_process_invalid")
    if (
        not isinstance(stable.get("processCreationTime100ns"), int)
        or isinstance(stable.get("processCreationTime100ns"), bool)
        or stable.get("processCreationTime100ns", 0) <= 0
    ):
        reasons.append("stable_frame_process_creation_invalid")
    end_ticks = stable.get("qpcTicks")
    end_frequency = stable.get("qpcFrequency")
    if not isinstance(end_ticks, int) or isinstance(end_ticks, bool) or end_ticks <= 0:
        reasons.append("stable_frame_qpc_invalid")
    if end_frequency != start_frequency:
        reasons.append("qpc_frequency_mismatch")
    if (
        not isinstance(stable.get("stableSampleCount"), int)
        or isinstance(stable.get("stableSampleCount"), bool)
        or stable.get("stableSampleCount", 0) < 2
    ):
        reasons.append("stable_frame_not_confirmed")
    expected_hashes = stable.get("expectedTextSha256")
    if (
        not isinstance(expected_hashes, list)
        or not expected_hashes
        or any(not _is_sha256(value) for value in expected_hashes)
    ):
        reasons.append("stable_frame_expected_text_hashes_invalid")
    stable_artifact, artifact_reason = _load_bound_json_artifact(
        stable.get("artifact"), artifact_root, hash_cache
    )
    if artifact_reason:
        reasons.append(f"stable_frame_{artifact_reason}")
    elif not (
        stable_artifact.get("ok") is True
        and stable_artifact.get("endpoint") == "first_stable_visible_frame"
        and stable_artifact.get("stableQpcTicks") == end_ticks
        and stable_artifact.get("qpcFrequency") == end_frequency
        and stable_artifact.get("stableSampleCount")
        == stable.get("stableSampleCount")
        and stable_artifact.get("processId") == stable.get("windowProcessId")
        and stable_artifact.get("processCreationTime100ns")
        == stable.get("processCreationTime100ns")
        and stable_artifact.get("expectedTextSha256") == expected_hashes
    ):
        reasons.append("stable_frame_artifact_payload_mismatch")

    generation_evidence = (
        sample.get("processGeneration")
        if isinstance(sample.get("processGeneration"), dict)
        else {}
    )
    if generation_evidence.get("fingerprint") != generation:
        reasons.append("process_generation_evidence_fingerprint_mismatch")
    generation_artifact, artifact_reason = _load_bound_json_artifact(
        generation_evidence.get("artifact"), artifact_root, hash_cache
    )
    if artifact_reason:
        reasons.append(f"process_generation_{artifact_reason}")
    else:
        generation_app = (
            generation_artifact.get("app")
            if isinstance(generation_artifact.get("app"), dict)
            else {}
        )
        generation_backend = (
            generation_artifact.get("backend")
            if isinstance(generation_artifact.get("backend"), dict)
            else {}
        )
        generation_webviews = generation_artifact.get("webViewProcesses")
        if generation_artifact.get("ok") is not True:
            reasons.append("process_generation_not_attested")
        if generation_artifact.get("fingerprint") != generation:
            reasons.append("process_generation_artifact_fingerprint_mismatch")
        if process_generation_fingerprint(generation_artifact) != generation:
            reasons.append("process_generation_fingerprint_recompute_mismatch")
        if (
            generation_app.get("pid") != stable.get("windowProcessId")
            or generation_app.get("creationTime100ns")
            != stable.get("processCreationTime100ns")
        ):
            reasons.append("stable_frame_not_bound_to_app_generation")
        if (
            not isinstance(generation_backend.get("pid"), int)
            or isinstance(generation_backend.get("pid"), bool)
            or generation_backend.get("pid", 0) <= 0
            or not isinstance(generation_backend.get("creationTime100ns"), int)
            or isinstance(generation_backend.get("creationTime100ns"), bool)
            or generation_backend.get("creationTime100ns", 0) <= 0
        ):
            reasons.append("backend_process_generation_invalid")
        if (
            not isinstance(generation_webviews, list)
            or not generation_webviews
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("pid"), int)
                or isinstance(item.get("pid"), bool)
                or item.get("pid", 0) <= 0
                or not isinstance(item.get("creationTime100ns"), int)
                or isinstance(item.get("creationTime100ns"), bool)
                or item.get("creationTime100ns", 0) <= 0
                for item in (generation_webviews or [])
            )
        ):
            reasons.append("webview_process_generation_invalid")
        if not str(generation_artifact.get("backendStartedAt") or ""):
            reasons.append("backend_generation_started_at_missing")
        if not str(generation_artifact.get("frontendReadyReceivedAt") or ""):
            reasons.append("frontend_generation_ready_at_missing")

    if scenario in APP_UX_LIFECYCLE_SCENARIOS:
        event_evidence = (
            sample.get("eventEvidence")
            if isinstance(sample.get("eventEvidence"), dict)
            else {}
        )
        if event_evidence.get("installedRuntime") is not True:
            reasons.append("installed_runtime_event_unproven")
        if event_evidence.get("processGenerationFingerprint") != generation:
            reasons.append("event_process_generation_mismatch")
        if event_evidence.get("runId") != run_id:
            reasons.append("event_run_id_mismatch")
        if event_evidence.get("sampleId") != sample_id:
            reasons.append("event_sample_id_mismatch")
        session_id = str(event_evidence.get("sessionId") or "")
        try:
            session_uuid = uuid.UUID(session_id)
            if session_uuid.int == 0:
                raise ValueError("nil UUID")
            session_id = session_uuid.hex
        except (ValueError, AttributeError):
            reasons.append("event_session_id_invalid")
            session_id = ""
        if not isinstance(event_evidence.get("apiVersion"), int) or isinstance(
            event_evidence.get("apiVersion"), bool
        ) or event_evidence.get("apiVersion", 0) < 1:
            reasons.append("event_api_version_missing")
        expected_event_markers = {
            "stop_to_transcribing_visible": "recording_state_transcribing_emitted",
            "provider_result_to_completed_visible": "provider_response_complete",
            "session_finished_to_history_visible": "session_finished_emitted",
        }
        expected_event_sources = {
            "stop_to_transcribing_visible": "installed_backend_state_event",
            "provider_result_to_completed_visible": "installed_backend_provider_event",
            "session_finished_to_history_visible": "installed_backend_session_event",
        }
        event_ticks = event_evidence.get("qpcTicks")
        if event_evidence.get("marker") != expected_event_markers.get(scenario):
            reasons.append("event_marker_invalid")
        if event_evidence.get("source") != expected_event_sources.get(scenario):
            reasons.append("event_source_invalid")
        if (
            not isinstance(event_ticks, int)
            or isinstance(event_ticks, bool)
            or not isinstance(start_ticks, int)
            or not isinstance(end_ticks, int)
            or event_ticks < start_ticks
            or event_ticks > end_ticks
        ):
            reasons.append("event_qpc_outside_sample_window")
        if scenario != "stop_to_transcribing_visible" and event_ticks != start_ticks:
            reasons.append("event_qpc_start_mismatch")
        event_artifact, artifact_reason = _load_bound_json_artifact(
            event_evidence.get("artifact"), artifact_root, hash_cache
        )
        if artifact_reason:
            reasons.append(f"event_{artifact_reason}")
        elif not (
            event_artifact.get("ok") is True
            and event_artifact.get("source") == event_evidence.get("source")
            and event_artifact.get("marker") == event_evidence.get("marker")
            and event_artifact.get("qpcTicks") == event_ticks
            and event_artifact.get("qpcFrequency") == start_frequency
            and event_artifact.get("apiVersion") == event_evidence.get("apiVersion")
            and event_artifact.get("runId") == run_id
            and event_artifact.get("sampleId") == sample_id
            and event_artifact.get("sessionId") == session_id
            and event_artifact.get("processGenerationFingerprint") == generation
        ):
            reasons.append("event_artifact_payload_mismatch")

    frontend_performance = (
        sample.get("frontendPerformance")
        if isinstance(sample.get("frontendPerformance"), dict)
        else {}
    )
    if frontend_performance.get("observerSupported") is not True:
        reasons.append("long_task_observer_unavailable")
    source_instance_id = str(frontend_performance.get("sourceInstanceId") or "")
    try:
        source_uuid = uuid.UUID(source_instance_id)
        if source_uuid.int == 0:
            raise ValueError("nil UUID")
    except (ValueError, AttributeError):
        reasons.append("long_task_source_instance_invalid")
    for sequence_name in ("queryAfterSequence", "lastSequence"):
        sequence = frontend_performance.get(sequence_name)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            reasons.append(f"long_task_{sequence_name}_invalid")
    if (
        isinstance(frontend_performance.get("queryAfterSequence"), int)
        and isinstance(frontend_performance.get("lastSequence"), int)
        and frontend_performance["lastSequence"]
        < frontend_performance["queryAfterSequence"]
    ):
        reasons.append("long_task_sequence_regressed")
    if frontend_performance.get("truncated") is not False:
        reasons.append("long_task_window_truncated")
    if frontend_performance.get("heartbeatAcknowledged") is not True:
        reasons.append("long_task_heartbeat_unacknowledged")
    for counter_name in (
        "droppedEntriesBefore",
        "droppedEntriesAfter",
        "sequenceGapsBefore",
        "sequenceGapsAfter",
        "count",
    ):
        counter = frontend_performance.get(counter_name)
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            reasons.append(f"long_task_{counter_name}_invalid")
    if frontend_performance.get("droppedEntriesBefore") != frontend_performance.get(
        "droppedEntriesAfter"
    ):
        reasons.append("long_task_entries_dropped")
    if frontend_performance.get("sequenceGapsBefore") != frontend_performance.get(
        "sequenceGapsAfter"
    ):
        reasons.append("long_task_sequence_gap")
    measurement_end = frontend_performance.get("measurementEndQpcTicks")
    heartbeat_ack = frontend_performance.get("heartbeatAckQpcTicks")
    if (
        not isinstance(measurement_end, int)
        or isinstance(measurement_end, bool)
        or not isinstance(end_ticks, int)
        or measurement_end < end_ticks
    ):
        reasons.append("long_task_measurement_ended_before_frame")
    if (
        not isinstance(heartbeat_ack, int)
        or isinstance(heartbeat_ack, bool)
        or not isinstance(measurement_end, int)
        or heartbeat_ack <= measurement_end
    ):
        reasons.append("long_task_heartbeat_not_post_window")
    for duration_name in ("maxDurationMs", "totalDurationMs"):
        duration_value = frontend_performance.get(duration_name)
        if not finite_number(duration_value) or float(duration_value) < 0:
            reasons.append(f"long_task_{duration_name}_invalid")
    performance_artifact, artifact_reason = _load_bound_json_artifact(
        frontend_performance.get("artifact"), artifact_root, hash_cache
    )
    if artifact_reason:
        reasons.append(f"long_task_{artifact_reason}")
    elif any(
        performance_artifact.get(name) != frontend_performance.get(name)
        for name in (
            "observerSupported",
            "sourceInstanceId",
            "queryAfterSequence",
            "lastSequence",
            "truncated",
            "droppedEntriesBefore",
            "droppedEntriesAfter",
            "sequenceGapsBefore",
            "sequenceGapsAfter",
            "heartbeatAcknowledged",
            "measurementEndQpcTicks",
            "heartbeatAckQpcTicks",
            "count",
            "maxDurationMs",
            "totalDurationMs",
        )
    ):
        reasons.append("long_task_artifact_payload_mismatch")

    measured = duration_ms(start_ticks, end_ticks, start_frequency)
    if measured is None:
        reasons.append("duration_unavailable")
    reported_duration = sample.get("durationMs")
    if (
        measured is not None
        and (
            not finite_number(reported_duration)
            or abs(float(reported_duration) - measured) > 0.001
        )
    ):
        reasons.append("reported_duration_mismatch")

    normalized = dict(sample)
    normalized["sampleId"] = sample_id
    normalized["durationMs"] = measured if measured is not None else "unknown"
    return normalized, sorted(set(reasons))


def validate_app_ux_lifecycle_import(
    payload: Any,
    *,
    artifact_root: Path,
    required_samples_per_scenario: int,
    expected_run_id: str,
    expected_installed_exe_sha256: str,
    expected_harness_manifest_sha256: str,
) -> dict[str, Any]:
    """Validate provider-owned App-UX samples before a UIA collector merges them.

    The import is deliberately narrower than ``b7-app-ux-v1``.  It may contain
    only the three state/provider lifecycle scenarios that require an installed
    backend replay.  Every marker, external stable-frame observation, Long Task
    window, and resource value is still hash-bound and validated by the same
    sample contract as the final nine-scenario package.  Missing or additional
    scenarios fail closed; the UIA collector never manufactures lifecycle
    markers on behalf of a provider replay.
    """

    reasons: list[str] = []
    if not isinstance(payload, dict):
        payload = {}
        reasons.append("evidence_not_object")
    if payload.get("schemaVersion") != 1:
        reasons.append("schema_version_invalid")
    if payload.get("contract") != APP_UX_LIFECYCLE_IMPORT_CONTRACT:
        reasons.append("contract_version_invalid")

    def canonical_uuid(value: Any, reason: str) -> str:
        try:
            parsed = uuid.UUID(str(value or ""))
            if parsed.int == 0:
                raise ValueError("nil UUID")
            return parsed.hex
        except (ValueError, AttributeError):
            reasons.append(reason)
            return ""

    run_id = canonical_uuid(payload.get("runId"), "run_id_invalid")
    expected_run = canonical_uuid(expected_run_id, "expected_run_id_invalid")
    if run_id and expected_run and run_id != expected_run:
        reasons.append("run_id_mismatch")
    installed_sha256 = str(payload.get("installedExeSha256") or "")
    harness_sha256 = str(payload.get("harnessManifestSha256") or "")
    if installed_sha256 != expected_installed_exe_sha256:
        reasons.append("installed_exe_sha256_mismatch")
    if harness_sha256 != expected_harness_manifest_sha256:
        reasons.append("harness_manifest_sha256_mismatch")
    if payload.get("samplesPerScenario") != required_samples_per_scenario:
        reasons.append("samples_per_scenario_contract_mismatch")
    declared_order = payload.get("scenarioOrder")
    expected_order = [
        scenario
        for scenario in APP_UX_SCENARIOS
        if scenario in APP_UX_LIFECYCLE_SCENARIOS
    ]
    if declared_order != expected_order:
        reasons.append("scenario_order_invalid")

    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list):
        raw_samples = []
        reasons.append("samples_missing")
    hash_cache: dict[Path, str] = {}
    normalized_samples: list[dict[str, Any]] = []
    invalid_samples: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()
    seen_sample_ids: set[str] = set()
    counts = {scenario: 0 for scenario in expected_order}
    for index, sample in enumerate(raw_samples):
        if not isinstance(sample, dict) or sample.get("scenario") not in APP_UX_LIFECYCLE_SCENARIOS:
            invalid_samples.append(
                {"index": index, "reasons": ["non_lifecycle_scenario_in_import"]}
            )
            continue
        normalized, sample_reasons = _validate_app_ux_sample(
            sample,
            artifact_root=artifact_root,
            run_id=run_id,
            installed_exe_sha256=installed_sha256,
            harness_manifest_sha256=harness_sha256,
            hash_cache=hash_cache,
        )
        if normalized is None:
            invalid_samples.append({"index": index, "reasons": sample_reasons})
            continue
        scenario = str(normalized.get("scenario") or "")
        iteration = normalized.get("iteration")
        key = (scenario, iteration) if isinstance(iteration, int) else (scenario, -1)
        if key in seen_keys:
            sample_reasons.append("duplicate_scenario_iteration")
        seen_keys.add(key)
        normalized_sample_id = str(normalized.get("sampleId") or "")
        if normalized_sample_id in seen_sample_ids:
            sample_reasons.append("duplicate_sample_id")
        seen_sample_ids.add(normalized_sample_id)
        if sample_reasons:
            invalid_samples.append(
                {
                    "index": index,
                    "scenario": scenario,
                    "iteration": iteration,
                    "reasons": sorted(set(sample_reasons)),
                }
            )
            continue
        counts[scenario] += 1
        normalized_samples.append(normalized)

    for scenario, count in counts.items():
        if count != required_samples_per_scenario:
            reasons.append(f"scenario_sample_count:{scenario}")
    if invalid_samples:
        reasons.append("invalid_samples")

    resource_evidence = (
        payload.get("resourceEvidence")
        if isinstance(payload.get("resourceEvidence"), dict)
        else {}
    )
    resource_values: dict[str, Any] = {
        "idleCpuPercent": "unknown",
        "workingSetMb": "unknown",
    }
    required_total = required_samples_per_scenario * len(expected_order)
    if not resource_evidence:
        reasons.append("resource_evidence_missing")
    else:
        if resource_evidence.get("runId") != run_id:
            reasons.append("resource_run_id_mismatch")
        if resource_evidence.get("installedExeSha256") != installed_sha256:
            reasons.append("resource_installed_exe_mismatch")
        if resource_evidence.get("harnessManifestSha256") != harness_sha256:
            reasons.append("resource_harness_manifest_mismatch")
        if resource_evidence.get("sampleCount") != required_total:
            reasons.append("resource_sample_count_mismatch")
        for metric_name in ("idleCpuPercent", "workingSetMb"):
            metric_value = resource_evidence.get(metric_name)
            if not finite_number(metric_value) or float(metric_value) < 0:
                reasons.append(f"resource_{metric_name}_invalid")
            else:
                resource_values[metric_name] = float(metric_value)
        resource_artifact, artifact_reason = _load_bound_json_artifact(
            resource_evidence.get("artifact"), artifact_root, hash_cache
        )
        if artifact_reason:
            reasons.append(f"resource_{artifact_reason}")
        elif any(
            resource_artifact.get(name) != resource_evidence.get(name)
            for name in (
                "runId",
                "installedExeSha256",
                "harnessManifestSha256",
                "sampleCount",
                "idleCpuPercent",
                "workingSetMb",
            )
        ):
            reasons.append("resource_artifact_payload_mismatch")

    complete = len(normalized_samples) == required_total and not reasons
    return {
        "attempted": True,
        "contract": APP_UX_LIFECYCLE_IMPORT_CONTRACT,
        "metricEligible": complete,
        "metricBlockedReason": None if complete else "invalid_lifecycle_import",
        "reasons": sorted(set(reasons)),
        "runId": run_id or None,
        "installedExeSha256": installed_sha256 or None,
        "harnessManifestSha256": harness_sha256 or None,
        "scenarioOrder": expected_order,
        "requestedSamplesPerScenario": required_samples_per_scenario,
        "scenarioCounts": counts,
        "invalidSamples": invalid_samples,
        "results": normalized_samples,
        "resourceEvidence": resource_evidence,
        "resourceMetrics": resource_values,
    }


def validate_app_ux_evidence(
    payload: Any,
    *,
    artifact_root: Path,
    required_samples_per_scenario: int,
    expected_installed_exe_sha256: str | None = None,
    expected_harness_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the complete externally observed B7 App-UX matrix.

    This intentionally does not infer missing scenarios from a generic window
    show timing. Every sample is bound to the installed executable, one process
    generation, a QPC start, and a hash-verified external UIA observation.
    """

    reasons: list[str] = []
    if not isinstance(payload, dict):
        payload = {}
        reasons.append("evidence_not_object")
    if payload.get("schemaVersion") != 1:
        reasons.append("schema_version_invalid")
    if payload.get("contract") != APP_UX_EVIDENCE_CONTRACT:
        reasons.append("contract_version_invalid")
    run_id = str(payload.get("runId") or "")
    try:
        parsed_run_id = uuid.UUID(run_id)
        if parsed_run_id.int == 0:
            raise ValueError("nil UUID")
        run_id = parsed_run_id.hex
    except (ValueError, AttributeError):
        reasons.append("run_id_invalid")
        run_id = ""
    installed_exe_sha256 = str(payload.get("installedExeSha256") or "")
    harness_manifest_sha256 = str(payload.get("harnessManifestSha256") or "")
    if not _is_sha256(installed_exe_sha256):
        reasons.append("installed_exe_sha256_invalid")
    if not _is_sha256(harness_manifest_sha256):
        reasons.append("harness_manifest_sha256_invalid")
    if (
        expected_installed_exe_sha256 is not None
        and installed_exe_sha256 != expected_installed_exe_sha256
    ):
        reasons.append("installed_exe_sha256_mismatch")
    if (
        expected_harness_manifest_sha256 is not None
        and harness_manifest_sha256 != expected_harness_manifest_sha256
    ):
        reasons.append("harness_manifest_sha256_mismatch")
    if payload.get("samplesPerScenario") != required_samples_per_scenario:
        reasons.append("samples_per_scenario_contract_mismatch")

    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    if not isinstance(payload.get("samples"), list):
        reasons.append("samples_missing")
    hash_cache: dict[Path, str] = {}
    normalized_samples: list[dict[str, Any]] = []
    invalid_samples: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()
    seen_sample_ids: set[str] = set()
    scenario_durations: dict[str, list[float]] = {
        scenario: [] for scenario in APP_UX_SCENARIOS
    }
    for index, sample in enumerate(samples):
        normalized, sample_reasons = _validate_app_ux_sample(
            sample,
            artifact_root=artifact_root,
            run_id=run_id,
            installed_exe_sha256=installed_exe_sha256,
            harness_manifest_sha256=harness_manifest_sha256,
            hash_cache=hash_cache,
        )
        if normalized is None:
            invalid_samples.append({"index": index, "reasons": sample_reasons})
            continue
        scenario = str(normalized.get("scenario") or "")
        iteration = normalized.get("iteration")
        key = (scenario, iteration) if isinstance(iteration, int) else (scenario, -1)
        if key in seen_keys:
            sample_reasons.append("duplicate_scenario_iteration")
        seen_keys.add(key)
        normalized_sample_id = str(normalized.get("sampleId") or "")
        if normalized_sample_id in seen_sample_ids:
            sample_reasons.append("duplicate_sample_id")
        seen_sample_ids.add(normalized_sample_id)
        if sample_reasons:
            invalid_samples.append(
                {"index": index, "scenario": scenario, "iteration": iteration, "reasons": sample_reasons}
            )
            continue
        normalized_samples.append(normalized)
        scenario_durations[scenario].append(float(normalized["durationMs"]))

    scenario_results: dict[str, Any] = {}
    all_durations: list[float] = []
    for scenario in APP_UX_SCENARIOS:
        durations = scenario_durations[scenario]
        count_ok = len(durations) == required_samples_per_scenario
        if not count_ok:
            reasons.append(f"scenario_sample_count:{scenario}")
        all_durations.extend(durations)
        scenario_results[scenario] = {
            "sampleCount": len(durations),
            "requiredSampleCount": required_samples_per_scenario,
            "metricEligible": count_ok,
            "p50Ms": percentile_ms(durations, 50.0),
            "p75Ms": percentile_ms(durations, 75.0),
            "p95Ms": percentile_ms(durations, 95.0),
        }

    if invalid_samples:
        reasons.append("invalid_samples")
    long_task_counts: list[int] = []
    long_task_maxima: list[float] = []
    long_task_totals: list[float] = []
    for sample in normalized_samples:
        performance = sample.get("frontendPerformance")
        if not isinstance(performance, dict):
            continue
        long_task_counts.append(int(performance["count"]))
        long_task_maxima.append(float(performance["maxDurationMs"]))
        long_task_totals.append(float(performance["totalDurationMs"]))

    resource_evidence = (
        payload.get("resourceEvidence")
        if isinstance(payload.get("resourceEvidence"), dict)
        else {}
    )
    resource_metrics: dict[str, Any] = {
        "ui_long_tasks_gt_200ms": sum(long_task_counts)
        if len(long_task_counts) == len(normalized_samples)
        else "unknown",
        "ui_long_task_max_ms": max(long_task_maxima) if long_task_maxima else "unknown",
        "ui_long_task_total_ms": sum(long_task_totals) if long_task_totals else "unknown",
        "idle_cpu_pct": "unknown",
        "working_set_mb": "unknown",
    }
    if not resource_evidence:
        reasons.append("resource_evidence_missing")
    else:
        if resource_evidence.get("runId") != run_id:
            reasons.append("resource_run_id_mismatch")
        if resource_evidence.get("installedExeSha256") != installed_exe_sha256:
            reasons.append("resource_installed_exe_mismatch")
        if resource_evidence.get("harnessManifestSha256") != harness_manifest_sha256:
            reasons.append("resource_harness_manifest_mismatch")
        if resource_evidence.get("sampleCount") != len(normalized_samples):
            reasons.append("resource_sample_count_mismatch")
        for metric_name in ("idleCpuPercent", "workingSetMb"):
            metric_value = resource_evidence.get(metric_name)
            if not finite_number(metric_value) or float(metric_value) < 0:
                reasons.append(f"resource_{metric_name}_invalid")
        resource_artifact, artifact_reason = _load_bound_json_artifact(
            resource_evidence.get("artifact"), artifact_root, hash_cache
        )
        if artifact_reason:
            reasons.append(f"resource_{artifact_reason}")
        elif any(
            resource_artifact.get(name) != resource_evidence.get(name)
            for name in (
                "runId",
                "installedExeSha256",
                "harnessManifestSha256",
                "sampleCount",
                "idleCpuPercent",
                "workingSetMb",
            )
        ):
            reasons.append("resource_artifact_payload_mismatch")
        if not any(reason.startswith("resource_") for reason in reasons):
            resource_metrics["idle_cpu_pct"] = round(
                float(resource_evidence["idleCpuPercent"]), 3
            )
            resource_metrics["working_set_mb"] = round(
                float(resource_evidence["workingSetMb"]), 3
            )

    required_total = required_samples_per_scenario * len(APP_UX_SCENARIOS)
    matrix_complete = len(normalized_samples) == required_total and not reasons
    metrics = {
        "app_ux_p50_ms": percentile_ms(all_durations, 50.0) if matrix_complete else "unknown",
        "app_ux_p75_ms": percentile_ms(all_durations, 75.0) if matrix_complete else "unknown",
        "app_ux_p95_ms": percentile_ms(all_durations, 95.0) if matrix_complete else "unknown",
        "app_ux_sample_count": len(normalized_samples),
    }
    return {
        "attempted": True,
        "contract": APP_UX_EVIDENCE_CONTRACT,
        "metricEligible": matrix_complete,
        "metricBlockedReason": None if matrix_complete else "incomplete_app_ux_scenario_matrix",
        "reasons": sorted(set(reasons)),
        "runId": run_id or None,
        "installedExeSha256": installed_exe_sha256 or None,
        "harnessManifestSha256": harness_manifest_sha256 or None,
        "requestedSamplesPerScenario": required_samples_per_scenario,
        "requiredScenarioCount": len(APP_UX_SCENARIOS),
        "scenarioOrder": list(APP_UX_SCENARIOS),
        "scenarioResults": scenario_results,
        "invalidSamples": invalid_samples,
        "results": normalized_samples,
        "metrics": metrics,
        "externalStableFrameObserved": matrix_complete,
        "resourceMetrics": resource_metrics,
        "resourceEvidence": resource_evidence,
    }


def load_and_validate_app_ux_evidence(
    evidence_path: Path,
    *,
    required_samples_per_scenario: int,
    installed_exe_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    payload = load_json(evidence_path)
    result = validate_app_ux_evidence(
        payload,
        artifact_root=evidence_path.parent,
        required_samples_per_scenario=required_samples_per_scenario,
        expected_installed_exe_sha256=sha256_file(installed_exe_path),
        expected_harness_manifest_sha256=app_ux_harness_manifest_sha256(repo_root),
    )
    result["evidenceArtifact"] = {
        "path": evidence_path.name,
        "sha256": sha256_file(evidence_path) if evidence_path.is_file() else None,
    }
    return result


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
                "-FrontendPerformanceGatePath",
                str(app_observer_path),
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
    frontend_performance = (
        shell_menu.get("frontendPerformance")
        if isinstance(shell_menu.get("frontendPerformance"), dict)
        else {}
    )
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
    if finite_number(stability.get("processTreeCpuAvgPercent")):
        resource_metrics["idle_cpu_pct"] = round(float(stability["processTreeCpuAvgPercent"]), 3)
    if finite_number(stability.get("totalWorkingSetMaxMb")):
        resource_metrics["working_set_mb"] = round(float(stability["totalWorkingSetMaxMb"]), 3)
    performance_baseline = (
        frontend_performance.get("baseline")
        if isinstance(frontend_performance.get("baseline"), dict)
        else {}
    )
    performance_after_show = (
        frontend_performance.get("afterShow")
        if isinstance(frontend_performance.get("afterShow"), dict)
        else {}
    )
    baseline_window = (
        performance_baseline.get("window")
        if isinstance(performance_baseline.get("window"), dict)
        else {}
    )
    measured_window = (
        performance_after_show.get("window")
        if isinstance(performance_after_show.get("window"), dict)
        else {}
    )
    flush_request = (
        frontend_performance.get("flushRequest")
        if isinstance(frontend_performance.get("flushRequest"), dict)
        else {}
    )
    long_task_window_ms = frontend_performance.get("measurementWindowMs")
    measurement_end_ticks = frontend_performance.get("measurementEndQpcTicks")
    heartbeat_ack_ticks = frontend_performance.get("heartbeatAckQpcTicks")
    dropped_unchanged = (
        isinstance(baseline_window.get("droppedEntries"), int)
        and not isinstance(baseline_window.get("droppedEntries"), bool)
        and isinstance(measured_window.get("droppedEntries"), int)
        and not isinstance(measured_window.get("droppedEntries"), bool)
        and measured_window["droppedEntries"] == baseline_window["droppedEntries"]
    )
    sequence_gaps_unchanged = (
        isinstance(baseline_window.get("sequenceGaps"), int)
        and not isinstance(baseline_window.get("sequenceGaps"), bool)
        and isinstance(measured_window.get("sequenceGaps"), int)
        and not isinstance(measured_window.get("sequenceGaps"), bool)
        and measured_window["sequenceGaps"] == baseline_window["sequenceGaps"]
    )
    flush_sequence = flush_request.get("heartbeatSequence")
    flush_requested_at = flush_request.get("requestedAtUptimeSeconds")
    flush_requested_after_frontend = flush_request.get(
        "requestedAfterFrontendUptimeMs"
    )
    heartbeat_acknowledged = bool(
        frontend_performance.get("heartbeatAcknowledged") is True
        and isinstance(flush_sequence, int)
        and not isinstance(flush_sequence, bool)
        and flush_sequence > 0
        and measured_window.get("heartbeatSequence") == flush_sequence
        and finite_number(flush_requested_at)
        and finite_number(measured_window.get("heartbeatReceivedAtUptimeSeconds"))
        and float(measured_window["heartbeatReceivedAtUptimeSeconds"])
        >= float(flush_requested_at)
        and finite_number(measured_window.get("heartbeatObservedAtFrontendUptimeMs"))
        and finite_number(flush_requested_after_frontend)
        and float(measured_window["heartbeatObservedAtFrontendUptimeMs"])
        >= float(flush_requested_after_frontend)
        and isinstance(measurement_end_ticks, int)
        and not isinstance(measurement_end_ticks, bool)
        and isinstance(heartbeat_ack_ticks, int)
        and not isinstance(heartbeat_ack_ticks, bool)
        and heartbeat_ack_ticks > measurement_end_ticks
        and (observer_ticks is None or measurement_end_ticks >= int(observer_ticks))
    )
    long_tasks_measured = bool(
        performance_baseline.get("available")
        and performance_baseline.get("observerSupported") is True
        and performance_after_show.get("available")
        and performance_after_show.get("observerSupported") is True
        and performance_after_show.get("sourceInstanceId")
        == performance_baseline.get("sourceInstanceId")
        and measured_window.get("queryAfterSequence") == baseline_window.get("lastSequence")
        and measured_window.get("truncated") is False
        and dropped_unchanged
        and sequence_gaps_unchanged
        and heartbeat_acknowledged
        and isinstance(measured_window.get("count"), int)
        and not isinstance(measured_window.get("count"), bool)
        and finite_number(measured_window.get("maxDurationMs"))
        and finite_number(measured_window.get("totalDurationMs"))
        and finite_number(long_task_window_ms)
        and float(long_task_window_ms) > 0
    )
    if long_tasks_measured:
        resource_metrics["ui_long_tasks_gt_200ms"] = int(measured_window["count"])
        resource_metrics["ui_long_task_max_ms"] = round(
            float(measured_window["maxDurationMs"]),
            3,
        )
        resource_metrics["ui_long_task_total_ms"] = round(
            float(measured_window["totalDurationMs"]),
            3,
        )
        resource_metrics["ui_long_task_window_ms"] = round(float(long_task_window_ms), 3)
    else:
        resource_metrics["ui_long_tasks_gt_200ms"] = "unknown"
    return {
        "attempted": True,
        "scenario": "warm_app_activation",
        "diagnosticOnly": True,
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
        "frontendPerformance": {
            "measured": long_tasks_measured,
            "baseline": performance_baseline,
            "afterShow": performance_after_show,
            "measurementWindowMs": long_task_window_ms
            if finite_number(long_task_window_ms)
            else "unknown",
            "heartbeatAcknowledged": heartbeat_acknowledged,
            "flushRequest": flush_request,
        },
        "smokePath": str(smoke_path),
        "appObserverPath": str(app_observer_path),
        "stdoutTail": smoke_result.stdout[-2000:],
        "stderrTail": smoke_result.stderr[-2000:],
    }


def run_app_frame_probes(
    repo_root: Path,
    install_root: Path,
    output_dir: Path,
    timeout_sec: int,
    iterations: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    durations: list[Any] = []
    for iteration in range(1, max(1, iterations) + 1):
        iteration_dir = output_dir / f"app-frame-{iteration:02d}"
        try:
            result = run_app_frame_probe(
                repo_root,
                install_root,
                iteration_dir,
                timeout_sec,
            )
        except Exception as exc:
            result = {
                "attempted": True,
                "metricEligible": False,
                "externalStableFrameObserved": False,
                "durationMs": "unknown",
                "error": str(exc),
            }
        result["iteration"] = iteration
        results.append(result)
        durations.append(result.get("durationMs"))

    finite_resources: dict[str, list[float]] = {
        "idle_cpu_pct": [],
        "working_set_mb": [],
    }
    long_task_values: list[int] = []
    long_task_max_values: list[float] = []
    long_task_total_values: list[float] = []
    long_task_window_values: list[float] = []
    long_tasks_measured = True
    for result in results:
        resources = result.get("resourceMetrics")
        if not isinstance(resources, dict):
            resources = {}
        for metric in finite_resources:
            value = resources.get(metric)
            if finite_number(value):
                finite_resources[metric].append(float(value))
        long_tasks = resources.get("ui_long_tasks_gt_200ms")
        if isinstance(long_tasks, int) and not isinstance(long_tasks, bool):
            long_task_values.append(long_tasks)
            if finite_number(resources.get("ui_long_task_max_ms")):
                long_task_max_values.append(float(resources["ui_long_task_max_ms"]))
            if finite_number(resources.get("ui_long_task_total_ms")):
                long_task_total_values.append(float(resources["ui_long_task_total_ms"]))
            if finite_number(resources.get("ui_long_task_window_ms")):
                long_task_window_values.append(float(resources["ui_long_task_window_ms"]))
        else:
            long_tasks_measured = False

    resource_metrics: dict[str, Any] = {
        # Worst observed resource value is the conservative promotion guard.
        "idle_cpu_pct": round(max(finite_resources["idle_cpu_pct"]), 3)
        if finite_resources["idle_cpu_pct"]
        else "unknown",
        "working_set_mb": round(max(finite_resources["working_set_mb"]), 3)
        if finite_resources["working_set_mb"]
        else "unknown",
        "ui_long_tasks_gt_200ms": sum(long_task_values) if long_tasks_measured else "unknown",
    }
    if long_tasks_measured:
        resource_metrics.update(
            {
                "ui_long_task_max_ms": round(max(long_task_max_values), 3)
                if long_task_max_values
                else 0.0,
                "ui_long_task_total_ms": round(sum(long_task_total_values), 3),
                "ui_long_task_window_ms": round(sum(long_task_window_values), 3),
            }
        )
    diagnostic_sample_count = sum(finite_number(value) for value in durations)
    scenario_results = {
        scenario: {
            "sampleCount": diagnostic_sample_count
            if scenario == "warm_app_activation"
            else 0,
            "requiredSampleCount": max(1, iterations),
            "metricEligible": False,
            "p50Ms": percentile_ms(durations, 50.0)
            if scenario == "warm_app_activation"
            else "unknown",
            "p75Ms": percentile_ms(durations, 75.0)
            if scenario == "warm_app_activation"
            else "unknown",
            "p95Ms": percentile_ms(durations, 95.0)
            if scenario == "warm_app_activation"
            else "unknown",
        }
        for scenario in APP_UX_SCENARIOS
    }
    metrics = {
        # A generic shell show-window repeat is retained as diagnostic evidence,
        # but can no longer masquerade as the nine-scenario B7 App-UX metric.
        "app_ux_p50_ms": "unknown",
        "app_ux_p75_ms": "unknown",
        "app_ux_p95_ms": "unknown",
        "app_ux_sample_count": diagnostic_sample_count,
    }
    return {
        "attempted": True,
        "contract": APP_UX_EVIDENCE_CONTRACT,
        "metricEligible": False,
        "metricBlockedReason": "incomplete_app_ux_scenario_matrix",
        "reasons": [
            f"scenario_sample_count:{scenario}"
            for scenario in APP_UX_SCENARIOS
            if scenario != "warm_app_activation"
        ],
        "requestedSamplesPerScenario": max(1, iterations),
        "requiredScenarioCount": len(APP_UX_SCENARIOS),
        "scenarioOrder": list(APP_UX_SCENARIOS),
        "scenarioResults": scenario_results,
        "externalStableFrameObserved": False,
        "diagnosticStableFrameObserved": bool(results)
        and all(result.get("externalStableFrameObserved") for result in results),
        "results": results,
        "metrics": metrics,
        "resourceMetrics": resource_metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe real Scriber Windows user endpoints.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--timeout-sec", type=int, default=45)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--suite", choices=sorted(SAMPLE_PLANS), default="FastLocal")
    parser.add_argument(
        "--app-ux-evidence",
        default=os.environ.get("SCRIBER_B7_APP_UX_EVIDENCE", ""),
        help=(
            "Hash-bound b7-app-ux-v1 evidence package. Without it the legacy "
            "show-window probe remains diagnostic-only and App UX fails closed."
        ),
    )
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
    sample_plan = dict(SAMPLE_PLANS[args.suite])
    evidence: dict[str, Any] = {}
    try:
        evidence["overlayHotkey"] = run_overlay_hotkey_probes(
            repo_root,
            install_root,
            output_dir,
            args.timeout_sec,
            args.python,
            cold_samples=int(sample_plan["overlayCold"]),
            warm_samples=int(sample_plan["overlayWarm"]),
        )
    except Exception as exc:
        evidence["overlayHotkey"] = {"attempted": True, "metricEligible": False, "error": str(exc)}

    try:
        if args.app_ux_evidence:
            evidence["appFrame"] = load_and_validate_app_ux_evidence(
                Path(args.app_ux_evidence).resolve(),
                required_samples_per_scenario=int(
                    sample_plan["appUxPerScenario"]
                ),
                installed_exe_path=install_root / "scriber-desktop.exe",
                repo_root=repo_root,
            )
        else:
            evidence["appFrame"] = run_app_frame_probes(
                repo_root,
                install_root,
                output_dir,
                args.timeout_sec,
                int(sample_plan["appUxPerScenario"]),
            )
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
        evidence["providerReplay"] = run_provider_text_replay(
            repo_root,
            install_root,
            output_dir,
            args.timeout_sec,
            int(sample_plan["providerReplay"]),
        )
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
        "overlay_warm_p50_ms": "unknown",
        "overlay_warm_p95_ms": "unknown",
        "overlay_cold_p50_ms": "unknown",
        "overlay_cold_p95_ms": "unknown",
        "microsoft_local_tail_p50_ms": "unknown",
        "microsoft_local_tail_p95_ms": "unknown",
        "soniox_local_tail_p50_ms": "unknown",
        "soniox_local_tail_p95_ms": "unknown",
        "app_ux_p50_ms": "unknown",
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
    app_metrics = app_frame.get("metrics") if isinstance(app_frame.get("metrics"), dict) else {}
    metrics.update(app_metrics)
    resource_metrics = app_frame.get("resourceMetrics") if isinstance(app_frame.get("resourceMetrics"), dict) else {}
    for key in ("ui_long_tasks_gt_200ms", "idle_cpu_pct", "working_set_mb"):
        if key in resource_metrics:
            metrics[key] = resource_metrics[key]
    provider_ok = bool(evidence["providerReplay"].get("ok"))
    score_required = [
        "overlay_warm_p50_ms",
        "overlay_warm_p95_ms",
        "overlay_cold_p50_ms",
        "overlay_cold_p95_ms",
        "microsoft_local_tail_p50_ms",
        "microsoft_local_tail_p95_ms",
        "soniox_local_tail_p50_ms",
        "soniox_local_tail_p95_ms",
        "app_ux_p50_ms",
        "app_ux_p95_ms",
    ]
    guard_required = [
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
    score_complete = all(metrics.get(name) != "unknown" for name in score_required)
    guards_complete = all(metrics.get(name) != "unknown" for name in guard_required)
    clean_errors = all(
        finite_number(metrics.get(name)) and float(metrics[name]) == 0.0
        for name in (
            "text_errors",
            "focus_errors",
            "clipboard_errors",
            "overlay_errors",
            "ui_long_tasks_gt_200ms",
        )
    )
    if score_complete and all(
        finite_number(metrics.get(name)) and float(metrics[name]) == 0.0
        for name in ("text_errors", "focus_errors", "clipboard_errors", "overlay_errors")
    ):
        metrics["local_wux"] = compute_local_wux(metrics, load_baseline_metrics(repo_root))
    promotion_eligible = (
        metrics["local_wux"] != "unknown"
        and guards_complete
        and clean_errors
        and bool(evidence.get("overlayHotkey", {}).get("metricEligible"))
        and bool(app_frame.get("metricEligible"))
        and bool(evidence["providerReplay"].get("metricEligible"))
    )
    status = "MEASURED" if promotion_eligible else ("PARTIAL" if overlay_ok or app_ok or provider_ok else "BLOCKED")
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
        "suite": args.suite,
        "samplePlan": sample_plan,
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

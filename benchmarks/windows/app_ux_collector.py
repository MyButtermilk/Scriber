from __future__ import annotations

"""Collect the externally visible B7 App-UX scenario matrix on Windows.

Six scenarios are driven through real Windows UI Automation actions against an
installed production build.  The three provider/lifecycle scenarios are never
synthesized here: an installed provider-replay harness must provide a
``b7-app-ux-lifecycle-import-v1`` package.  The import schema lives beside this
collector and its stronger semantic validation is implemented in
``endpoint_probe.validate_app_ux_lifecycle_import``.

Typical orchestration (the run id is persisted in the request file):

1. ``app_ux_collector.py --prepare-only ...``
2. Installed provider replay reads ``app-ux-lifecycle-request.json`` and emits
   the required lifecycle import plus its hash-bound artifacts.
3. Run this collector again with ``--lifecycle-evidence <file>``.

The final package is accepted only when all nine scenarios have exactly the
requested number of samples.  No internal navigation callback or fabricated
provider marker can substitute for a UIA stable frame or installed event.
"""

import argparse
import copy
import ctypes
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - reported as a fail-closed runtime error
    psutil = None  # type: ignore[assignment]

try:
    from . import endpoint_probe as contract
except ImportError:  # Direct script execution from benchmarks/windows.
    import endpoint_probe as contract  # type: ignore[no-redef]


LIFECYCLE_REQUEST_CONTRACT = "b7-app-ux-lifecycle-request-v1"
UIA_SCENARIOS = tuple(
    scenario
    for scenario in contract.APP_UX_SCENARIOS
    if scenario not in contract.APP_UX_LIFECYCLE_SCENARIOS
)
LIFECYCLE_SCENARIOS = tuple(
    scenario
    for scenario in contract.APP_UX_SCENARIOS
    if scenario in contract.APP_UX_LIFECYCLE_SCENARIOS
)
FIRST_TRANSCRIPT_TITLE = "B7 stable transcript alpha"
SECOND_TRANSCRIPT_TITLE = "B7 stable transcript beta"
QPC_FREQUENCY = contract.qpc_frequency()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _artifact_binding(path: Path, artifact_root: Path) -> dict[str, str]:
    resolved = path.resolve()
    relative = resolved.relative_to(artifact_root.resolve()).as_posix()
    return {"path": relative, "sha256": contract.sha256_file(resolved)}


def _write_artifact(
    artifact_root: Path,
    relative_path: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    path = artifact_root / relative_path
    _write_json(path, payload)
    return _artifact_binding(path, artifact_root)


def _canonical_uuid(value: str | None = None) -> str:
    parsed = uuid.UUID(value) if value else uuid.uuid4()
    if parsed.int == 0:
        raise ValueError("run id must not be the nil UUID")
    return parsed.hex


def _powershell_executable() -> str:
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"


def _script_process(
    script: Path,
    arguments: list[str],
    *,
    cwd: Path,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            _powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_script(
    process: subprocess.Popen[str],
    timeout_sec: float,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int, str, str]:
    try:
        stdout, stderr = process.communicate(timeout=max(1.0, timeout_sec))
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        stderr = f"{stderr}\ncollector timeout after {timeout_sec:.1f}s"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")
    return int(process.returncode or 0), stdout or "", stderr or ""


def _start_observer(
    repo_root: Path,
    sample_dir: Path,
    *,
    expected_text: str,
    forbidden_text: str = "",
    process_id: int = 0,
    process_creation_time_100ns: int = 0,
    gate_path: Path | None = None,
    timeout_sec: float = 20.0,
) -> tuple[subprocess.Popen[str], Path]:
    output_path = sample_dir / "stable-frame.json"
    arguments = [
        "-WindowTitle",
        "Scriber",
        "-ExpectedText",
        expected_text,
        "-TimeoutSec",
        str(max(1.0, timeout_sec)),
        "-OutputPath",
        str(output_path),
    ]
    if forbidden_text:
        arguments.extend(["-ForbiddenText", forbidden_text])
    if process_id > 0:
        arguments.extend(["-ExpectedProcessId", str(process_id)])
    if process_creation_time_100ns > 0:
        arguments.extend(
            ["-ExpectedProcessCreationTime100ns", str(process_creation_time_100ns)]
        )
    if gate_path is not None:
        arguments.extend(["-StartAfterPath", str(gate_path)])
    return (
        _script_process(
            repo_root / "benchmarks" / "windows" / "app_observer.ps1",
            arguments,
            cwd=repo_root,
        ),
        output_path,
    )


def _run_action(
    repo_root: Path,
    sample_dir: Path,
    *,
    process_id: int,
    process_creation_time_100ns: int,
    control_name: str,
    control_type: str,
    control_name_match: str = "Exact",
    gate_path: Path | None,
    timeout_sec: float,
    output_name: str = "user-action.json",
) -> dict[str, Any]:
    output_path = sample_dir / output_name
    arguments = [
        "-ProcessId",
        str(process_id),
        "-ProcessCreationTime100ns",
        str(process_creation_time_100ns),
        "-WindowTitle",
        "Scriber",
        "-ControlName",
        control_name,
        "-ControlNameMatch",
        control_name_match,
        "-ControlType",
        control_type,
        "-TimeoutSec",
        str(max(1.0, timeout_sec)),
        "-OutputPath",
        str(output_path),
    ]
    if gate_path is not None:
        arguments.extend(["-InputMarkerPath", str(gate_path)])
    process = _script_process(
        repo_root / "benchmarks" / "windows" / "app_action.ps1",
        arguments,
        cwd=repo_root,
    )
    exit_code, _, stderr = _wait_script(
        process,
        timeout_sec + 5,
        sample_dir / f"{output_name}.stdout.txt",
        sample_dir / f"{output_name}.stderr.txt",
    )
    payload = contract.load_json(output_path)
    if exit_code != 0 or payload.get("ok") is not True:
        raise RuntimeError(
            f"UIA action {control_name!r} failed (exit {exit_code}): {stderr[-500:]}"
        )
    return payload


def _wait_observer(
    process: subprocess.Popen[str],
    output_path: Path,
    sample_dir: Path,
    timeout_sec: float,
) -> dict[str, Any]:
    exit_code, _, stderr = _wait_script(
        process,
        timeout_sec + 5,
        sample_dir / "stable-frame.stdout.txt",
        sample_dir / "stable-frame.stderr.txt",
    )
    payload = contract.load_json(output_path)
    if exit_code != 0 or payload.get("ok") is not True:
        raise RuntimeError(
            f"UIA stable-frame observer failed (exit {exit_code}): {stderr[-500:]}"
        )
    return payload


def _request_json(
    port: int,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout_sec: float = 3.0,
) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-Scriber-Token": token}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=max(0.1, timeout_sec)) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def _frontend_performance(
    port: int,
    token: str,
    *,
    after_sequence: int | None = None,
    source_instance_id: str = "",
) -> dict[str, Any]:
    query: dict[str, str] = {}
    if after_sequence is not None:
        query["afterSequence"] = str(after_sequence)
    if source_instance_id:
        query["sourceInstanceId"] = source_instance_id
    suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
    return _request_json(
        port,
        token,
        f"/api/runtime/frontend-performance{suffix}",
    )


def _wait_frontend_performance(
    port: int,
    token: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout_sec)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = _frontend_performance(port, token)
            source = str(last.get("sourceInstanceId") or "")
            window = last.get("window") if isinstance(last.get("window"), dict) else {}
            if (
                last.get("available") is True
                and last.get("observerSupported") is True
                and source
                and isinstance(window.get("lastSequence"), int)
            ):
                _canonical_uuid(source)
                return last
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"frontend Long Tasks diagnostics unavailable: {last}")


def _collect_frontend_performance_window(
    artifact_root: Path,
    sample_dir: Path,
    port: int,
    token: str,
    stable_qpc_ticks: int,
    *,
    baseline: dict[str, Any] | None,
    include_from_source_start: bool = False,
    timeout_sec: float = 8.0,
) -> tuple[dict[str, Any], dict[str, str]]:
    initial = baseline or _wait_frontend_performance(port, token, timeout_sec)
    initial_window = initial.get("window") if isinstance(initial.get("window"), dict) else {}
    source_id = str(initial.get("sourceInstanceId") or "")
    _canonical_uuid(source_id)
    query_after = 0 if include_from_source_start else int(initial_window["lastSequence"])
    dropped_before = int(initial_window.get("droppedEntries") or 0)
    gaps_before = int(initial_window.get("sequenceGaps") or 0)
    measurement_end = contract.qpc_ticks()
    if measurement_end < stable_qpc_ticks:
        raise RuntimeError("Long Task measurement ended before the stable visible frame")
    flush = _request_json(
        port,
        token,
        "/api/runtime/frontend-performance/flush-request",
        method="POST",
        body={"apiVersion": "1", "sourceInstanceId": source_id},
    )
    heartbeat_sequence = flush.get("heartbeatSequence")
    if flush.get("accepted") is not True or not isinstance(heartbeat_sequence, int):
        raise RuntimeError("frontend Long Task flush request was not accepted")

    deadline = time.monotonic() + max(1.0, timeout_sec)
    measured: dict[str, Any] = {}
    while time.monotonic() < deadline:
        measured = _frontend_performance(
            port,
            token,
            after_sequence=query_after,
            source_instance_id=source_id,
        )
        window = measured.get("window") if isinstance(measured.get("window"), dict) else {}
        if (
            measured.get("available") is True
            and measured.get("observerSupported") is True
            and measured.get("sourceInstanceId") == source_id
            and window.get("heartbeatSequence") == heartbeat_sequence
            and window.get("heartbeatReceivedAtUptimeSeconds") is not None
            and window.get("heartbeatObservedAtFrontendUptimeMs") is not None
        ):
            break
        time.sleep(0.03)
    else:
        raise RuntimeError("frontend Long Task flush heartbeat was not acknowledged")
    heartbeat_ack = contract.qpc_ticks()
    window = measured["window"]
    payload = {
        "observerSupported": True,
        "sourceInstanceId": source_id,
        "queryAfterSequence": query_after,
        "lastSequence": int(window["lastSequence"]),
        "truncated": bool(window["truncated"]),
        "droppedEntriesBefore": dropped_before,
        "droppedEntriesAfter": int(window["droppedEntries"]),
        "sequenceGapsBefore": gaps_before,
        "sequenceGapsAfter": int(window["sequenceGaps"]),
        "heartbeatAcknowledged": True,
        "measurementEndQpcTicks": measurement_end,
        "heartbeatAckQpcTicks": heartbeat_ack,
        "count": int(window["count"]),
        "maxDurationMs": float(window["maxDurationMs"]),
        "totalDurationMs": float(window["totalDurationMs"]),
    }
    if (
        payload["truncated"]
        or payload["droppedEntriesBefore"] != payload["droppedEntriesAfter"]
        or payload["sequenceGapsBefore"] != payload["sequenceGapsAfter"]
        or payload["heartbeatAckQpcTicks"] <= payload["measurementEndQpcTicks"]
    ):
        raise RuntimeError("frontend Long Task measurement window was not lossless")
    binding = _write_artifact(
        artifact_root,
        (sample_dir / "frontend-performance.json").relative_to(artifact_root).as_posix(),
        payload,
    )
    return payload, binding


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _seed_transcripts(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "transcripts.db"
    now = "2026-07-15T12:00:00+00:00"
    rows = [
        (
            "b7-alpha-transcript",
            FIRST_TRANSCRIPT_TITLE,
            "15.07.2026, 14:00",
            "00:42",
            "B7 alpha transcript body used only for installed UI timing.",
            now,
        ),
        (
            "b7-beta-transcript",
            SECOND_TRANSCRIPT_TITLE,
            "15.07.2026, 14:01",
            "01:03",
            "B7 beta transcript body used only for installed UI timing.",
            "2026-07-15T12:01:00+00:00",
        ),
    ]
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE transcripts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                duration TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT NOT NULL,
                language TEXT NOT NULL,
                step TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                channel TEXT DEFAULT '',
                thumbnail_url TEXT DEFAULT '',
                content TEXT DEFAULT '',
                preview TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT DEFAULT '',
                summary_status TEXT DEFAULT 'idle',
                summary_error TEXT DEFAULT '',
                summary_updated_at TEXT DEFAULT ''
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO transcripts (
                id, title, date, duration, status, type, language, step,
                source_url, channel, thumbnail_url, content, preview,
                created_at, updated_at, summary, summary_status,
                summary_error, summary_updated_at
            ) VALUES (?, ?, ?, ?, 'completed', 'mic', 'English', '', '', '', '', ?, ?, ?, ?, '', 'idle', '', '')
            """,
            [
                (identifier, title, date, duration, body, title, created, created)
                for identifier, title, date, duration, body, created in rows
            ],
        )
        connection.commit()


def _runtime_environment(data_dir: Path, token: str, port: int) -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "SCRIBER_REPO_ROOT",
        "SCRIBER_PYTHON",
        "SCRIBER_BACKEND_EXE",
        "SCRIBER_RUNTIME_MODE",
        "SCRIBER_BACKEND_LAUNCH_KIND",
        "SCRIBER_LEGACY_DATA_DIR",
    ):
        env.pop(name, None)
    env.update(
        {
            "SCRIBER_DATA_DIR": str(data_dir),
            "SCRIBER_FORCE_MANAGED_BACKEND": "1",
            "SCRIBER_SESSION_TOKEN": token,
            "SCRIBER_WEB_HOST": "127.0.0.1",
            "SCRIBER_WEB_PORT": str(port),
            "SCRIBER_DISABLE_HOTKEYS": "1",
            "SCRIBER_DISABLE_DEVICE_MONITOR": "1",
            "SCRIBER_SKIP_LEGACY_DATA_MIGRATION": "1",
            "SCRIBER_AUTO_MIGRATE_LEGACY_DATA": "0",
            "SCRIBER_AUTO_SUMMARIZE": "0",
            "SCRIBER_MIC_ALWAYS_ON": "0",
        }
    )
    return env


def _wait_runtime(
    app_process: subprocess.Popen[Any],
    port: int,
    token: str,
    timeout_sec: float,
) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + max(1.0, timeout_sec)
    health: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if app_process.poll() is not None:
            raise RuntimeError(
                f"installed Scriber exited during startup with {app_process.returncode}"
            )
        try:
            health = _request_json(port, token, "/api/health")
            backend_pid = int(health.get("pid") or 0)
            if (
                health.get("ok") is True
                and health.get("runtimeMode") == "tauri-supervised"
                and backend_pid > 0
            ):
                ready = _request_json(port, token, "/api/runtime/frontend-ready")
                last_seen = ready.get("lastSeen") if isinstance(ready.get("lastSeen"), dict) else {}
                if ready.get("ready") is True and int(last_seen.get("pid") or 0) == backend_pid:
                    return backend_pid, health
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"installed Scriber runtime did not become ready: {health}")


def _wait_generation(
    app_pid: int,
    backend_pid: int,
    port: int,
    token: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout_sec)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = contract.process_generation_snapshot(app_pid, backend_pid, port, token)
        if last.get("ok"):
            return last
        time.sleep(0.05)
    raise RuntimeError(f"installed process generation could not be attested: {last}")


def _same_executable_processes(executable: Path) -> list[int]:
    if psutil is None:
        raise RuntimeError("psutil is required for process-tree resource evidence")
    expected = os.path.normcase(str(executable.resolve()))
    matches: list[int] = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if os.path.normcase(str(Path(process.info["exe"]).resolve())) == expected:
                matches.append(int(process.info["pid"]))
        except (psutil.Error, OSError, TypeError):
            continue
    return matches


def _process_tree(app_pid: int, backend_pid: int) -> list[Any]:
    if psutil is None:
        raise RuntimeError("psutil is required for process-tree resource evidence")
    processes: dict[int, Any] = {}
    for root_pid in (app_pid, backend_pid):
        try:
            root = psutil.Process(root_pid)
            processes[root.pid] = root
            for child in root.children(recursive=True):
                processes[child.pid] = child
        except psutil.Error:
            continue
    return list(processes.values())


def _resource_snapshot(app_pid: int, backend_pid: int) -> dict[str, float]:
    cpu_seconds = 0.0
    working_set_bytes = 0
    for process in _process_tree(app_pid, backend_pid):
        try:
            cpu = process.cpu_times()
            cpu_seconds += float(cpu.user) + float(cpu.system)
            working_set_bytes += int(process.memory_info().rss)
        except psutil.Error:
            continue
    return {
        "cpuSeconds": cpu_seconds,
        "workingSetMb": working_set_bytes / (1024.0 * 1024.0),
    }


def _idle_resource_observation(app_pid: int, backend_pid: int) -> dict[str, float]:
    before = _resource_snapshot(app_pid, backend_pid)
    started = time.perf_counter()
    time.sleep(1.0)
    after = _resource_snapshot(app_pid, backend_pid)
    elapsed = max(0.001, time.perf_counter() - started)
    delta = max(0.0, after["cpuSeconds"] - before["cpuSeconds"])
    logical_processors = max(1, os.cpu_count() or 1)
    return {
        "idleCpuPercent": round(delta / (elapsed * logical_processors) * 100.0, 3),
        "workingSetMb": round(max(before["workingSetMb"], after["workingSetMb"]), 3),
    }


def _window_visible_for_process(process_id: int) -> bool:
    hwnd = contract.find_window("Scriber")
    if not hwnd or contract.window_process_id(hwnd) != process_id:
        return False
    return bool(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(hwnd)))


def _wait_window_hidden(process_id: int, timeout_sec: float) -> None:
    deadline = time.monotonic() + max(0.5, timeout_sec)
    while time.monotonic() < deadline:
        if not _window_visible_for_process(process_id):
            return
        time.sleep(0.03)
    raise RuntimeError("the primary Scriber window did not hide before warm activation")


def _sample_from_artifacts(
    artifact_root: Path,
    sample_dir: Path,
    *,
    scenario: str,
    iteration: int,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    generation: dict[str, Any],
    action: dict[str, Any],
    action_path: Path,
    observer: dict[str, Any],
    observer_path: Path,
    performance: dict[str, Any],
    performance_binding: dict[str, str],
) -> dict[str, Any]:
    start_ticks = int(action["qpcTicks"])
    end_ticks = int(observer["stableQpcTicks"])
    measured = contract.duration_ms(start_ticks, end_ticks, int(action["qpcFrequency"]))
    if measured is None:
        raise RuntimeError(f"{scenario} produced no monotonic App UX duration")
    fingerprint = str(generation["fingerprint"])
    generation_binding = _write_artifact(
        artifact_root,
        (sample_dir / "process-generation.json").relative_to(artifact_root).as_posix(),
        generation,
    )
    return {
        "scenario": scenario,
        "iteration": iteration,
        "runId": run_id,
        "sampleId": uuid.uuid4().hex,
        "installedExeSha256": installed_sha256,
        "harnessManifestSha256": harness_sha256,
        "processGenerationFingerprint": fingerprint,
        "processGeneration": {
            "fingerprint": fingerprint,
            "artifact": generation_binding,
        },
        "start": {
            "marker": "user_input_received",
            "source": str(action["source"]),
            "qpcTicks": start_ticks,
            "qpcFrequency": int(action["qpcFrequency"]),
            "processGenerationFingerprint": fingerprint,
            "artifact": _artifact_binding(action_path, artifact_root),
        },
        "stableFrame": {
            "marker": "first_stable_visible_frame",
            "source": "windows_uia",
            "qpcTicks": end_ticks,
            "qpcFrequency": int(observer["qpcFrequency"]),
            "stableSampleCount": int(observer["stableSampleCount"]),
            "expectedTextSha256": list(observer["expectedTextSha256"]),
            "windowProcessId": int(observer["processId"]),
            "processCreationTime100ns": int(observer["processCreationTime100ns"]),
            "processGenerationFingerprint": fingerprint,
            "artifact": _artifact_binding(observer_path, artifact_root),
        },
        "durationMs": measured,
        "frontendPerformance": {**performance, "artifact": performance_binding},
    }


def _run_uia_sample(
    repo_root: Path,
    artifact_root: Path,
    sample_dir: Path,
    *,
    scenario: str,
    iteration: int,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    app_pid: int,
    backend_pid: int,
    port: int,
    token: str,
    control_name: str,
    control_type: str,
    control_name_match: str = "Exact",
    expected_text: str,
    forbidden_text: str,
    timeout_sec: float,
) -> dict[str, Any]:
    generation = _wait_generation(app_pid, backend_pid, port, token, timeout_sec)
    app_created = int(generation["app"]["creationTime100ns"])
    baseline = _wait_frontend_performance(port, token, timeout_sec)
    gate_path = sample_dir / "input-ready.marker"
    observer_process, observer_path = _start_observer(
        repo_root,
        sample_dir,
        expected_text=expected_text,
        forbidden_text=forbidden_text,
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        gate_path=gate_path,
        timeout_sec=timeout_sec,
    )
    action = _run_action(
        repo_root,
        sample_dir,
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        control_name=control_name,
        control_type=control_type,
        control_name_match=control_name_match,
        gate_path=gate_path,
        timeout_sec=timeout_sec,
    )
    action_path = sample_dir / "user-action.json"
    observer = _wait_observer(
        observer_process, observer_path, sample_dir, timeout_sec
    )
    performance, performance_binding = _collect_frontend_performance_window(
        artifact_root,
        sample_dir,
        port,
        token,
        int(observer["stableQpcTicks"]),
        baseline=baseline,
        timeout_sec=timeout_sec,
    )
    observed_generation = _wait_generation(
        app_pid, backend_pid, port, token, timeout_sec
    )
    if not contract.process_generation_matches(generation, observed_generation):
        raise RuntimeError(f"{scenario} crossed an installed process generation")
    return _sample_from_artifacts(
        artifact_root,
        sample_dir,
        scenario=scenario,
        iteration=iteration,
        run_id=run_id,
        installed_sha256=installed_sha256,
        harness_sha256=harness_sha256,
        generation=generation,
        action=action,
        action_path=action_path,
        observer=observer,
        observer_path=observer_path,
        performance=performance,
        performance_binding=performance_binding,
    )


def _navigate_unmeasured(
    repo_root: Path,
    setup_dir: Path,
    *,
    app_pid: int,
    app_created: int,
    control_name: str,
    control_type: str,
    expected_text: str,
    forbidden_text: str = "",
    timeout_sec: float,
) -> None:
    _run_action(
        repo_root,
        setup_dir,
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        control_name=control_name,
        control_type=control_type,
        gate_path=None,
        timeout_sec=timeout_sec,
        output_name="setup-action.json",
    )
    observer_process, observer_path = _start_observer(
        repo_root,
        setup_dir,
        expected_text=expected_text,
        forbidden_text=forbidden_text,
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        timeout_sec=timeout_sec,
    )
    _wait_observer(observer_process, observer_path, setup_dir, timeout_sec)


def _run_cold_launch(
    repo_root: Path,
    artifact_root: Path,
    sample_dir: Path,
    executable: Path,
    runtime_env: dict[str, str],
    *,
    iteration: int,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    port: int,
    token: str,
    timeout_sec: float,
) -> tuple[dict[str, Any], subprocess.Popen[Any], int, dict[str, Any]]:
    if _same_executable_processes(executable):
        raise RuntimeError(
            "another installed Scriber process is running; cold launch would be ambiguous"
        )
    gate_path = sample_dir / "input-ready.marker"
    observer_process, observer_path = _start_observer(
        repo_root,
        sample_dir,
        expected_text="Recent recordings",
        gate_path=gate_path,
        timeout_sec=timeout_sec,
    )
    app_process: subprocess.Popen[Any] | None = None
    backend_pid = 0
    completed = False
    try:
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text("ready\n", encoding="ascii")
        started_ticks = contract.qpc_ticks()
        app_process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=runtime_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        action_payload = {
            "schemaVersion": 1,
            "ok": True,
            "endpoint": "user_input_received",
            "source": "windows_create_process",
            "qpcTicks": started_ticks,
            "qpcFrequency": QPC_FREQUENCY,
            "processId": int(app_process.pid),
        }
        action_path = sample_dir / "user-action.json"
        _write_json(action_path, action_payload)
        backend_pid, _ = _wait_runtime(app_process, port, token, timeout_sec)
        observer = _wait_observer(
            observer_process, observer_path, sample_dir, timeout_sec
        )
        if int(observer.get("processId") or 0) != app_process.pid:
            raise RuntimeError(
                "cold stable frame did not belong to the launched installed process"
            )
        generation = _wait_generation(
            app_process.pid, backend_pid, port, token, timeout_sec
        )
        if int(observer.get("processCreationTime100ns") or 0) != int(
            generation["app"]["creationTime100ns"]
        ):
            raise RuntimeError("cold stable frame crossed the launched process generation")
        performance, performance_binding = _collect_frontend_performance_window(
            artifact_root,
            sample_dir,
            port,
            token,
            int(observer["stableQpcTicks"]),
            baseline=None,
            include_from_source_start=True,
            timeout_sec=timeout_sec,
        )
        final_generation = _wait_generation(
            app_process.pid, backend_pid, port, token, timeout_sec
        )
        if not contract.process_generation_matches(generation, final_generation):
            raise RuntimeError("cold launch crossed an installed process generation")
        sample = _sample_from_artifacts(
            artifact_root,
            sample_dir,
            scenario="cold_app_launch",
            iteration=iteration,
            run_id=run_id,
            installed_sha256=installed_sha256,
            harness_sha256=harness_sha256,
            generation=generation,
            action=action_payload,
            action_path=action_path,
            observer=observer,
            observer_path=observer_path,
            performance=performance,
            performance_binding=performance_binding,
        )
        completed = True
        return sample, app_process, backend_pid, generation
    finally:
        if not completed:
            if observer_process.poll() is None:
                observer_process.kill()
                observer_process.communicate(timeout=5)
            if backend_pid <= 0:
                try:
                    backend_pid = int(
                        _request_json(port, token, "/api/health", timeout_sec=1.0).get(
                            "pid"
                        )
                        or 0
                    )
                except Exception:
                    backend_pid = 0
            if app_process is not None and backend_pid > 0:
                contract.terminate_runtime(app_process.pid, backend_pid, port, token)
            elif app_process is not None and app_process.poll() is None:
                app_process.terminate()
                try:
                    app_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    app_process.kill()


def _run_warm_activation(
    repo_root: Path,
    artifact_root: Path,
    sample_dir: Path,
    executable: Path,
    runtime_env: dict[str, str],
    *,
    iteration: int,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    app_pid: int,
    backend_pid: int,
    port: int,
    token: str,
    timeout_sec: float,
) -> dict[str, Any]:
    generation = _wait_generation(app_pid, backend_pid, port, token, timeout_sec)
    app_created = int(generation["app"]["creationTime100ns"])
    setup_dir = sample_dir / "hide-setup"
    _run_action(
        repo_root,
        setup_dir,
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        control_name="Close window",
        control_type="Button",
        gate_path=None,
        timeout_sec=timeout_sec,
        output_name="hide-action.json",
    )
    _wait_window_hidden(app_pid, timeout_sec)
    baseline = _wait_frontend_performance(port, token, timeout_sec)
    gate_path = sample_dir / "input-ready.marker"
    observer_process, observer_path = _start_observer(
        repo_root,
        sample_dir,
        expected_text="Recent recordings",
        process_id=app_pid,
        process_creation_time_100ns=app_created,
        gate_path=gate_path,
        timeout_sec=timeout_sec,
    )
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text("ready\n", encoding="ascii")
    started_ticks = contract.qpc_ticks()
    try:
        second = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=runtime_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        if observer_process.poll() is None:
            observer_process.kill()
            observer_process.communicate(timeout=5)
        raise
    action_payload = {
        "schemaVersion": 1,
        "ok": True,
        "endpoint": "user_input_received",
        "source": "windows_second_instance_launch",
        "qpcTicks": started_ticks,
        "qpcFrequency": QPC_FREQUENCY,
        "processId": int(second.pid),
        "primaryProcessId": app_pid,
        "primaryProcessCreationTime100ns": app_created,
    }
    action_path = sample_dir / "user-action.json"
    _write_json(action_path, action_payload)
    observer = _wait_observer(
        observer_process, observer_path, sample_dir, timeout_sec
    )
    try:
        second.wait(timeout=5)
    except subprocess.TimeoutExpired:
        second.kill()
        second.wait(timeout=5)
        raise RuntimeError("second Scriber instance did not exit after activation signal")
    performance, performance_binding = _collect_frontend_performance_window(
        artifact_root,
        sample_dir,
        port,
        token,
        int(observer["stableQpcTicks"]),
        baseline=baseline,
        timeout_sec=timeout_sec,
    )
    observed_generation = _wait_generation(
        app_pid, backend_pid, port, token, timeout_sec
    )
    if not contract.process_generation_matches(generation, observed_generation):
        raise RuntimeError("warm activation replaced the primary process generation")
    return _sample_from_artifacts(
        artifact_root,
        sample_dir,
        scenario="warm_app_activation",
        iteration=iteration,
        run_id=run_id,
        installed_sha256=installed_sha256,
        harness_sha256=harness_sha256,
        generation=generation,
        action=action_payload,
        action_path=action_path,
        observer=observer,
        observer_path=observer_path,
        performance=performance,
        performance_binding=performance_binding,
    )


def _collect_uia_iteration(
    repo_root: Path,
    artifact_root: Path,
    executable: Path,
    *,
    iteration: int,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    timeout_sec: float,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    iteration_dir = artifact_root / "uia" / f"iteration-{iteration:02d}"
    data_dir = iteration_dir / "runtime-data"
    _seed_transcripts(data_dir)
    token = uuid.uuid4().hex
    port = _free_loopback_port()
    runtime_env = _runtime_environment(data_dir, token, port)
    app_process: subprocess.Popen[Any] | None = None
    backend_pid = 0
    samples: list[dict[str, Any]] = []
    try:
        cold, app_process, backend_pid, generation = _run_cold_launch(
            repo_root,
            artifact_root,
            iteration_dir / "cold_app_launch",
            executable,
            runtime_env,
            iteration=iteration,
            run_id=run_id,
            installed_sha256=installed_sha256,
            harness_sha256=harness_sha256,
            port=port,
            token=token,
            timeout_sec=timeout_sec,
        )
        samples.append(cold)
        app_pid = int(app_process.pid)
        app_created = int(generation["app"]["creationTime100ns"])
        samples.append(
            _run_warm_activation(
                repo_root,
                artifact_root,
                iteration_dir / "warm_app_activation",
                executable,
                runtime_env,
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                app_pid=app_pid,
                backend_pid=backend_pid,
                port=port,
                token=token,
                timeout_sec=timeout_sec,
            )
        )
        samples.append(
            _run_uia_sample(
                repo_root,
                artifact_root,
                iteration_dir / "open_settings",
                scenario="open_settings",
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                app_pid=app_pid,
                backend_pid=backend_pid,
                port=port,
                token=token,
                control_name="Settings",
                control_type="Hyperlink",
                expected_text="Configure capture",
                forbidden_text="Recent recordings",
                timeout_sec=timeout_sec,
            )
        )
        _navigate_unmeasured(
            repo_root,
            iteration_dir / "setup-live-mic",
            app_pid=app_pid,
            app_created=app_created,
            control_name="Live Mic",
            control_type="Hyperlink",
            expected_text="Recent recordings",
            timeout_sec=timeout_sec,
        )
        samples.append(
            _run_uia_sample(
                repo_root,
                artifact_root,
                iteration_dir / "open_transcript_detail",
                scenario="open_transcript_detail",
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                app_pid=app_pid,
                backend_pid=backend_pid,
                port=port,
                token=token,
                control_name=FIRST_TRANSCRIPT_TITLE,
                control_type="Button",
                expected_text=FIRST_TRANSCRIPT_TITLE,
                forbidden_text="Recent recordings",
                timeout_sec=timeout_sec,
            )
        )
        _navigate_unmeasured(
            repo_root,
            iteration_dir / "setup-switch-command-palette",
            app_pid=app_pid,
            app_created=app_created,
            control_name="Open command palette",
            control_type="Button",
            expected_text="Search Scriber",
            timeout_sec=timeout_sec,
        )
        samples.append(
            _run_uia_sample(
                repo_root,
                artifact_root,
                iteration_dir / "switch_between_transcripts",
                scenario="switch_between_transcripts",
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                app_pid=app_pid,
                backend_pid=backend_pid,
                port=port,
                token=token,
                control_name=SECOND_TRANSCRIPT_TITLE,
                control_type="ListItem",
                control_name_match="Prefix",
                expected_text=SECOND_TRANSCRIPT_TITLE,
                forbidden_text="Recent recordings",
                timeout_sec=timeout_sec,
            )
        )
        samples.append(
            _run_uia_sample(
                repo_root,
                artifact_root,
                iteration_dir / "return_to_dashboard",
                scenario="return_to_dashboard",
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                app_pid=app_pid,
                backend_pid=backend_pid,
                port=port,
                token=token,
                control_name="Go back",
                control_type="Button",
                expected_text="Recent recordings",
                forbidden_text="",
                timeout_sec=timeout_sec,
            )
        )
        resources = _idle_resource_observation(app_pid, backend_pid)
        final_generation = _wait_generation(
            app_pid, backend_pid, port, token, timeout_sec
        )
        if not contract.process_generation_matches(generation, final_generation):
            raise RuntimeError("UIA iteration ended in a different process generation")
        return samples, resources
    finally:
        if app_process is not None and backend_pid > 0:
            cleanup = contract.terminate_runtime(
                int(app_process.pid), backend_pid, port, token
            )
            _write_json(iteration_dir / "runtime-cleanup.json", cleanup)
        elif app_process is not None and app_process.poll() is None:
            app_process.terminate()
            try:
                app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app_process.kill()


def _copy_bound_artifact(
    artifact: dict[str, Any],
    source_root: Path,
    artifact_root: Path,
    destination_relative: str,
) -> dict[str, str]:
    source = contract._safe_artifact_path(source_root, artifact.get("path"))
    if source is None or not source.is_file():
        raise RuntimeError("validated lifecycle artifact disappeared before binding")
    destination = artifact_root / destination_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    binding = _artifact_binding(destination, artifact_root)
    if binding["sha256"] != artifact.get("sha256"):
        raise RuntimeError("lifecycle artifact changed while being copied")
    return binding


def _bind_lifecycle_import(
    evidence_path: Path,
    artifact_root: Path,
    *,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    samples_per_scenario: int,
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, str]]:
    source_root = evidence_path.parent.resolve()
    payload = contract.load_json(evidence_path)
    validation = contract.validate_app_ux_lifecycle_import(
        payload,
        artifact_root=source_root,
        required_samples_per_scenario=samples_per_scenario,
        expected_run_id=run_id,
        expected_installed_exe_sha256=installed_sha256,
        expected_harness_manifest_sha256=harness_sha256,
    )
    if not validation.get("metricEligible"):
        raise RuntimeError(
            "installed lifecycle evidence failed closed: "
            + ", ".join(validation.get("reasons") or ["unknown reason"])
        )
    bound_samples: list[dict[str, Any]] = []
    for sample in validation["results"]:
        bound = copy.deepcopy(sample)
        scenario = str(bound["scenario"])
        iteration = int(bound["iteration"])
        base = f"lifecycle-bound/{scenario}/iteration-{iteration:02d}"
        bound["processGeneration"]["artifact"] = _copy_bound_artifact(
            bound["processGeneration"]["artifact"],
            source_root,
            artifact_root,
            f"{base}/process-generation.json",
        )
        for field, name in (
            ("start", "start.json"),
            ("stableFrame", "stable-frame.json"),
            ("frontendPerformance", "frontend-performance.json"),
            ("eventEvidence", "installed-event.json"),
        ):
            bound[field]["artifact"] = _copy_bound_artifact(
                bound[field]["artifact"],
                source_root,
                artifact_root,
                f"{base}/{name}",
            )
        bound_samples.append(bound)
    bound_resource_evidence = copy.deepcopy(validation["resourceEvidence"])
    bound_resource_evidence["artifact"] = _copy_bound_artifact(
        bound_resource_evidence["artifact"],
        source_root,
        artifact_root,
        "lifecycle-bound/resource-evidence.json",
    )
    bound_import_payload = {
        "schemaVersion": 1,
        "contract": contract.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
        "runId": run_id,
        "installedExeSha256": installed_sha256,
        "harnessManifestSha256": harness_sha256,
        "samplesPerScenario": samples_per_scenario,
        "scenarioOrder": list(LIFECYCLE_SCENARIOS),
        "samples": bound_samples,
        "resourceEvidence": bound_resource_evidence,
    }
    imported_copy = artifact_root / "lifecycle-bound" / "lifecycle-import.json"
    _write_json(imported_copy, bound_import_payload)
    import_binding = _artifact_binding(imported_copy, artifact_root)
    rebound_validation = contract.validate_app_ux_lifecycle_import(
        bound_import_payload,
        artifact_root=artifact_root,
        required_samples_per_scenario=samples_per_scenario,
        expected_run_id=run_id,
        expected_installed_exe_sha256=installed_sha256,
        expected_harness_manifest_sha256=harness_sha256,
    )
    if not rebound_validation.get("metricEligible"):
        raise RuntimeError(
            "copied lifecycle import lost its evidence binding: "
            + ", ".join(rebound_validation.get("reasons") or ["unknown reason"])
        )
    resources = validation["resourceMetrics"]
    return (
        bound_samples,
        {
            "idleCpuPercent": float(resources["idleCpuPercent"]),
            "workingSetMb": float(resources["workingSetMb"]),
        },
        import_binding,
    )


def _write_lifecycle_request(
    path: Path,
    *,
    run_id: str,
    installed_sha256: str,
    harness_sha256: str,
    samples_per_scenario: int,
    repo_root: Path,
) -> None:
    schema_path = repo_root / "benchmarks" / "windows" / "app_ux_lifecycle_import.schema.json"
    _write_json(
        path,
        {
            "schemaVersion": 1,
            "contract": LIFECYCLE_REQUEST_CONTRACT,
            "runId": run_id,
            "installedExeSha256": installed_sha256,
            "harnessManifestSha256": harness_sha256,
            "samplesPerScenario": samples_per_scenario,
            "scenarioOrder": list(LIFECYCLE_SCENARIOS),
            "requiredImportContract": contract.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
            "importSchema": {
                "path": "benchmarks/windows/app_ux_lifecycle_import.schema.json",
                "sha256": contract.sha256_file(schema_path),
            },
            "semanticValidator": (
                "benchmarks.windows.endpoint_probe.validate_app_ux_lifecycle_import"
            ),
            "rules": [
                "Use the installed production runtime and its real WebSocket/state/provider events.",
                "Invoke Stop through Windows UI Automation for stop_to_transcribing_visible.",
                "Bind every event to runId, sampleId, sessionId, process generation, and QPC.",
                "Observe first_stable_visible_frame externally through Windows UI Automation.",
                "Acknowledge a post-frame Long Tasks flush for every sample.",
                "Do not synthesize, infer, or relabel provider/session markers.",
            ],
        },
    )


def _existing_request_run_id(path: Path) -> str | None:
    payload = contract.load_json(path)
    if payload.get("contract") != LIFECYCLE_REQUEST_CONTRACT:
        return None
    try:
        return _canonical_uuid(str(payload.get("runId") or ""))
    except (ValueError, AttributeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect the complete installed B7 App UX scenario matrix."
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--samples-per-scenario", type=int, default=20)
    parser.add_argument("--lifecycle-evidence", default="")
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--uia-only",
        action="store_true",
        help="Diagnostic only: collect six UIA scenarios and emit an ineligible partial package.",
    )
    args = parser.parse_args(argv)

    if os.name != "nt":
        raise SystemExit("app_ux_collector requires Windows")
    if args.samples_per_scenario < 1:
        raise SystemExit("--samples-per-scenario must be positive")
    repo_root = Path(args.repo_root).resolve()
    install_root = (
        Path(args.install_root).resolve()
        if args.install_root
        else repo_root / "Scriber Install"
    )
    executable = install_root / "scriber-desktop.exe"
    if not executable.is_file():
        raise SystemExit(f"missing installed production executable: {executable}")
    output_path = Path(args.output).resolve()
    artifact_root = output_path.parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    request_path = artifact_root / "app-ux-lifecycle-request.json"
    run_id = _canonical_uuid(
        args.run_id or _existing_request_run_id(request_path)
    )
    installed_sha256 = contract.sha256_file(executable)
    harness_sha256 = contract.app_ux_harness_manifest_sha256(repo_root)
    _write_lifecycle_request(
        request_path,
        run_id=run_id,
        installed_sha256=installed_sha256,
        harness_sha256=harness_sha256,
        samples_per_scenario=args.samples_per_scenario,
        repo_root=repo_root,
    )
    if args.prepare_only:
        print(json.dumps(contract.load_json(request_path), ensure_ascii=False))
        return 0
    if not args.lifecycle_evidence and not args.uia_only:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "installed_lifecycle_evidence_required",
                    "requestPath": str(request_path),
                }
            )
        )
        return 2

    uia_root = artifact_root / "uia"
    lifecycle_bound_root = artifact_root / "lifecycle-bound"
    for target in (uia_root, lifecycle_bound_root):
        if target.exists():
            shutil.rmtree(target)

    lifecycle_samples: list[dict[str, Any]] = []
    lifecycle_resources: dict[str, float] | None = None
    lifecycle_import_binding: dict[str, str] | None = None
    if args.lifecycle_evidence:
        lifecycle_samples, lifecycle_resources, lifecycle_import_binding = (
            _bind_lifecycle_import(
                Path(args.lifecycle_evidence).resolve(),
                artifact_root,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                samples_per_scenario=args.samples_per_scenario,
            )
        )

    uia_samples: list[dict[str, Any]] = []
    uia_resources: list[dict[str, float]] = []
    failures: list[dict[str, Any]] = []
    for iteration in range(1, args.samples_per_scenario + 1):
        try:
            samples, resources = _collect_uia_iteration(
                repo_root,
                artifact_root,
                executable,
                iteration=iteration,
                run_id=run_id,
                installed_sha256=installed_sha256,
                harness_sha256=harness_sha256,
                timeout_sec=args.timeout_sec,
            )
            uia_samples.extend(samples)
            uia_resources.append(resources)
        except Exception as exc:
            failures.append({"iteration": iteration, "error": str(exc)})
            break

    all_samples = [*uia_samples, *lifecycle_samples]
    scenario_index = {name: index for index, name in enumerate(contract.APP_UX_SCENARIOS)}
    all_samples.sort(key=lambda item: (scenario_index[str(item["scenario"])], int(item["iteration"])))
    resource_candidates = list(uia_resources)
    if lifecycle_resources is not None:
        resource_candidates.append(lifecycle_resources)
    idle_cpu = max(
        (float(item["idleCpuPercent"]) for item in resource_candidates),
        default=-1.0,
    )
    working_set = max(
        (float(item["workingSetMb"]) for item in resource_candidates),
        default=-1.0,
    )
    resource_payload = {
        "runId": run_id,
        "installedExeSha256": installed_sha256,
        "harnessManifestSha256": harness_sha256,
        "sampleCount": len(all_samples),
        "idleCpuPercent": idle_cpu,
        "workingSetMb": working_set,
    }
    resource_binding = _write_artifact(
        artifact_root,
        "resources/app-ux-resource-evidence.json",
        resource_payload,
    )
    package: dict[str, Any] = {
        "schemaVersion": 1,
        "contract": contract.APP_UX_EVIDENCE_CONTRACT,
        "runId": run_id,
        "installedExeSha256": installed_sha256,
        "harnessManifestSha256": harness_sha256,
        "samplesPerScenario": args.samples_per_scenario,
        "samples": all_samples,
        "resourceEvidence": {**resource_payload, "artifact": resource_binding},
        "collector": {
            "uiaScenarioOrder": list(UIA_SCENARIOS),
            "lifecycleScenarioOrder": list(LIFECYCLE_SCENARIOS),
            "uiaIterationCount": len(uia_resources),
            "lifecycleImport": lifecycle_import_binding,
            "failures": failures,
            "diagnosticUiaOnly": bool(args.uia_only),
        },
    }
    _write_json(output_path, package)
    validation = contract.validate_app_ux_evidence(
        package,
        artifact_root=artifact_root,
        required_samples_per_scenario=args.samples_per_scenario,
        expected_installed_exe_sha256=installed_sha256,
        expected_harness_manifest_sha256=harness_sha256,
    )
    validation_path = artifact_root / "app-ux-validation.json"
    _write_json(validation_path, validation)
    print(json.dumps(validation, ensure_ascii=False))
    return 0 if validation.get("metricEligible") else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Collect installed provider-owned lifecycle evidence for B7 App UX.

This collector consumes the request emitted by ``app_ux_collector.py
--prepare-only``.  Every sample uses the installed Tauri application, the
private provider-replay gate, a real UI Automation stable-frame observation,
the authenticated frontend Long Tasks flush, and a point-in-time process-tree
attestation.

The runtime marker is retained verbatim in ``installedEvent``.  Its narrower
provider-replay process fingerprint is independently recomputed from the same
app/backend generation.  The surrounding evidence envelope then binds that
unchanged event to the fuller App-UX generation, which additionally includes
the WebView processes and frontend-ready timestamp.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import app_ux_collector as ux
    from . import endpoint_probe as contract
except ImportError:  # Direct script execution from benchmarks/windows.
    import app_ux_collector as ux  # type: ignore[no-redef]
    import endpoint_probe as contract  # type: ignore[no-redef]


LIFECYCLE_REQUEST_CONTRACT = "b7-app-ux-lifecycle-request-v1"
PROVIDER = "microsoft"
FIXTURE_DURATION_MS = 5_000
MANUAL_STOP_ENV = "SCRIBER_B7_PROVIDER_REPLAY_MANUAL_STOP"
LIFECYCLE_SCENARIOS = tuple(
    scenario for scenario in contract.APP_UX_SCENARIOS if scenario in contract.APP_UX_LIFECYCLE_SCENARIOS
)
EVENT_MARKERS = {
    "stop_to_transcribing_visible": "recording_state_transcribing_emitted",
    "provider_result_to_completed_visible": "provider_response_complete",
    "session_finished_to_history_visible": "session_finished_emitted",
}
EVENT_SOURCES = {
    "stop_to_transcribing_visible": "installed_backend_state_event",
    "provider_result_to_completed_visible": "installed_backend_provider_event",
    "session_finished_to_history_visible": "installed_backend_session_event",
}
EXPECTED_VISIBLE_TEXT_KEY = {
    "stop_to_transcribing_visible": "transcribing",
    "session_finished_to_history_visible": "saved_to_recent_recordings",
}


@dataclass(frozen=True)
class LifecycleRequest:
    run_id: str
    installed_exe_sha256: str
    harness_manifest_sha256: str
    samples_per_scenario: int


@dataclass
class InstalledRuntime:
    app_pid: int
    backend_pid: int
    port: int
    token: str
    generation: dict[str, Any]
    scenario_dir: Path
    data_dir: Path


def _canonical_uuid(value: Any, *, label: str) -> str:
    try:
        parsed = uuid.UUID(str(value or ""))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is not a UUID") from exc
    if parsed.int == 0:
        raise RuntimeError(f"{label} must not be nil")
    return parsed.hex


def _require_sha256(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise RuntimeError(f"{label} is not a SHA-256")
    return normalized


def load_and_validate_request(
    request_path: Path,
    *,
    repo_root: Path,
    install_root: Path,
) -> LifecycleRequest:
    payload = contract.load_json(request_path)
    if payload.get("schemaVersion") != 1:
        raise RuntimeError("lifecycle request schemaVersion is invalid")
    if payload.get("contract") != LIFECYCLE_REQUEST_CONTRACT:
        raise RuntimeError("lifecycle request contract is invalid")
    if payload.get("requiredImportContract") != contract.APP_UX_LIFECYCLE_IMPORT_CONTRACT:
        raise RuntimeError("lifecycle request import contract is invalid")
    if payload.get("scenarioOrder") != list(LIFECYCLE_SCENARIOS):
        raise RuntimeError("lifecycle request scenario order is invalid")
    if payload.get("semanticValidator") != ("benchmarks.windows.endpoint_probe.validate_app_ux_lifecycle_import"):
        raise RuntimeError("lifecycle request semantic validator is invalid")

    executable = install_root / "scriber-desktop.exe"
    if not executable.is_file():
        raise RuntimeError(f"installed production executable is missing: {executable}")
    installed_sha256 = _require_sha256(
        payload.get("installedExeSha256"),
        label="lifecycle request installed executable hash",
    )
    if installed_sha256 != contract.sha256_file(executable):
        raise RuntimeError("lifecycle request installed executable hash drifted")

    harness_sha256 = _require_sha256(
        payload.get("harnessManifestSha256"),
        label="lifecycle request harness hash",
    )
    if harness_sha256 != contract.app_ux_harness_manifest_sha256(repo_root):
        raise RuntimeError("lifecycle request harness hash drifted")

    import_schema = payload.get("importSchema") if isinstance(payload.get("importSchema"), dict) else {}
    expected_schema_relative = "benchmarks/windows/app_ux_lifecycle_import.schema.json"
    if import_schema.get("path") != expected_schema_relative:
        raise RuntimeError("lifecycle request schema path is invalid")
    schema_path = repo_root / expected_schema_relative
    if not schema_path.is_file():
        raise RuntimeError("lifecycle import schema is missing")
    schema_sha256 = _require_sha256(
        import_schema.get("sha256"),
        label="lifecycle request schema hash",
    )
    if schema_sha256 != contract.sha256_file(schema_path):
        raise RuntimeError("lifecycle request schema hash drifted")

    samples_per_scenario = payload.get("samplesPerScenario")
    if (
        not isinstance(samples_per_scenario, int)
        or isinstance(samples_per_scenario, bool)
        or not 1 <= samples_per_scenario <= 100
    ):
        raise RuntimeError("lifecycle request sample count is invalid")
    return LifecycleRequest(
        run_id=_canonical_uuid(payload.get("runId"), label="lifecycle request runId"),
        installed_exe_sha256=installed_sha256,
        harness_manifest_sha256=harness_sha256,
        samples_per_scenario=samples_per_scenario,
    )


def bind_installed_event(
    raw_marker: dict[str, Any],
    *,
    generation: dict[str, Any],
    run_id: str,
    sample_id: str,
    scenario: str,
    manual_stop_ready: dict[str, Any] | None = None,
    manual_stop_request: dict[str, Any] | None = None,
    stop_start_qpc_ticks: int | None = None,
) -> dict[str, Any]:
    """Validate an unchanged runtime marker and bind it to App-UX generation."""

    expected_marker = EVENT_MARKERS.get(scenario)
    expected_source = EVENT_SOURCES.get(scenario)
    if expected_marker is None or expected_source is None:
        raise RuntimeError("lifecycle scenario is unsupported")
    if raw_marker.get("ok") is not True:
        raise RuntimeError("installed lifecycle marker is not successful")
    if raw_marker.get("marker") != expected_marker:
        raise RuntimeError("installed lifecycle marker identity is invalid")
    if raw_marker.get("source") != expected_source:
        raise RuntimeError("installed lifecycle marker source is invalid")
    if _canonical_uuid(raw_marker.get("runId"), label="installed marker runId") != run_id:
        raise RuntimeError("installed lifecycle marker runId mismatch")
    if _canonical_uuid(raw_marker.get("sampleId"), label="installed marker sampleId") != sample_id:
        raise RuntimeError("installed lifecycle marker sampleId mismatch")
    session_id = _canonical_uuid(
        raw_marker.get("sessionId"),
        label="installed marker sessionId",
    )
    api_version = raw_marker.get("apiVersion")
    qpc_ticks = raw_marker.get("qpcTicks")
    qpc_frequency = raw_marker.get("qpcFrequency")
    if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
        raise RuntimeError("installed lifecycle marker API version is invalid")
    if (
        not isinstance(qpc_ticks, int)
        or isinstance(qpc_ticks, bool)
        or qpc_ticks <= 0
        or not isinstance(qpc_frequency, int)
        or isinstance(qpc_frequency, bool)
        or qpc_frequency <= 0
    ):
        raise RuntimeError("installed lifecycle marker QPC is invalid")

    if generation.get("ok") is not True:
        raise RuntimeError("App-UX process generation is unavailable")
    full_fingerprint = _require_sha256(
        generation.get("fingerprint"),
        label="App-UX process generation fingerprint",
    )
    if contract.process_generation_fingerprint(generation) != full_fingerprint:
        raise RuntimeError("App-UX process generation fingerprint drifted")
    expected_replay_fingerprint = contract.provider_replay_process_generation_sha256(generation)
    raw_replay_fingerprint = _require_sha256(
        raw_marker.get("processGenerationFingerprint"),
        label="installed marker replay process fingerprint",
    )
    if not expected_replay_fingerprint or raw_replay_fingerprint != expected_replay_fingerprint:
        raise RuntimeError("installed marker replay process generation mismatch")

    installed_event = copy.deepcopy(raw_marker)
    envelope = {
        "ok": True,
        "endpoint": expected_marker,
        "marker": expected_marker,
        "source": expected_source,
        "qpcTicks": qpc_ticks,
        "qpcFrequency": qpc_frequency,
        "apiVersion": api_version,
        "runId": run_id,
        "sampleId": sample_id,
        "sessionId": session_id,
        "processGenerationFingerprint": full_fingerprint,
        "providerReplayProcessGenerationFingerprint": raw_replay_fingerprint,
        "installedEvent": installed_event,
    }
    manual_artifacts = (
        ("manualStopReadyAttestation", manual_stop_ready),
        ("manualStopRequest", manual_stop_request),
    )
    if scenario == "stop_to_transcribing_visible":
        if (
            not isinstance(stop_start_qpc_ticks, int)
            or isinstance(stop_start_qpc_ticks, bool)
            or stop_start_qpc_ticks <= 0
        ):
            raise RuntimeError("manual Stop UIA start QPC is invalid")
        expected_sources = {
            "manualStopReadyAttestation": "rust_audio_frame_pipe_fixture_consumed",
            "manualStopRequest": "uia_stop_request",
        }
        validated_manual: dict[str, dict[str, Any]] = {}
        for field_name, artifact in manual_artifacts:
            if not isinstance(artifact, dict):
                raise RuntimeError(f"{field_name} is missing")
            if (
                artifact.get("source") != expected_sources[field_name]
                or _canonical_uuid(
                    artifact.get("runId"),
                    label=f"{field_name} runId",
                )
                != run_id
                or _canonical_uuid(
                    artifact.get("sampleId"),
                    label=f"{field_name} sampleId",
                )
                != sample_id
                or _canonical_uuid(
                    artifact.get("sessionId"),
                    label=f"{field_name} sessionId",
                )
                != session_id
                or artifact.get("processGenerationFingerprint") != raw_replay_fingerprint
                or not isinstance(artifact.get("qpcTicks"), int)
                or isinstance(artifact.get("qpcTicks"), bool)
                or artifact.get("qpcTicks", 0) <= 0
                or artifact.get("qpcFrequency") != qpc_frequency
            ):
                raise RuntimeError(f"{field_name} binding is invalid")
            validated_manual[field_name] = copy.deepcopy(artifact)
        ready_ticks = int(validated_manual["manualStopReadyAttestation"]["qpcTicks"])
        request_ticks = int(validated_manual["manualStopRequest"]["qpcTicks"])
        if not (ready_ticks <= stop_start_qpc_ticks <= request_ticks <= qpc_ticks):
            raise RuntimeError("manual Stop QPC ordering is invalid")
        envelope.update(validated_manual)
    elif any(artifact is not None for _field, artifact in manual_artifacts):
        raise RuntimeError("manual Stop artifacts appeared in a non-Stop sample")
    return envelope


def _runtime_environment(
    *,
    request: LifecycleRequest,
    fixture_path: Path,
    fixture_sha256: str,
    manual_stop: bool,
) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "SCRIBER_REPO_ROOT",
        "SCRIBER_PYTHON",
        "SCRIBER_BACKEND_EXE",
        "SCRIBER_RUNTIME_MODE",
        "SCRIBER_BACKEND_LAUNCH_KIND",
        "SCRIBER_LEGACY_DATA_DIR",
        MANUAL_STOP_ENV,
    ):
        environment.pop(name, None)
    environment.update(
        {
            "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID": request.run_id,
            "SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID": request.run_id,
            "SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_DURATION_MS": str(FIXTURE_DURATION_MS),
            "SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_PCM_SHA256": fixture_sha256,
            "SCRIBER_RUST_AUDIO_SYNTHETIC_SIGNAL": "1",
            "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH": str(fixture_path),
            "SCRIBER_MIC_ALWAYS_ON": "0",
            "SCRIBER_MIC_DEVICE": "default",
            "SCRIBER_FAVORITE_MIC": "",
            "SCRIBER_DISABLE_TEXT_INJECTION": "0",
            "SCRIBER_TAURI_GLOBAL_HOTKEY": "1",
            "SCRIBER_HOTKEY": "ctrl+alt+shift+f12",
            "SCRIBER_AUTO_SUMMARIZE": "0",
            "SCRIBER_AZURE_MAI_REGION": "northeurope",
            "SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3": "0",
            "SCRIBER_SPEECHMATICS_CAPTURE_TIME_WAV": "0",
            "AZURE_MAI_SPEECH_KEY": "",
            "SPEECHMATICS_API_KEY": "",
        }
    )
    if manual_stop:
        environment[MANUAL_STOP_ENV] = "1"
    return environment


def _launch_runtime(
    repo_root: Path,
    install_root: Path,
    scenario_dir: Path,
    *,
    request: LifecycleRequest,
    fixture_path: Path,
    fixture_sha256: str,
    manual_stop: bool,
    timeout_sec: float,
) -> InstalledRuntime:
    smoke_dir = repo_root / "tmp" / "app-ux-lifecycle-smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_path = smoke_dir / f"{request.run_id}-{uuid.uuid4().hex}.json"
    data_dir = scenario_dir / "runtime-data"
    token = uuid.uuid4().hex
    environment = _runtime_environment(
        request=request,
        fixture_path=fixture_path,
        fixture_sha256=fixture_sha256,
        manual_stop=manual_stop,
    )
    try:
        smoke_result = contract.run_capture(
            contract.smoke_args(
                repo_root,
                install_root,
                data_dir,
                smoke_path,
                extra=[
                    "-KeepAppOpen",
                    "-OccupyDefaultPort",
                    "-VerifyFrontend",
                    "-LiveRecordingAudioEngine",
                    "rust-wasapi",
                    "-LiveRecordingRustAudioCaptureMode",
                    "synthetic",
                    "-SessionToken",
                    token,
                ],
                timeout_sec=max(45, int(timeout_sec)),
            ),
            cwd=repo_root,
            timeout=max(90, int(timeout_sec) + 60),
            env=environment,
        )
        smoke = contract.load_json(smoke_path)
    finally:
        smoke_path.unlink(missing_ok=True)
    app_pid = int(smoke.get("appPid") or 0)
    backend_pid = int(smoke.get("backendPid") or 0)
    port = int(smoke.get("backendPort") or 0)
    if smoke_result.returncode != 0 or smoke.get("ok") is not True or app_pid <= 0 or backend_pid <= 0 or port <= 0:
        cleanup: dict[str, Any] | None = None
        if app_pid > 0 and backend_pid > 0 and port > 0:
            cleanup = contract.terminate_runtime(
                app_pid,
                backend_pid,
                port,
                token,
            )
            ux._write_json(scenario_dir / "runtime-startup-cleanup.json", cleanup)
        detail = str(smoke.get("error") or smoke_result.stderr or "").strip()
        if len(detail) > 500:
            detail = detail[-500:]
        raise RuntimeError(
            f"installed lifecycle runtime failed to start (smoke exit {smoke_result.returncode}): {detail}"
        )
    try:
        generation = ux._wait_generation(
            app_pid,
            backend_pid,
            port,
            token,
            timeout_sec,
        )
    except Exception:
        cleanup = contract.terminate_runtime(
            app_pid,
            backend_pid,
            port,
            token,
        )
        ux._write_json(scenario_dir / "runtime-startup-cleanup.json", cleanup)
        resolved_data = data_dir.resolve()
        resolved_scenario = scenario_dir.resolve()
        if resolved_data != resolved_scenario and resolved_scenario in resolved_data.parents and resolved_data.exists():
            shutil.rmtree(resolved_data)
        raise
    return InstalledRuntime(
        app_pid=app_pid,
        backend_pid=backend_pid,
        port=port,
        token=token,
        generation=generation,
        scenario_dir=scenario_dir,
        data_dir=data_dir,
    )


def _focus_installed_app(runtime: InstalledRuntime, timeout_sec: float) -> None:
    if os.name != "nt":
        raise RuntimeError("installed lifecycle UI focus requires Windows")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    deadline = time.monotonic() + max(0.5, timeout_sec)
    while time.monotonic() < deadline:
        hwnd = contract.find_window("Scriber")
        if hwnd and contract.window_process_id(hwnd) == runtime.app_pid:
            current_thread = int(kernel32.GetCurrentThreadId())
            target_thread = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None))
            foreground = int(user32.GetForegroundWindow() or 0)
            foreground_thread = (
                int(
                    user32.GetWindowThreadProcessId(
                        wintypes.HWND(foreground),
                        None,
                    )
                )
                if foreground
                else 0
            )
            attached: list[int] = []
            for thread_id in {target_thread, foreground_thread}:
                if (
                    thread_id
                    and thread_id != current_thread
                    and user32.AttachThreadInput(current_thread, thread_id, True)
                ):
                    attached.append(thread_id)
            try:
                user32.ShowWindow(wintypes.HWND(hwnd), 9)
                user32.BringWindowToTop(wintypes.HWND(hwnd))
                user32.SetActiveWindow(wintypes.HWND(hwnd))
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
            finally:
                for thread_id in attached:
                    user32.AttachThreadInput(current_thread, thread_id, False)
            foreground_snapshot = contract.foreground_window_snapshot()
            if (
                foreground_snapshot.get("ok") is True
                and foreground_snapshot.get("pid") == runtime.app_pid
                and foreground_snapshot.get("processCreationTime100ns")
                == runtime.generation["app"]["creationTime100ns"]
            ):
                return
        time.sleep(0.05)
    raise RuntimeError("installed Scriber window could not be focused")


def _prepare_and_arm(
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    manual_stop: bool,
    timeout_sec: float,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    prepare_payload: dict[str, Any] = {
        "schemaVersion": contract.PROVIDER_REPLAY_CONTRACT_VERSION,
        "runId": request.run_id,
        "provider": PROVIDER,
    }
    if manual_stop:
        prepare_payload["manualStopRequired"] = True
    prepared = contract.request_runtime_json(
        runtime.port,
        runtime.token,
        f"{contract.PROVIDER_REPLAY_ROUTE}/prepare",
        method="POST",
        payload=prepare_payload,
        timeout_sec=max(15.0, timeout_sec),
    )
    sample_id = _canonical_uuid(
        prepared.get("sampleId"),
        label="prepared provider replay sampleId",
    )
    expected_replay_fingerprint = contract.provider_replay_process_generation_sha256(runtime.generation)
    if (
        prepared.get("state") != "prepared"
        or prepared.get("provider") != PROVIDER
        or _canonical_uuid(prepared.get("runId"), label="prepared runId") != request.run_id
        or prepared.get("authoritativeFixtureDurationMs") != FIXTURE_DURATION_MS
        or prepared.get("processGenerationFingerprint") != expected_replay_fingerprint
        or prepared.get("manualStopRequired") is not manual_stop
    ):
        raise RuntimeError("provider replay prepare contract is invalid")

    _focus_installed_app(runtime, min(5.0, timeout_sec))
    app_created = int(runtime.generation["app"]["creationTime100ns"])
    armed = contract.request_runtime_json(
        runtime.port,
        runtime.token,
        f"{contract.PROVIDER_REPLAY_ROUTE}/{sample_id}/arm",
        method="POST",
        payload={
            "schemaVersion": contract.PROVIDER_REPLAY_CONTRACT_VERSION,
            "runId": request.run_id,
            "activationKind": "button",
            "targetProcessId": runtime.app_pid,
            "targetCreationTime100ns": app_created,
        },
        timeout_sec=max(10.0, timeout_sec),
    )
    if (
        armed.get("state") != "activation_armed"
        or armed.get("activationKind") != "button"
        or armed.get("sessionId") is not None
        or armed.get("manualStopRequired") is not manual_stop
    ):
        raise RuntimeError("provider replay arm contract is invalid")
    return sample_id, prepared, armed


def _activate(
    repo_root: Path,
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    sample_id: str,
    manual_stop: bool,
    timeout_sec: float,
) -> dict[str, Any]:
    activation = contract.trigger_provider_replay_activation(
        activation_kind="button",
        repo_root=repo_root,
        app_pid=runtime.app_pid,
        app_creation_time_100ns=int(runtime.generation["app"]["creationTime100ns"]),
        iteration_dir=runtime.scenario_dir,
    )
    if activation.get("ok") is not True:
        raise RuntimeError("installed provider replay activation failed")
    status = _wait_status(
        runtime,
        request=request,
        sample_id=sample_id,
        timeout_sec=timeout_sec,
        predicate=lambda value: (
            value.get("state") == "armed" and bool(contract._canonical_non_nil_uuid(value.get("sessionId")))
        ),
    )
    if status.get("manualStopRequired") is not manual_stop:
        raise RuntimeError("provider replay manual-stop status drifted")
    return status


def _wait_manual_stop_ready(
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    sample_id: str,
    timeout_sec: float,
) -> dict[str, Any]:
    expected_replay_fingerprint = contract.provider_replay_process_generation_sha256(runtime.generation)
    status = _wait_status(
        runtime,
        request=request,
        sample_id=sample_id,
        timeout_sec=timeout_sec,
        predicate=lambda value: value.get("manualStopReady") is True,
    )
    ready = (
        status.get("manualStopReadyAttestation") if isinstance(status.get("manualStopReadyAttestation"), dict) else {}
    )
    if (
        status.get("manualStopRequired") is not True
        or status.get("manualStopRequested") is not False
        or status.get("manualStopCompleted") is not False
        or ready.get("source") != "rust_audio_frame_pipe_fixture_consumed"
        or ready.get("runId") != request.run_id
        or ready.get("sampleId") != sample_id
        or ready.get("sessionId") != status.get("sessionId")
        or ready.get("processGenerationFingerprint") != expected_replay_fingerprint
        or not isinstance(ready.get("qpcTicks"), int)
        or isinstance(ready.get("qpcTicks"), bool)
        or ready.get("qpcTicks", 0) <= 0
        or not isinstance(ready.get("qpcFrequency"), int)
        or isinstance(ready.get("qpcFrequency"), bool)
        or ready.get("qpcFrequency", 0) <= 0
    ):
        raise RuntimeError("provider replay manual Stop readiness is invalid")
    return copy.deepcopy(ready)


def _wait_status(
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    sample_id: str,
    timeout_sec: float,
    predicate: Any,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.5, timeout_sec)
    query = urllib.parse.urlencode({"runId": request.run_id})
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = contract.request_runtime_json(
            runtime.port,
            runtime.token,
            f"{contract.PROVIDER_REPLAY_ROUTE}/{sample_id}?{query}",
            timeout_sec=3.0,
        )
        if predicate(last):
            return last
        if last.get("state") in {"failed", "expired"}:
            raise RuntimeError(
                "provider replay failed before required lifecycle evidence "
                f"({last.get('errorCode') or last.get('state')})"
            )
        time.sleep(0.02)
    raise RuntimeError("provider replay lifecycle status timed out")


def _wait_marker(
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    sample_id: str,
    scenario: str,
    timeout_sec: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    marker_name = EVENT_MARKERS[scenario]

    def marker_available(status: dict[str, Any]) -> bool:
        markers = status.get("markers")
        return isinstance(markers, list) and any(
            isinstance(item, dict) and item.get("marker") == marker_name for item in markers
        )

    status = _wait_status(
        runtime,
        request=request,
        sample_id=sample_id,
        timeout_sec=timeout_sec,
        predicate=marker_available,
    )
    matches = [
        item for item in status.get("markers", []) if isinstance(item, dict) and item.get("marker") == marker_name
    ]
    if len(matches) != 1:
        raise RuntimeError("installed lifecycle marker is missing or duplicated")
    return copy.deepcopy(matches[0]), status


def _wait_recording(
    runtime: InstalledRuntime,
    *,
    request: LifecycleRequest,
    sample_id: str,
    timeout_sec: float,
) -> None:
    status = _wait_status(
        runtime,
        request=request,
        sample_id=sample_id,
        timeout_sec=timeout_sec,
        predicate=lambda value: any(
            isinstance(item, dict) and item.get("marker") == "recording_state_visible"
            for item in (value.get("markers") or [])
        ),
    )
    if status.get("state") != "armed":
        raise RuntimeError("provider replay did not remain armed for UIA Stop")
    state = contract.wait_runtime_state(
        runtime.port,
        runtime.token,
        lambda value: value.get("recordingState") == "recording" and value.get("listening") is True,
        timeout_sec,
    )
    if state.get("recordingState") != "recording" or state.get("listening") is not True:
        raise RuntimeError("installed Live Mic never reached recording state")


def _run_stop_action(
    repo_root: Path,
    runtime: InstalledRuntime,
    sample_dir: Path,
    *,
    timeout_sec: float,
) -> tuple[dict[str, Any], Path]:
    output_path = sample_dir / "user-action.json"
    process = ux._script_process(
        repo_root / "benchmarks" / "windows" / "app_action.ps1",
        [
            "-ProcessId",
            str(runtime.app_pid),
            "-ProcessCreationTime100ns",
            str(runtime.generation["app"]["creationTime100ns"]),
            "-WindowTitle",
            "Scriber",
            "-ControlAutomationId",
            "live-mic-toggle-button",
            "-ControlType",
            "Button",
            "-TimeoutSec",
            str(max(1.0, timeout_sec)),
            "-OutputPath",
            str(output_path),
        ],
        cwd=repo_root,
    )
    exit_code, _, stderr = ux._wait_script(
        process,
        timeout_sec + 5,
        sample_dir / "user-action.stdout.txt",
        sample_dir / "user-action.stderr.txt",
    )
    action = contract.load_json(output_path)
    if (
        exit_code != 0
        or action.get("ok") is not True
        or action.get("endpoint") != "user_input_received"
        or action.get("source") != "uia_invoke"
    ):
        raise RuntimeError(f"installed UIA Stop failed (exit {exit_code}): {stderr[-300:]}")
    return action, output_path


def _sample_from_installed_evidence(
    artifact_root: Path,
    sample_dir: Path,
    *,
    request: LifecycleRequest,
    scenario: str,
    iteration: int,
    generation: dict[str, Any],
    event: dict[str, Any],
    start: dict[str, Any],
    start_path: Path | None,
    observer: dict[str, Any],
    observer_path: Path,
    performance: dict[str, Any],
    performance_binding: dict[str, str],
) -> dict[str, Any]:
    fingerprint = str(generation["fingerprint"])
    event_binding = ux._write_artifact(
        artifact_root,
        (sample_dir / "installed-event.json").relative_to(artifact_root).as_posix(),
        event,
    )
    generation_binding = ux._write_artifact(
        artifact_root,
        (sample_dir / "process-generation.json").relative_to(artifact_root).as_posix(),
        generation,
    )
    start_binding = ux._artifact_binding(start_path, artifact_root) if start_path is not None else event_binding
    start_ticks = int(start["qpcTicks"])
    end_ticks = int(observer["stableQpcTicks"])
    duration = contract.duration_ms(
        start_ticks,
        end_ticks,
        int(start["qpcFrequency"]),
    )
    if duration is None:
        raise RuntimeError("installed lifecycle duration is unavailable")
    return {
        "scenario": scenario,
        "iteration": iteration,
        "runId": request.run_id,
        "sampleId": str(event["sampleId"]),
        "installedExeSha256": request.installed_exe_sha256,
        "harnessManifestSha256": request.harness_manifest_sha256,
        "processGenerationFingerprint": fingerprint,
        "processGeneration": {
            "fingerprint": fingerprint,
            "artifact": generation_binding,
        },
        "start": {
            "marker": str(start["endpoint"]),
            "source": str(start["source"]),
            "qpcTicks": start_ticks,
            "qpcFrequency": int(start["qpcFrequency"]),
            "processGenerationFingerprint": fingerprint,
            "artifact": start_binding,
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
            "artifact": ux._artifact_binding(observer_path, artifact_root),
        },
        "durationMs": duration,
        "frontendPerformance": {
            **performance,
            "artifact": performance_binding,
        },
        "eventEvidence": {
            "installedRuntime": True,
            "apiVersion": int(event["apiVersion"]),
            "runId": request.run_id,
            "sampleId": str(event["sampleId"]),
            "sessionId": str(event["sessionId"]),
            "source": str(event["source"]),
            "marker": str(event["marker"]),
            "qpcTicks": int(event["qpcTicks"]),
            "processGenerationFingerprint": fingerprint,
            "providerReplayProcessGenerationFingerprint": str(event["providerReplayProcessGenerationFingerprint"]),
            "artifact": event_binding,
        },
    }


def _collect_scenario(
    repo_root: Path,
    install_root: Path,
    artifact_root: Path,
    *,
    request: LifecycleRequest,
    scenario: str,
    iteration: int,
    fixture_path: Path,
    fixture_sha256: str,
    timeout_sec: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    sample_dir = artifact_root / "lifecycle-source" / scenario / f"iteration-{iteration:02d}"
    sample_dir.mkdir(parents=True, exist_ok=False)
    manual_stop = scenario == "stop_to_transcribing_visible"
    runtime: InstalledRuntime | None = None
    observer_process: subprocess.Popen[str] | None = None
    manual_stop_ready: dict[str, Any] | None = None
    try:
        runtime = _launch_runtime(
            repo_root,
            install_root,
            sample_dir,
            request=request,
            fixture_path=fixture_path,
            fixture_sha256=fixture_sha256,
            manual_stop=manual_stop,
            timeout_sec=timeout_sec,
        )
        ui_locale = ux._detect_ui_locale(
            repo_root,
            sample_dir / "locale-detection",
            process_id=runtime.app_pid,
            process_creation_time_100ns=int(runtime.generation["app"]["creationTime100ns"]),
            timeout_sec=timeout_sec,
        )
        ui_text = ux.UI_TEXT[ui_locale]
        sample_id, _prepared, _armed = _prepare_and_arm(
            runtime,
            request=request,
            manual_stop=manual_stop,
            timeout_sec=timeout_sec,
        )

        if manual_stop:
            _activate(
                repo_root,
                runtime,
                request=request,
                sample_id=sample_id,
                manual_stop=True,
                timeout_sec=timeout_sec,
            )
            _wait_recording(
                runtime,
                request=request,
                sample_id=sample_id,
                timeout_sec=timeout_sec,
            )
            manual_stop_ready = _wait_manual_stop_ready(
                runtime,
                request=request,
                sample_id=sample_id,
                timeout_sec=max(
                    timeout_sec,
                    FIXTURE_DURATION_MS / 1000.0 + 15.0,
                ),
            )

        generation = ux._wait_generation(
            runtime.app_pid,
            runtime.backend_pid,
            runtime.port,
            runtime.token,
            timeout_sec,
        )
        if not contract.process_generation_matches(runtime.generation, generation):
            raise RuntimeError("installed lifecycle runtime generation changed")
        baseline = ux._wait_frontend_performance(
            runtime.port,
            runtime.token,
            timeout_sec,
        )
        expected_text = (
            ui_text[EXPECTED_VISIBLE_TEXT_KEY[scenario]]
            if scenario in EXPECTED_VISIBLE_TEXT_KEY
            else str(_prepared.get("fixtureText") or "")
        )
        if not expected_text:
            raise RuntimeError("installed lifecycle expected text is unavailable")
        observer_process, observer_path = ux._start_observer(
            repo_root,
            sample_dir,
            expected_text=expected_text,
            process_id=runtime.app_pid,
            process_creation_time_100ns=int(generation["app"]["creationTime100ns"]),
            timeout_sec=timeout_sec,
        )

        start_path: Path | None = None
        if manual_stop:
            start, start_path = _run_stop_action(
                repo_root,
                runtime,
                sample_dir,
                timeout_sec=timeout_sec,
            )
        else:
            _activate(
                repo_root,
                runtime,
                request=request,
                sample_id=sample_id,
                manual_stop=False,
                timeout_sec=timeout_sec,
            )

        raw_marker, marker_status = _wait_marker(
            runtime,
            request=request,
            sample_id=sample_id,
            scenario=scenario,
            timeout_sec=max(timeout_sec, FIXTURE_DURATION_MS / 1000.0 + 120.0),
        )
        event = bind_installed_event(
            raw_marker,
            generation=generation,
            run_id=request.run_id,
            sample_id=sample_id,
            scenario=scenario,
            manual_stop_ready=manual_stop_ready,
            manual_stop_request=(
                marker_status.get("manualStopRequest")
                if manual_stop and isinstance(marker_status.get("manualStopRequest"), dict)
                else None
            ),
            stop_start_qpc_ticks=(int(start["qpcTicks"]) if manual_stop else None),
        )
        start = start if manual_stop else event
        observer = ux._wait_observer(
            observer_process,
            observer_path,
            sample_dir,
            timeout_sec,
        )
        performance, performance_binding = ux._collect_frontend_performance_window(
            artifact_root,
            sample_dir,
            runtime.port,
            runtime.token,
            int(observer["stableQpcTicks"]),
            baseline=baseline,
            timeout_sec=timeout_sec,
        )
        final_generation = ux._wait_generation(
            runtime.app_pid,
            runtime.backend_pid,
            runtime.port,
            runtime.token,
            timeout_sec,
        )
        if not contract.process_generation_matches(generation, final_generation):
            raise RuntimeError("installed lifecycle sample crossed a process generation")
        sample = _sample_from_installed_evidence(
            artifact_root,
            sample_dir,
            request=request,
            scenario=scenario,
            iteration=iteration,
            generation=generation,
            event=event,
            start=start,
            start_path=start_path,
            observer=observer,
            observer_path=observer_path,
            performance=performance,
            performance_binding=performance_binding,
        )

        if manual_stop:
            completed = _wait_status(
                runtime,
                request=request,
                sample_id=sample_id,
                timeout_sec=max(timeout_sec, 120.0),
                predicate=lambda value: (
                    value.get("state") == "completed"
                    and value.get("manualStopRequired") is True
                    and value.get("manualStopRequested") is True
                    and value.get("manualStopCompleted") is True
                ),
            )
            if completed.get("manualStopRequest") != event.get("manualStopRequest"):
                raise RuntimeError("manual Stop request attestation changed")
        idle_state = contract.wait_runtime_state(
            runtime.port,
            runtime.token,
            lambda value: value.get("recordingState") == "idle" and value.get("listening") is False,
            min(20.0, timeout_sec),
        )
        if idle_state.get("recordingState") != "idle" or idle_state.get("listening") is not False:
            raise RuntimeError("installed lifecycle runtime did not become idle before resource observation")
        resources = ux._idle_resource_observation(
            runtime.app_pid,
            runtime.backend_pid,
        )
        return sample, resources
    finally:
        if observer_process is not None and observer_process.poll() is None:
            contract.terminate_child(observer_process)
        if runtime is not None:
            cleanup = contract.terminate_runtime(
                runtime.app_pid,
                runtime.backend_pid,
                runtime.port,
                runtime.token,
            )
            ux._write_json(sample_dir / "runtime-cleanup.json", cleanup)
            resolved_data = runtime.data_dir.resolve()
            resolved_sample = sample_dir.resolve()
            if resolved_data != resolved_sample and resolved_sample in resolved_data.parents and resolved_data.exists():
                shutil.rmtree(resolved_data)


def collect_lifecycle_import(
    repo_root: Path,
    install_root: Path,
    output_path: Path,
    *,
    request: LifecycleRequest,
    timeout_sec: float,
) -> dict[str, Any]:
    artifact_root = output_path.parent.resolve()
    source_root = artifact_root / "lifecycle-source"
    if output_path.exists() or source_root.exists():
        raise RuntimeError("lifecycle output already exists; use a fresh evidence directory")
    artifact_root.mkdir(parents=True, exist_ok=True)

    fixture_path = artifact_root / "lifecycle-provider-fixture-s16le-48000-mono.pcm"
    fixture = contract.write_provider_replay_audio_fixture(
        fixture_path,
        duration_ms=FIXTURE_DURATION_MS,
    )
    if not contract.attest_provider_replay_audio_fixture(
        fixture_path,
        fixture,
        expected_duration_ms=FIXTURE_DURATION_MS,
    ):
        raise RuntimeError("lifecycle provider replay fixture attestation failed")
    fixture_sha256 = str(fixture["sha256"])

    samples: list[dict[str, Any]] = []
    resource_observations: list[dict[str, float]] = []
    for iteration in range(1, request.samples_per_scenario + 1):
        for scenario in LIFECYCLE_SCENARIOS:
            sample, resources = _collect_scenario(
                repo_root,
                install_root,
                artifact_root,
                request=request,
                scenario=scenario,
                iteration=iteration,
                fixture_path=fixture_path,
                fixture_sha256=fixture_sha256,
                timeout_sec=timeout_sec,
            )
            samples.append(sample)
            resource_observations.append(resources)

    resource_payload = {
        "runId": request.run_id,
        "installedExeSha256": request.installed_exe_sha256,
        "harnessManifestSha256": request.harness_manifest_sha256,
        "sampleCount": len(samples),
        "idleCpuPercent": max(float(item["idleCpuPercent"]) for item in resource_observations),
        "workingSetMb": max(float(item["workingSetMb"]) for item in resource_observations),
    }
    resource_binding = ux._write_artifact(
        artifact_root,
        "lifecycle-source/resource-evidence.json",
        resource_payload,
    )
    payload = {
        "schemaVersion": 1,
        "contract": contract.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
        "runId": request.run_id,
        "installedExeSha256": request.installed_exe_sha256,
        "harnessManifestSha256": request.harness_manifest_sha256,
        "samplesPerScenario": request.samples_per_scenario,
        "scenarioOrder": list(LIFECYCLE_SCENARIOS),
        "samples": samples,
        "resourceEvidence": {
            **resource_payload,
            "artifact": resource_binding,
        },
    }
    ux._write_json(output_path, payload)
    validation = contract.validate_app_ux_lifecycle_import(
        payload,
        artifact_root=artifact_root,
        required_samples_per_scenario=request.samples_per_scenario,
        expected_run_id=request.run_id,
        expected_installed_exe_sha256=request.installed_exe_sha256,
        expected_harness_manifest_sha256=request.harness_manifest_sha256,
    )
    ux._write_json(
        artifact_root / "app-ux-lifecycle-validation.json",
        validation,
    )
    if validation.get("metricEligible") is not True:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            "collected lifecycle evidence failed semantic validation: "
            + ", ".join(validation.get("reasons") or ["unknown reason"])
        )
    return validation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect installed B7 lifecycle App-UX evidence.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    args = parser.parse_args(argv)

    if os.name != "nt":
        raise SystemExit("app_ux_lifecycle_collector requires Windows")
    if args.timeout_sec < 5 or args.timeout_sec > 600:
        raise SystemExit("--timeout-sec must be in [5, 600]")
    repo_root = Path(args.repo_root).resolve()
    install_root = Path(args.install_root).resolve()
    request_path = Path(args.request).resolve()
    output_path = Path(args.output).resolve()
    request = load_and_validate_request(
        request_path,
        repo_root=repo_root,
        install_root=install_root,
    )
    try:
        validation = collect_lifecycle_import(
            repo_root,
            install_root,
            output_path,
            request=request,
            timeout_sec=float(args.timeout_sec),
        )
    except Exception as exc:
        failure = {
            "ok": False,
            "contract": contract.APP_UX_LIFECYCLE_IMPORT_CONTRACT,
            "reason": type(exc).__name__,
            "message": str(exc)[:500],
        }
        print(json.dumps(failure, ensure_ascii=False))
        return 2
    print(json.dumps(validation, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from benchmarks.windows import endpoint_probe


REPO_ROOT = Path(__file__).resolve().parents[2]


def _provider() -> dict[str, object]:
    return {
        "provider": "soniox",
        "defaultStt": "soniox",
        "requiredEnv": ["SONIOX_API_KEY"],
    }


def test_overlay_series_cleans_failed_keep_open_smoke_and_forces_always_on_off(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_capture(*_args, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="failed")

    monkeypatch.setattr(endpoint_probe, "run_capture", fake_run_capture)
    monkeypatch.setattr(
        endpoint_probe,
        "load_json",
        lambda _path: {
            "ok": False,
            "appPid": 101,
            "backendPid": 202,
            "backendPort": 8765,
        },
    )

    def fake_terminate(app_pid: int, backend_pid: int, port: int, token: str):
        captured["cleanup"] = (app_pid, backend_pid, port, bool(token))
        return {"ok": True, "appExited": True, "backendExited": True}

    monkeypatch.setattr(endpoint_probe, "terminate_runtime", fake_terminate)

    result = endpoint_probe.run_overlay_process_series(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "work",
        5,
        "python",
        _provider(),
        series_index=1,
        warm_iterations=0,
    )

    assert result["ok"] is False
    assert result["reason"] == "runtime_start_failed"
    assert result["cleanup"]["ok"] is True
    assert captured["cleanup"] == (101, 202, 8765, True)
    assert captured["env"]["SCRIBER_MIC_ALWAYS_ON"] == "0"
    assert result["micAlwaysOnChildEnv"] == "0"


def test_hot_path_readiness_accepts_only_explicit_tauri_qpc_marker(monkeypatch) -> None:
    base_item = {
        "sessionId": "session-1",
        "markerNames": [
            "hotkey_received",
            "tauri_hotkey_received",
            "mic_ready",
            "first_audio_frame",
        ],
        "segments": {
            "hotkey_received_to_mic_ready_ms": 12.0,
            "hotkey_received_to_first_audio_frame_ms": 20.0,
        },
    }
    payload = {"activeItems": [base_item], "items": []}
    monkeypatch.setattr(endpoint_probe, "request_runtime_json", lambda *_args, **_kwargs: payload)

    missing = endpoint_probe.hot_path_readiness(8765, "token", "session-1")
    assert missing["verified"] is True
    assert missing["tauriHotkeyReceivedMarkerAvailable"] is False
    assert missing["tauriHotkeyReceivedMarker"] is None

    base_item["tauriHotkeyReceived"] = {
        "schemaVersion": 1,
        "marker": "hotkey_received",
        "source": "tauri_global_shortcut",
        "runId": "7de1a48651d44f859042b7cbcb30da52",
        "sampleId": "2b3022ee3f404333a1156da089a24962",
        "processId": 4321,
        "qpcTicks": 1234,
        "qpcFrequency": 1000,
        "timestampNs": 1_234_000_000,
    }
    present = endpoint_probe.hot_path_readiness(
        8765,
        "token",
        "session-1",
        expected_run_id="7de1a48651d44f859042b7cbcb30da52",
        expected_process_id=4321,
        seen_sample_ids=set(),
    )
    assert present["tauriHotkeyReceivedMarkerAvailable"] is True
    assert present["tauriHotkeyReceivedMarker"] == base_item["tauriHotkeyReceived"]

    wrong_process = endpoint_probe.hot_path_readiness(
        8765,
        "token",
        "session-1",
        expected_run_id="7de1a48651d44f859042b7cbcb30da52",
        expected_process_id=9999,
        seen_sample_ids=set(),
    )
    assert wrong_process["tauriHotkeyReceivedMarkerAvailable"] is False
    duplicate = endpoint_probe.hot_path_readiness(
        8765,
        "token",
        "session-1",
        expected_run_id="7de1a48651d44f859042b7cbcb30da52",
        expected_process_id=4321,
        seen_sample_ids={"2b3022ee3f404333a1156da089a24962"},
    )
    assert duplicate["tauriHotkeyReceivedMarkerAvailable"] is False

    base_item["tauriHotkeyReceived"]["source"] = "windows_send_input"
    wrong_source = endpoint_probe.hot_path_readiness(8765, "token", "session-1")
    assert wrong_source["tauriHotkeyReceivedMarkerAvailable"] is False


def test_only_stopped_idle_state_is_a_successful_terminal_session() -> None:
    stopped = {
        "listening": False,
        "sessionId": None,
        "recordingState": "idle",
        "status": "Stopped",
    }
    failed = {**stopped, "status": "Error"}
    incomplete = {**stopped, "recordingState": "finalizing"}

    assert endpoint_probe.terminal_state_observed(stopped) is True
    assert endpoint_probe.successful_terminal_state(stopped) is True
    assert endpoint_probe.terminal_state_observed(failed) is True
    assert endpoint_probe.successful_terminal_state(failed) is False
    assert endpoint_probe.terminal_state_observed(incomplete) is False
    assert endpoint_probe.successful_terminal_state(incomplete) is False


def test_process_generation_comparison_is_fail_closed() -> None:
    baseline = {"ok": True, "fingerprint": "generation-a"}
    assert endpoint_probe.process_generation_matches(baseline, dict(baseline)) is True
    assert endpoint_probe.process_generation_matches(
        baseline, {"ok": True, "fingerprint": "generation-b"}
    ) is False
    assert endpoint_probe.process_generation_matches(
        baseline, {"ok": False, "fingerprint": "generation-a"}
    ) is False
    assert endpoint_probe.process_generation_matches({}, {}) is False


def test_overlay_observer_requires_ready_handshake_and_exact_pid_hwnd_contract() -> None:
    source = (
        REPO_ROOT / "benchmarks" / "windows" / "overlay_observer.py"
    ).read_text(encoding="utf-8")

    assert 'parser.add_argument("--expected-pid"' in source
    assert 'parser.add_argument("--expected-hwnd"' in source
    assert 'parser.add_argument("--ready-output"' in source
    assert '"endpoint": "overlay_observer_ready"' in source
    assert 'ready_observation.get("pid") == args.expected_pid' in source
    assert 'not ready_observation.get("visible")' in source
    assert 'item.get("hwndHash") == hwnd_hash(args.expected_hwnd)' in source
    assert "hash(str(hwnd))" not in source


def test_overlay_sample_attests_target_focus_generations_and_blocks_missing_tauri_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    smoke = {
        "ok": True,
        "appPid": 101,
        "backendPid": 202,
        "backendPort": 8765,
    }
    overlay_hwnd = 333
    overlay_hash = endpoint_probe.hwnd_hash(overlay_hwnd)
    observed = {
        "ok": True,
        "qpcFrequency": 1000,
        "firstVisible": {
            "qpcTicks": 1200,
            "pid": 101,
            "hwndHash": overlay_hash,
        },
    }
    monkeypatch.setattr(
        endpoint_probe,
        "load_json",
        lambda path: observed if path.name == "overlay-observer.json" else smoke,
    )
    captured: dict[str, object] = {}

    def fake_run_capture(*_args, **kwargs):
        captured["childEnv"] = kwargs["env"]
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(endpoint_probe, "run_capture", fake_run_capture)
    observer = SimpleNamespace(poll=lambda: None, terminate=lambda: None, kill=lambda: None)

    def fake_run_process(args, **_kwargs):
        captured["observerArgs"] = args
        return observer

    monkeypatch.setattr(endpoint_probe, "run_process", fake_run_process)
    monkeypatch.setattr(endpoint_probe, "wait_process", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(endpoint_probe, "terminate_child", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        endpoint_probe,
        "wait_for_json_file",
        lambda *_args, **_kwargs: {
            "ok": True,
            "endpoint": "overlay_observer_ready",
            "expectedPid": 101,
            "expectedHwndHash": overlay_hash,
        },
    )
    monkeypatch.setattr(endpoint_probe, "wait_overlay_hidden", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        endpoint_probe,
        "overlay_window_snapshot",
        lambda _pid: {
            "ok": True,
            "hwnd": overlay_hwnd,
            "hwndHash": overlay_hash,
            "pid": 101,
            "visible": False,
        },
    )
    generation = {
        "ok": True,
        "fingerprint": "same-generation",
        "app": {"pid": 101, "creationTime100ns": 1},
        "backend": {"pid": 202, "creationTime100ns": 2},
        "webViewProcesses": [{"pid": 303, "creationTime100ns": 3}],
    }
    monkeypatch.setattr(
        endpoint_probe,
        "process_generation_snapshot",
        lambda *_args, **_kwargs: dict(generation),
    )
    focus = {
        "ok": True,
        "hwnd": 444,
        "hwndHash": endpoint_probe.hwnd_hash(444),
        "pid": 404,
        "processCreationTime100ns": 4,
    }
    monkeypatch.setattr(endpoint_probe, "foreground_window_snapshot", lambda: dict(focus))
    dispatch_ticks = iter([1000, 1300])
    monkeypatch.setattr(endpoint_probe, "send_global_hotkey_chord", lambda: next(dispatch_ticks))
    active = {
        "listening": True,
        "sessionId": "session-1",
        "recordingState": "recording",
        "status": "Listening",
    }
    terminal = {
        "listening": False,
        "sessionId": None,
        "recordingState": "idle",
        "status": "Stopped",
    }
    states = iter([active, terminal])
    monkeypatch.setattr(
        endpoint_probe,
        "wait_runtime_state",
        lambda *_args, **_kwargs: next(states),
    )
    monkeypatch.setattr(
        endpoint_probe,
        "request_runtime_json",
        lambda _port, _token, path, **_kwargs: (
            {"microphone": {"micAlwaysOn": False}}
            if path == "/api/runtime/audio-diagnostics"
            else {}
        ),
    )
    monkeypatch.setattr(
        endpoint_probe,
        "hot_path_readiness",
        lambda *_args, **_kwargs: {
            "verified": True,
            "hotkeyToMicReadyMs": 10.0,
            "hotkeyToFirstAudioFrameMs": 20.0,
            "tauriHotkeyReceivedMarkerAvailable": False,
            "tauriHotkeyReceivedMarker": None,
        },
    )
    monkeypatch.setattr(
        endpoint_probe,
        "terminate_runtime",
        lambda *_args, **_kwargs: {"ok": True, "appExited": True, "backendExited": True},
    )

    result = endpoint_probe.run_overlay_process_series(
        REPO_ROOT,
        tmp_path / "install",
        tmp_path / "work",
        5,
        "python",
        _provider(),
        series_index=1,
        warm_iterations=0,
    )

    assert result["ok"] is False
    assert result["reason"] == "tauri_hotkey_received_marker_unavailable"
    assert result["cleanup"]["ok"] is True
    assert captured["childEnv"]["SCRIBER_MIC_ALWAYS_ON"] == "0"
    benchmark_run_id = captured["childEnv"][
        "SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID"
    ]
    assert len(benchmark_run_id) == 32
    assert result["benchmarkRunId"] == benchmark_run_id
    observer_args = captured["observerArgs"]
    assert observer_args[observer_args.index("--expected-pid") + 1] == "101"
    assert observer_args[observer_args.index("--expected-hwnd") + 1] == "333"
    assert "--ready-output" in observer_args

    sample = result["samples"][0]
    assert sample["durationMs"] == "unknown"
    assert sample["windowsDispatchToVisibleMs"] == 200.0
    assert sample["windowsDispatchDiagnosticOnly"] is True
    assert sample["primaryStartMarker"] == "unknown"
    assert sample["metricEligible"] is False
    assert sample["metricBlockedReason"] == "tauri_hotkey_received_marker_unavailable"
    assert sample["terminalSuccessful"] is True
    assert sample["focusPreserved"] is True
    assert sample["processGenerationBeforeMatches"] is True
    assert sample["processGenerationAfterMatches"] is True
    assert sample["expectedOverlayTargetObserved"] is True
    markers = [event["marker"] for event in result["events"]]
    assert markers == ["windows_hotkey_dispatch_started", "overlay_first_visible_frame"]
    assert "hotkey_received" not in markers

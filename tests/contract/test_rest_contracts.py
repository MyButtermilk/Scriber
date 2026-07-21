from __future__ import annotations

import asyncio
import copy

import pytest

from src.core.rest_contracts import (
    REST_API_VERSION,
    RESTContractError,
    validate_audio_diagnostics_payload,
    validate_frontend_performance_flush_request_payload,
    validate_frontend_performance_payload,
    validate_frontend_performance_request_payload,
    validate_frontend_ready_payload,
    validate_frontend_ready_request_payload,
    validate_health_payload,
    validate_live_mic_stop_request_payload,
    validate_runtime_payload,
    validate_tauri_activation_marker_request_payload,
    validate_tauri_hotkey_marker_request_payload,
)
from src.web_api import ScriberWebController


def test_runtime_and_health_payloads_match_contract(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        runtime = ctl.get_runtime_info()
        health = ctl.get_health()
        frontend_ready = ctl.get_frontend_ready()
        frontend_performance = ctl.get_frontend_performance()
        audio_diagnostics = ctl.get_audio_diagnostics()
    finally:
        loop.close()

    validate_runtime_payload(runtime)
    validate_health_payload(health)
    validate_frontend_ready_payload(frontend_ready)
    validate_frontend_performance_payload(frontend_performance)
    validate_audio_diagnostics_payload(audio_diagnostics)
    assert runtime["apiVersion"] == REST_API_VERSION
    assert health["apiVersion"] == REST_API_VERSION
    assert frontend_ready["apiVersion"] == REST_API_VERSION
    assert frontend_performance["apiVersion"] == REST_API_VERSION
    assert audio_diagnostics["apiVersion"] == REST_API_VERSION
    assert health["workerVersion"] == runtime["workerVersion"]
    assert health["runtimeMode"] == runtime["runtimeMode"]
    assert health["pid"] == runtime["pid"]
    assert audio_diagnostics["runtimeMode"] == runtime["runtimeMode"]
    assert audio_diagnostics["pid"] == runtime["pid"]


def test_runtime_contract_rejects_incompatible_payload() -> None:
    with pytest.raises(RESTContractError):
        validate_runtime_payload({"apiVersion": REST_API_VERSION})

    valid_minimal = {
        "version": "1.0.0",
        "apiVersion": REST_API_VERSION,
        "workerVersion": "1",
        "runtimeMode": "tauri-supervised",
        "launchKind": "sidecar",
        "pid": 123,
        "host": "127.0.0.1",
        "port": 8765,
        "startedAt": "2026-06-02T00:00:00Z",
        "uptimeSeconds": 0.1,
        "dataDir": "data",
        "downloadsDir": "downloads",
        "logsDir": "logs",
        "activeSession": None,
        "recordingState": "idle",
        "capabilities": {
            "rest": True,
            "websocket": True,
            "liveMic": True,
            "fileTranscription": True,
            "youtubeTranscription": True,
            "exports": ["pdf", "docx"],
            "localStt": False,
        },
        "featureFlags": {
            "audioEngine": "python",
            "requestedAudioEngine": "python",
            "rustAudioRequested": False,
            "rustAudioAvailable": False,
            "nativeDeviceEvents": "auto",
            "requestedNativeDeviceEvents": "auto",
            "nativeDeviceEventsRequested": True,
            "micAlwaysOn": False,
            "sessionTokenRequired": True,
            "validateWsContracts": False,
        },
        "startup": {
            "transcriptsLoaded": False,
            "deviceMonitor": "disabled",
        },
    }
    validate_runtime_payload(valid_minimal)

    invalid = dict(valid_minimal)
    invalid["apiVersion"] = "0"
    with pytest.raises(RESTContractError):
        validate_runtime_payload(invalid)

    invalid = dict(valid_minimal)
    invalid["capabilities"] = {**valid_minimal["capabilities"], "exports": []}
    with pytest.raises(RESTContractError):
        validate_runtime_payload(invalid)


def test_health_contract_rejects_incompatible_payload() -> None:
    valid = {
        "ok": True,
        "ready": True,
        "version": "1.0.0",
        "apiVersion": REST_API_VERSION,
        "workerVersion": "1",
        "pid": 123,
        "host": "127.0.0.1",
        "port": 8765,
        "startedAt": "2026-06-02T00:00:00Z",
        "uptimeSeconds": 0.1,
        "activeSession": None,
        "recordingState": "idle",
        "runtimeMode": "tauri-supervised",
    }
    validate_health_payload(valid)

    invalid = dict(valid)
    invalid["ready"] = "yes"
    with pytest.raises(RESTContractError):
        validate_health_payload(invalid)

    invalid = dict(valid)
    invalid["uptimeSeconds"] = -1
    with pytest.raises(RESTContractError):
        validate_health_payload(invalid)


def test_frontend_ready_contract_rejects_incompatible_payload() -> None:
    valid_empty = {
        "apiVersion": REST_API_VERSION,
        "ready": False,
        "lastSeen": None,
    }
    validate_frontend_ready_payload(valid_empty)

    valid_ready = {
        "apiVersion": REST_API_VERSION,
        "ready": True,
        "lastSeen": {
            "receivedAt": "2026-06-02T00:00:00Z",
            "receivedAtUptimeSeconds": 1.0,
            "runtimeMode": "tauri-supervised",
            "pid": 123,
            "tauriRuntime": True,
            "backendBaseUrl": "http://127.0.0.1:8765",
            "locationOrigin": "http://tauri.localhost",
            "path": "/",
            "origin": "http://tauri.localhost",
            "userAgent": "Scriber smoke",
        },
    }
    validate_frontend_ready_payload(valid_ready)

    invalid = dict(valid_ready)
    invalid["apiVersion"] = "0"
    with pytest.raises(RESTContractError):
        validate_frontend_ready_payload(invalid)

    invalid = dict(valid_ready)
    invalid["lastSeen"] = {**valid_ready["lastSeen"], "tauriRuntime": "yes"}
    with pytest.raises(RESTContractError):
        validate_frontend_ready_payload(invalid)


def test_frontend_ready_request_contract_rejects_incompatible_payload() -> None:
    valid = {
        "apiVersion": REST_API_VERSION,
        "tauriRuntime": True,
        "backendBaseUrl": "http://127.0.0.1:8765",
        "locationOrigin": "http://tauri.localhost",
        "path": "/",
    }
    validate_frontend_ready_request_payload(valid)

    invalid = dict(valid)
    invalid.pop("apiVersion")
    with pytest.raises(RESTContractError):
        validate_frontend_ready_request_payload(invalid)

    invalid = dict(valid)
    invalid["tauriRuntime"] = "yes"
    with pytest.raises(RESTContractError):
        validate_frontend_ready_request_payload(invalid)


def test_tauri_hotkey_marker_contract_binds_run_sample_parent_and_qpc() -> None:
    run_id = "7de1a48651d44f859042b7cbcb30da52"
    sample_id = "2b3022ee3f404333a1156da089a24962"
    qpc_ticks = 123_456_789
    qpc_frequency = 10_000_000
    timestamp_ns = (qpc_ticks * 1_000_000_000) // qpc_frequency
    payload = {
        "benchmarkHotkeyMarker": {
            "schemaVersion": 1,
            "marker": "hotkey_received",
            "source": "tauri_global_shortcut",
            "runId": run_id,
            "sampleId": sample_id,
            "processId": 4321,
            "qpcTicks": qpc_ticks,
            "qpcFrequency": qpc_frequency,
            "timestampNs": timestamp_ns,
        }
    }

    normalized = validate_tauri_hotkey_marker_request_payload(
        payload,
        configured_run_id="7de1a486-51d4-4f85-9042-b7cbcb30da52",
        expected_parent_pid=4321,
        now_ns=timestamp_ns + 1_000_000,
    )

    assert normalized["runId"] == run_id
    assert normalized["sampleId"] == sample_id
    assert normalized["processId"] == 4321
    assert normalized["timestampNs"] == timestamp_ns

    with pytest.raises(RESTContractError, match="disabled"):
        validate_tauri_hotkey_marker_request_payload(
            payload,
            configured_run_id=None,
            expected_parent_pid=4321,
            now_ns=timestamp_ns + 1_000_000,
        )
    with pytest.raises(RESTContractError, match="managed backend parent"):
        validate_tauri_hotkey_marker_request_payload(
            payload,
            configured_run_id=run_id,
            expected_parent_pid=9999,
            now_ns=timestamp_ns + 1_000_000,
        )
    with pytest.raises(RESTContractError, match="stale"):
        validate_tauri_hotkey_marker_request_payload(
            payload,
            configured_run_id=run_id,
            expected_parent_pid=4321,
            now_ns=timestamp_ns + 31_000_000_000,
        )

    leaked = copy.deepcopy(payload)
    leaked["benchmarkHotkeyMarker"]["text"] = "must never be accepted"
    with pytest.raises(RESTContractError, match="unsupported marker fields"):
        validate_tauri_hotkey_marker_request_payload(
            leaked,
            configured_run_id=run_id,
            expected_parent_pid=4321,
            now_ns=timestamp_ns + 1_000_000,
        )


def test_tauri_activation_marker_contract_accepts_only_authoritative_lane_pair() -> None:
    run_id = "7de1a48651d44f859042b7cbcb30da52"
    qpc_ticks = 123_456_789
    qpc_frequency = 10_000_000
    timestamp_ns = (qpc_ticks * 1_000_000_000) // qpc_frequency
    payload = {
        "benchmarkActivationMarker": {
            "schemaVersion": 1,
            "marker": "button_received",
            "source": "tauri_ui_command",
            "runId": run_id,
            "sampleId": "2b3022ee3f404333a1156da089a24962",
            "processId": 4321,
            "qpcTicks": qpc_ticks,
            "qpcFrequency": qpc_frequency,
            "timestampNs": timestamp_ns,
        }
    }

    normalized = validate_tauri_activation_marker_request_payload(
        payload,
        configured_run_id=run_id,
        expected_parent_pid=4321,
        now_ns=timestamp_ns + 1_000_000,
    )
    assert normalized["activationKind"] == "button"
    assert normalized["marker"] == "button_received"
    assert normalized["source"] == "tauri_ui_command"

    mismatched = copy.deepcopy(payload)
    mismatched["benchmarkActivationMarker"]["source"] = "tauri_global_shortcut"
    with pytest.raises(RESTContractError, match="authoritative activation"):
        validate_tauri_activation_marker_request_payload(
            mismatched,
            configured_run_id=run_id,
            expected_parent_pid=4321,
            now_ns=timestamp_ns + 1_000_000,
        )

    expanded = copy.deepcopy(payload)
    expanded["benchmarkHotkeyMarker"] = expanded["benchmarkActivationMarker"]
    with pytest.raises(RESTContractError, match="permits only"):
        validate_tauri_activation_marker_request_payload(
            expanded,
            configured_run_id=run_id,
            expected_parent_pid=4321,
            now_ns=timestamp_ns + 1_000_000,
        )


def test_frontend_performance_contract_records_bounded_long_tasks(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    request = {
        "apiVersion": REST_API_VERSION,
        "sourceInstanceId": "webview-123",
        "observerSupported": True,
        "windowStartedAtMs": 100.0,
        "observedAtMs": 900.0,
        "droppedEntries": 0,
        "heartbeatSequence": 0,
        "entries": [
            {"sequence": 1, "startTimeMs": 200.0, "durationMs": 225.5},
            {"sequence": 2, "startTimeMs": 500.0, "durationMs": 300.25},
        ],
    }
    validate_frontend_performance_request_payload(request)

    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        response = ctl.record_frontend_performance(request)
        replayed = ctl.record_frontend_performance({**request, "droppedEntries": 1})
        replayed = ctl.record_frontend_performance({**request, "droppedEntries": 1})
        forged = ctl.record_frontend_performance(
            {**request, "entries": [], "heartbeatSequence": 999}
        )
        assert ctl.request_frontend_performance_flush("another-webview") is None
        flush_request = ctl.request_frontend_performance_flush("webview-123")
        assert flush_request is not None
        stale = ctl.record_frontend_performance(
            {
                **request,
                "entries": [],
                "heartbeatSequence": flush_request["heartbeatSequence"],
            }
        )
        acknowledged = ctl.record_frontend_performance(
            {
                **request,
                "observedAtMs": 1_000.0,
                "entries": [],
                "heartbeatSequence": flush_request["heartbeatSequence"],
            }
        )
        delta = ctl.get_frontend_performance(
            after_sequence=1,
            source_instance_id="webview-123",
        )
    finally:
        loop.close()

    validate_frontend_performance_payload(response)
    validate_frontend_performance_payload(delta)
    assert response["window"]["count"] == 2
    assert response["window"]["maxDurationMs"] == 300.25
    assert replayed["window"]["cumulativeCount"] == 2
    assert replayed["window"]["droppedEntries"] == 1
    assert forged["window"]["heartbeatSequence"] == 0
    assert stale["window"]["heartbeatSequence"] == 0
    assert acknowledged["window"]["heartbeatSequence"] == flush_request["heartbeatSequence"]
    assert acknowledged["window"]["heartbeatReceivedAtUptimeSeconds"] >= flush_request["requestedAtUptimeSeconds"]
    assert delta["window"]["count"] == 1
    assert delta["window"]["totalDurationMs"] == 300.25
    assert delta["window"]["queryAfterSequence"] == 1

    changed = ctl.get_frontend_performance(
        after_sequence=1,
        source_instance_id="another-webview",
    )
    validate_frontend_performance_payload(changed)
    assert changed["available"] is False
    assert changed["reason"] == "source_instance_changed"


def test_frontend_performance_request_rejects_fake_or_sensitive_evidence() -> None:
    valid = {
        "apiVersion": REST_API_VERSION,
        "sourceInstanceId": "webview-123",
        "observerSupported": True,
        "windowStartedAtMs": 100.0,
        "observedAtMs": 900.0,
        "droppedEntries": 0,
        "heartbeatSequence": 0,
        "entries": [
            {"sequence": 1, "startTimeMs": 200.0, "durationMs": 225.5},
        ],
    }

    for invalid in (
        {**valid, "sourceInstanceId": "https://private.example/person"},
        {**valid, "observerSupported": False},
        {**valid, "heartbeatSequence": -1},
        {
            **valid,
            "entries": [{"sequence": 1, "startTimeMs": 200.0, "durationMs": 200.0}],
        },
        {
            **valid,
            "entries": [
                {"sequence": 2, "startTimeMs": 200.0, "durationMs": 225.5},
                {"sequence": 1, "startTimeMs": 500.0, "durationMs": 250.0},
            ],
        },
    ):
        with pytest.raises(RESTContractError):
            validate_frontend_performance_request_payload(invalid)

    validate_frontend_performance_flush_request_payload(
        {"apiVersion": REST_API_VERSION, "sourceInstanceId": "webview-123"}
    )
    with pytest.raises(RESTContractError):
        validate_frontend_performance_flush_request_payload(
            {
                "apiVersion": REST_API_VERSION,
                "sourceInstanceId": "https://private.example/person",
            }
        )


def test_frontend_performance_drop_and_sequence_gap_counters_are_cumulative(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    request = {
        "apiVersion": REST_API_VERSION,
        "sourceInstanceId": "webview-gap",
        "observerSupported": True,
        "windowStartedAtMs": 100.0,
        "observedAtMs": 900.0,
        "droppedEntries": 1,
        "heartbeatSequence": 0,
        "entries": [
            {"sequence": 2, "startTimeMs": 200.0, "durationMs": 225.5},
        ],
    }

    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        first = ctl.record_frontend_performance(request)
        replay = ctl.record_frontend_performance(request)
        second = ctl.record_frontend_performance(
            {
                **request,
                "observedAtMs": 1_100.0,
                "entries": [
                    {"sequence": 4, "startTimeMs": 1_000.0, "durationMs": 250.0},
                ],
            }
        )
    finally:
        loop.close()

    assert first["window"]["droppedEntries"] == 1
    assert first["window"]["sequenceGaps"] == 1
    assert replay["window"]["droppedEntries"] == 1
    assert replay["window"]["sequenceGaps"] == 1
    assert second["window"]["droppedEntries"] == 1
    assert second["window"]["sequenceGaps"] == 2


@pytest.mark.parametrize(
    ("stop_scheduled", "already_finalizing", "already_stopped", "finalizing"),
    (
        (True, False, False, True),
        (False, True, False, True),
        (False, False, True, False),
    ),
)
def test_live_mic_stop_request_contract_accepts_each_idempotent_disposition(
    stop_scheduled: bool,
    already_finalizing: bool,
    already_stopped: bool,
    finalizing: bool,
) -> None:
    validate_live_mic_stop_request_payload(
        {
            "apiVersion": REST_API_VERSION,
            "stopAccepted": True,
            "stopScheduled": stop_scheduled,
            "alreadyFinalizing": already_finalizing,
            "alreadyStopped": already_stopped,
            "finalizing": finalizing,
            "sessionId": "session-1" if finalizing else None,
        }
    )


def test_live_mic_stop_request_contract_rejects_conflicting_dispositions() -> None:
    conflicting = {
        "apiVersion": REST_API_VERSION,
        "stopAccepted": True,
        "stopScheduled": True,
        "alreadyFinalizing": True,
        "alreadyStopped": False,
        "finalizing": True,
        "sessionId": "session-1",
    }
    with pytest.raises(RESTContractError):
        validate_live_mic_stop_request_payload(conflicting)

    false_completion = {
        **conflicting,
        "stopScheduled": False,
        "alreadyFinalizing": False,
        "alreadyStopped": True,
        "finalizing": True,
    }
    with pytest.raises(RESTContractError):
        validate_live_mic_stop_request_payload(false_completion)


def test_audio_diagnostics_contract_rejects_incompatible_payload() -> None:
    valid = {
        "apiVersion": REST_API_VERSION,
        "runtimeMode": "tauri-supervised",
        "pid": 123,
        "recordingState": "idle",
        "featureFlags": {
            "audioEngine": "python",
            "requestedAudioEngine": "python",
            "rustAudioRequested": False,
            "rustAudioAvailable": False,
            "nativeDeviceEvents": "auto",
            "requestedNativeDeviceEvents": "auto",
            "nativeDeviceEventsRequested": True,
        },
        "provider": {
            "configured": "azure_mai",
            "active": None,
            "sonioxMode": "realtime",
        },
        "microphone": {
            "configuredDevice": "default",
            "favoriteMic": "",
            "favoriteMicConfigured": False,
            "micAlwaysOn": False,
            "idlePrewarmActive": False,
            "prebufferMs": 400,
            "prewarm": {
                "configured": True,
                "engine": "rust-wasapi",
                "active": True,
                "hasStream": True,
                "prewarmIdHash": "prewarm-hash",
                "activeCaptureAttached": False,
                "pausedForActiveCapture": False,
                "pausedForDeviceRefresh": False,
                "streamStartedAgoSeconds": 0.25,
                "streamStartCount": 2,
                "streamCloseCount": 1,
                "healthRestartCount": 0,
                "deviceRefreshPauseCount": 0,
                "deviceRefreshResumeCount": 0,
                "activeCapturePauseCount": 1,
                "activeCaptureResumeCount": 1,
                "activeCaptureResumeReadyCount": 1,
                "activeCaptureResumeFailedCount": 0,
                "lastActiveCaptureResumeGapMs": 12.0,
                "lastActiveCaptureStopToReadyMs": 18.0,
                "maxActiveCaptureStopToReadyMs": 18.0,
                "adoptionCount": 1,
                "lastAdoptedPrewarmIdHash": "adopted-hash",
                "lastActiveCaptureDetachAgoSeconds": 0.4,
                "lastActiveCaptureResumeAttemptAgoSeconds": 0.3,
                "lastError": "",
                "lastStartAttemptAgoSeconds": 0.25,
                "lastStartDurationMs": 9.0,
                "lastStartResponseMs": 8.5,
                "lastStartSuccess": True,
                "lastStopAgoSeconds": 1.0,
                "lastStopReason": "active_capture",
                "lastStopResponseMs": 4.0,
                "lastStopSuccess": True,
                "lastStopError": "",
                "lastHealthCheckAgoSeconds": 0.1,
                "lastHealthCheckReason": "watchdog",
                "lastHealthCheckActive": True,
                "lastHealthResponseMs": 3.0,
                "lastHealthError": "",
                "lastTransition": "started",
                "lastTransitionReason": "start",
                "lastTransitionAgoSeconds": 0.25,
                "lastStop": {"prewarmIdHash": "stop-hash"},
                "lastStatus": {"active": True, "prewarmIdHash": "status-hash"},
                "start": {"prewarmIdHash": "start-hash"},
                "recentEvents": [
                    {
                        "event": "started",
                        "reason": "start",
                        "ageSeconds": 0.25,
                        "prewarmIdHash": "prewarm-hash",
                        "activeCaptureResumeGapMs": 12.0,
                        "activeCaptureStopToReadyMs": 18.0,
                    }
                ],
            },
            "nativeDeviceEvents": {
                "shellIpcAvailable": False,
                "available": False,
                "reason": "shellIpcUnavailable",
            },
            "rustAudioFallbackCircuit": {
                "available": True,
                "open": True,
                "reason": "pipeClosed",
                "remainingSeconds": 12.5,
                "cooldownSeconds": 60.0,
            },
            "activeCapture": {
                "running": True,
                "engine": "rust-wasapi",
                "requestedEngine": "rust-wasapi",
                "frameSource": "rust-frame-pipe",
                "hasStream": True,
                "streamActive": True,
                "sampleRate": 16000,
                "targetChannels": 1,
                "captureChannels": 1,
                "blockSize": 512,
                "device": "default",
                "callbackCount": 3,
                "droppedFrameCount": 0,
                "nativeEndpointIdHash": "endpoint-hash",
                "sidecarPid": 1234,
                "sidecarExitStatus": 0,
                "sidecarUptimeMs": 55,
                "sidecarKilledAfterTimeout": False,
                "sidecarWaitError": None,
                "sidecarConnected": True,
                "sidecarFramesWritten": 3,
                "sidecarPrebufferFramesWritten": 1,
                "sidecarLiveFramesWritten": 2,
                "sidecarBytesWritten": 3072,
                "sidecarWriterError": None,
                "sidecarStopReason": "captureStop",
                "sidecarStartCount": 1,
                "sidecarRestartCount": 0,
                "readerThreadAlive": False,
                "framePipeFramesRead": 3,
                "framePipeAudioFramesRead": 1536,
                "framePipeSequenceErrorCount": 0,
                "framePipeProtocolErrorCount": 0,
                "framePipePrebufferAfterLiveCount": 0,
                "framePipeFirstFrameReadMs": 9.5,
                "framePipeReaderEndReason": "stopRequested",
                "midSessionFailureReason": "",
                "lastRustAudioMidSessionFailureReason": "",
                "source": {
                    "engine": "rust-wasapi",
                    "frameSource": "rust-frame-pipe",
                    "hasStream": False,
                    "streamActive": False,
                    "sidecarKilledAfterTimeout": False,
                    "sidecarWaitError": None,
                },
            },
        },
        "watchdog": {
            "enabled": True,
            "intervalSeconds": 5.0,
            "callbackGapSeconds": 15.0,
            "taskRunning": False,
            "lastWarning": {
                "message": "Live microphone watchdog could not verify active capture",
                "recordedAt": "2026-06-11T12:00:00Z",
                "recordedAtUptimeSeconds": 12.5,
                "diagnostics": {
                    "engine": "rust-wasapi",
                    "frameSource": "rust-frame-pipe",
                    "streamActive": True,
                    "lastHealthFailureReason": "staleCallbacks",
                    "healthRestartThrottleCount": 1,
                    "lastHealthRestartThrottledReason": "watchdog:staleCallbacks",
                    "lastHealthRestartThrottleRemainingSeconds": 2.5,
                },
            },
        },
        "textInjection": {
            "method": "auto",
            "disabled": False,
            "pastePreDelayMs": 80,
            "pasteRestoreDelayMs": 1500,
            "shellIpc": {
                "available": False,
                "pipeConfigured": False,
                "tokenConfigured": False,
                "apiVersion": "1",
                "pipeNameHash": None,
                "lastCommand": None,
                "lastSuccess": None,
                "lastError": None,
                "lastCommandAgoSeconds": None,
            },
        },
        "runtimeImports": {
            "onnxruntime": {
                "importable": True,
                "error": None,
            },
            "pipecat.audio.vad.silero": {
                "importable": False,
                "error": "ModuleNotFoundError: example",
            },
        },
    }
    validate_audio_diagnostics_payload(valid)

    tauri_injection = copy.deepcopy(valid)
    tauri_injection["textInjection"]["method"] = "tauri"
    tauri_injection["textInjection"]["shellIpc"] = {
        "available": True,
        "pipeConfigured": True,
        "tokenConfigured": True,
        "apiVersion": "1",
        "pipeNameHash": "pipe-hash",
        "lastCommand": "injectText",
        "lastSuccess": True,
        "lastError": None,
        "lastErrorCode": None,
        "lastFallbackReason": None,
        "lastCommandAgoSeconds": 0.1,
        "lastResponse": {
            "success": True,
            "errorCode": None,
            "fallbackReason": None,
            "timingsMs": {"total": 12.0},
            "payload": {
                "method": "tauri",
                "dispatch": "ctrlV",
                "preDelayMode": "auto",
                "requestedPreDelayMs": 80.0,
                "deadlineMs": 2000.0,
                "markers": ["clipboard_set", "paste"],
                "restoreScheduled": True,
                "restore": {
                    "scheduled": True,
                    "attempted": False,
                    "succeeded": None,
                    "skippedReason": "scheduled",
                    "errorCode": None,
                },
                "foregroundBefore": {
                    "available": True,
                    "windowHash": "window-hash",
                    "titleHash": "title-hash",
                    "processIdHash": "pid-hash",
                },
                "foregroundAfter": {
                    "available": True,
                    "windowHash": "window-hash",
                    "titleHash": "title-hash",
                    "processIdHash": "pid-hash",
                },
                "foregroundChanged": False,
                "timingsMs": {
                    "clipboardRead": 1.0,
                    "clipboardSet": 2.0,
                    "preDelay": 80.0,
                    "pasteDispatch": 3.0,
                    "total": 10.0,
                },
            },
        },
    }
    validate_audio_diagnostics_payload(tauri_injection)

    invalid = copy.deepcopy(tauri_injection)
    invalid["textInjection"]["shellIpc"]["lastResponse"]["payload"]["preDelayMode"] = "fixed"
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(tauri_injection)
    invalid["textInjection"]["shellIpc"]["lastResponse"]["payload"].pop("deadlineMs")
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["runtimeImports"] = {}
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["textInjection"] = {**valid["textInjection"], "disabled": "no"}
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["microphone"] = {
        **valid["microphone"],
        "rustAudioFallbackCircuit": {
            **valid["microphone"]["rustAudioFallbackCircuit"],
            "open": "yes",
        },
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["microphone"] = {
        **valid["microphone"],
        "activeCapture": {
            **valid["microphone"]["activeCapture"],
            "sidecarKilledAfterTimeout": "no",
        },
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["microphone"] = {
        **valid["microphone"],
        "activeCapture": {
            **valid["microphone"]["activeCapture"],
            "source": {
                **valid["microphone"]["activeCapture"]["source"],
                "streamActive": "yes",
            },
        },
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["microphone"]["prewarm"]["activeCaptureResumeReadyCount"] = "1"
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["microphone"]["prewarm"]["lastStatus"]["sidecarPayload"] = {
        "prewarmId": "raw-prewarm-id"
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    idle_warning = copy.deepcopy(valid)
    idle_warning["watchdog"]["lastWarning"] = {
        **valid["watchdog"]["lastWarning"],
        "message": "Idle microphone watchdog recovered prewarm stream",
        "diagnostics": {
            **valid["microphone"]["prewarm"],
            "healthRestartCount": 1,
            "lastHealthError": "missingPrewarmSession",
        },
    }
    validate_audio_diagnostics_payload(idle_warning)

    invalid = copy.deepcopy(idle_warning)
    invalid["watchdog"]["lastWarning"]["diagnostics"]["lastStatus"]["raw"] = {
        "prewarmId": "raw-prewarm-id"
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = copy.deepcopy(valid)
    invalid["watchdog"] = {
        **valid["watchdog"],
        "lastWarning": {
            **valid["watchdog"]["lastWarning"],
            "diagnostics": {
                **valid["watchdog"]["lastWarning"]["diagnostics"],
                "healthRestartThrottleCount": "1",
            },
        },
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

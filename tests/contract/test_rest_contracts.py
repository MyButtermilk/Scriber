from __future__ import annotations

import asyncio

import pytest

from src.core.rest_contracts import (
    REST_API_VERSION,
    RESTContractError,
    validate_audio_diagnostics_payload,
    validate_frontend_ready_payload,
    validate_frontend_ready_request_payload,
    validate_health_payload,
    validate_runtime_payload,
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
        audio_diagnostics = ctl.get_audio_diagnostics()
    finally:
        loop.close()

    validate_runtime_payload(runtime)
    validate_health_payload(health)
    validate_frontend_ready_payload(frontend_ready)
    validate_audio_diagnostics_payload(audio_diagnostics)
    assert runtime["apiVersion"] == REST_API_VERSION
    assert health["apiVersion"] == REST_API_VERSION
    assert frontend_ready["apiVersion"] == REST_API_VERSION
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
                    "engine": "rust-prototype",
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

    invalid = dict(valid)
    invalid["runtimeImports"] = {}
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = dict(valid)
    invalid["textInjection"] = {**valid["textInjection"], "disabled": "no"}
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = dict(valid)
    invalid["microphone"] = {
        **valid["microphone"],
        "rustAudioFallbackCircuit": {
            **valid["microphone"]["rustAudioFallbackCircuit"],
            "open": "yes",
        },
    }
    with pytest.raises(RESTContractError):
        validate_audio_diagnostics_payload(invalid)

    invalid = dict(valid)
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

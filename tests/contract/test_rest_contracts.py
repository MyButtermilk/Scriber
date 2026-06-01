from __future__ import annotations

import asyncio

import pytest

from src.core.rest_contracts import (
    REST_API_VERSION,
    RESTContractError,
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
    finally:
        loop.close()

    validate_runtime_payload(runtime)
    validate_health_payload(health)
    assert runtime["apiVersion"] == REST_API_VERSION
    assert health["apiVersion"] == REST_API_VERSION
    assert health["workerVersion"] == runtime["workerVersion"]
    assert health["runtimeMode"] == runtime["runtimeMode"]
    assert health["pid"] == runtime["pid"]


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

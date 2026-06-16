import asyncio

import pytest

from src.core.ws_contracts import (
    WS_API_VERSION,
    WSContractError,
    audio_level_event,
    error_event,
    history_updated_event,
    input_warning_event,
    session_finished_event,
    session_started_event,
    state_event,
    status_event,
    transcript_event,
    transcribing_event,
    validate_event_payload,
    version_event_payload,
)
from src.web_api import ScriberWebController


def test_ws_event_builders_match_contract():
    payloads = [
        status_event("Listening", True, session_id="s1"),
        audio_level_event(0.12, session_id="s1"),
        input_warning_event(
            True,
            message="Mic level too low",
            code="mic_level_very_low",
            actions=[
                {
                    "id": "open_input_volume",
                    "label": "Open input volume",
                    "uri": "ms-settings:sound-defaultinputproperties",
                }
            ],
            session_id="s1",
        ),
        transcript_event("hello", True, session_id="s1"),
        error_event("failed", session_id="s1"),
        error_event(
            "Soniox rejected the API key. Check the Soniox key in Settings.",
            title="Soniox API key issue",
            provider="soniox",
            provider_label="Soniox",
            category="auth_invalid",
            code="401",
            retryable=False,
            session_id="s1",
        ),
        history_updated_event(),
        history_updated_event(
            transcript_id="t1",
            transcript_type="youtube",
            status="processing",
            step="Transcribing...",
            summary_status="pending",
            updated_at="2026-06-09T12:00:00",
            reason="progress",
        ),
        transcribing_event(session_id="s1"),
        session_started_event({"id": "s1"}, session_id="s1"),
        session_finished_event({"id": "s1"}, session_id="s1"),
    ]
    for payload in payloads:
        assert payload["apiVersion"] == WS_API_VERSION
        validate_event_payload(payload)


def test_ws_state_and_auxiliary_events_match_contract():
    payloads = [
        state_event(
            {
                "listening": False,
                "status": "Stopped",
                "inputWarning": "",
                "inputWarningCode": "",
                "inputWarningActions": [],
                "current": None,
                "sessionId": None,
                "backgroundProcessing": False,
                "recordingState": "idle",
                "transcribing": False,
            }
        ),
        version_event_payload({"type": "settings_updated"}),
        version_event_payload({"type": "microphones_updated", "devices": [], "favoriteMicRestored": False}),
        version_event_payload(
            {
                "type": "onnx_download_progress",
                "modelId": "nemo-parakeet",
                "progress": 10.0,
                "status": "downloading",
                "message": "Downloading",
            }
        ),
        version_event_payload({"type": "onnx_models_updated", "modelId": "nemo-parakeet"}),
        version_event_payload(
            {
                "type": "nemo_download_progress",
                "modelId": "parakeet",
                "progress": 100.0,
                "status": "ready",
                "message": "Ready",
            }
        ),
        version_event_payload({"type": "nemo_models_updated"}),
    ]

    for payload in payloads:
        validate_event_payload(payload)


def test_ws_contract_validation_rejects_invalid_payload():
    with pytest.raises(WSContractError):
        validate_event_payload({"type": "status", "status": "Listening"})
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "transcript", "text": "hello", "isFinal": "yes"}))
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "error", "message": 42}))
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "error", "message": "failed", "retryable": "yes"}))
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "input_warning", "active": True, "message": "m", "code": 1}))
    with pytest.raises(WSContractError):
        validate_event_payload(
            {
                "type": "input_warning",
                "apiVersion": WS_API_VERSION,
                "active": True,
                "message": "m",
                "actions": [{"id": "a", "label": "Open"}],
            }
        )
    with pytest.raises(WSContractError):
        validate_event_payload({"type": "history_updated", "apiVersion": "0"})
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "history_updated", "transcriptId": 123}))
    with pytest.raises(WSContractError):
        validate_event_payload(version_event_payload({"type": "unknown_event"}))


@pytest.mark.asyncio
async def test_controller_can_enforce_ws_contract(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_VALIDATE_WS_CONTRACTS", "1")
    ctl = ScriberWebController(loop)
    with pytest.raises(WSContractError):
        await ctl.broadcast({"type": "status", "status": "Listening"})  # missing listening: bool


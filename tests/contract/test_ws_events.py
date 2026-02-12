import asyncio

import pytest

from src.core.ws_contracts import (
    WSContractError,
    audio_level_event,
    error_event,
    history_updated_event,
    input_warning_event,
    session_finished_event,
    session_started_event,
    status_event,
    transcript_event,
    transcribing_event,
    validate_event_payload,
)
from src.web_api import ScriberWebController


def test_ws_event_builders_match_contract():
    payloads = [
        status_event("Listening", True, session_id="s1"),
        audio_level_event(0.12, session_id="s1"),
        input_warning_event(True, message="Mic level too low", session_id="s1"),
        transcript_event("hello", True, session_id="s1"),
        error_event("failed", session_id="s1"),
        history_updated_event(),
        transcribing_event(session_id="s1"),
        session_started_event({"id": "s1"}, session_id="s1"),
        session_finished_event({"id": "s1"}, session_id="s1"),
    ]
    for payload in payloads:
        validate_event_payload(payload)


def test_ws_contract_validation_rejects_invalid_payload():
    with pytest.raises(WSContractError):
        validate_event_payload({"type": "status", "status": "Listening"})
    with pytest.raises(WSContractError):
        validate_event_payload({"type": "transcript", "text": "hello", "isFinal": "yes"})
    with pytest.raises(WSContractError):
        validate_event_payload({"type": "error", "message": 42})


@pytest.mark.asyncio
async def test_controller_can_enforce_ws_contract(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_VALIDATE_WS_CONTRACTS", "1")
    ctl = ScriberWebController(loop)
    with pytest.raises(WSContractError):
        await ctl.broadcast({"type": "status", "status": "Listening"})  # missing listening: bool


from __future__ import annotations

from typing import Any


class WSContractError(ValueError):
    pass


def _optional_session(payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
    out = dict(payload)
    if session_id:
        out["sessionId"] = session_id
    return out


def status_event(status: str, listening: bool, *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session(
        {
            "type": "status",
            "status": str(status),
            "listening": bool(listening),
        },
        session_id,
    )


def audio_level_event(rms: float, *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "audio_level", "rms": float(rms)}, session_id)


def input_warning_event(
    active: bool,
    *,
    message: str = "",
    session_id: str | None = None,
) -> dict[str, Any]:
    return _optional_session(
        {
            "type": "input_warning",
            "active": bool(active),
            "message": str(message),
        },
        session_id,
    )


def transcript_event(text: str, is_final: bool, *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session(
        {
            "type": "transcript",
            "text": text if isinstance(text, str) else str(text),
            "isFinal": bool(is_final),
        },
        session_id,
    )


def error_event(message: str, *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "error", "message": str(message)}, session_id)


def history_updated_event() -> dict[str, Any]:
    return {"type": "history_updated"}


def transcribing_event(*, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "transcribing"}, session_id)


def session_started_event(session: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "session_started", "session": dict(session)}, session_id)


def session_finished_event(session: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "session_finished", "session": dict(session)}, session_id)


def validate_event_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise WSContractError("WebSocket payload must be a dict")
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise WSContractError("WebSocket payload requires non-empty string 'type'")

    if event_type == "status":
        if not isinstance(payload.get("status"), str):
            raise WSContractError("status event requires string 'status'")
        if not isinstance(payload.get("listening"), bool):
            raise WSContractError("status event requires bool 'listening'")
    elif event_type == "audio_level":
        if not isinstance(payload.get("rms"), (int, float)):
            raise WSContractError("audio_level event requires numeric 'rms'")
    elif event_type == "input_warning":
        if not isinstance(payload.get("active"), bool):
            raise WSContractError("input_warning event requires bool 'active'")
        if not isinstance(payload.get("message"), str):
            raise WSContractError("input_warning event requires string 'message'")
    elif event_type == "transcript":
        if not isinstance(payload.get("text"), str):
            raise WSContractError("transcript event requires string 'text'")
        if not isinstance(payload.get("isFinal"), bool):
            raise WSContractError("transcript event requires bool 'isFinal'")
    elif event_type == "error":
        if not isinstance(payload.get("message"), str):
            raise WSContractError("error event requires string 'message'")
    elif event_type in {"history_updated", "transcribing"}:
        pass
    elif event_type in {"session_started", "session_finished"}:
        if not isinstance(payload.get("session"), dict):
            raise WSContractError(f"{event_type} event requires object 'session'")

    session_id = payload.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise WSContractError("sessionId must be a string when present")


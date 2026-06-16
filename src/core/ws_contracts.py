from __future__ import annotations

from typing import Any


class WSContractError(ValueError):
    pass


WS_API_VERSION = "1"


def version_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("apiVersion", WS_API_VERSION)
    return out


def _optional_session(payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
    out = version_event_payload(payload)
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
    code: str = "",
    actions: list[dict[str, str]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "input_warning",
        "active": bool(active),
        "message": str(message),
    }
    normalized_code = str(code or "").strip()
    if normalized_code:
        payload["code"] = normalized_code
    if actions:
        normalized_actions: list[dict[str, str]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("id", "")).strip()
            label = str(action.get("label", "")).strip()
            uri = str(action.get("uri", "")).strip()
            if action_id and label and uri:
                normalized_actions.append(
                    {
                        "id": action_id,
                        "label": label,
                        "uri": uri,
                    }
                )
        if normalized_actions:
            payload["actions"] = normalized_actions
    return _optional_session(payload, session_id)


def transcript_event(text: str, is_final: bool, *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session(
        {
            "type": "transcript",
            "text": text if isinstance(text, str) else str(text),
            "isFinal": bool(is_final),
        },
        session_id,
    )


def error_event(
    message: str,
    *,
    title: str | None = None,
    provider: str | None = None,
    provider_label: str | None = None,
    category: str | None = None,
    code: str | None = None,
    retryable: bool | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "error", "message": str(message)}
    optional_strings = {
        "title": title,
        "provider": provider,
        "providerLabel": provider_label,
        "category": category,
        "code": code,
    }
    for field, value in optional_strings.items():
        normalized = str(value or "").strip()
        if normalized:
            payload[field] = normalized
    if retryable is not None:
        payload["retryable"] = bool(retryable)
    return _optional_session(payload, session_id)


def history_updated_event(
    *,
    transcript_id: str | None = None,
    transcript_type: str | None = None,
    status: str | None = None,
    step: str | None = None,
    summary_status: str | None = None,
    updated_at: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "history_updated"}
    if transcript_id:
        payload["transcriptId"] = str(transcript_id)
    if transcript_type:
        payload["transcriptType"] = str(transcript_type)
    if status:
        payload["status"] = str(status)
    if step:
        payload["step"] = str(step)
    if summary_status:
        payload["summaryStatus"] = str(summary_status)
    if updated_at:
        payload["updatedAt"] = str(updated_at)
    if reason:
        payload["reason"] = str(reason)
    return version_event_payload(payload)


def transcribing_event(*, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "transcribing"}, session_id)


def session_started_event(session: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "session_started", "session": dict(session)}, session_id)


def session_finished_event(session: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    return _optional_session({"type": "session_finished", "session": dict(session)}, session_id)


def state_event(state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["type"] = "state"
    return version_event_payload(payload)


def _require_string(payload: dict[str, Any], field: str, event_type: str) -> None:
    if not isinstance(payload.get(field), str):
        raise WSContractError(f"{event_type} event requires string '{field}'")


def _require_bool(payload: dict[str, Any], field: str, event_type: str) -> None:
    if not isinstance(payload.get(field), bool):
        raise WSContractError(f"{event_type} event requires bool '{field}'")


def _require_number(payload: dict[str, Any], field: str, event_type: str) -> None:
    if not isinstance(payload.get(field), (int, float)):
        raise WSContractError(f"{event_type} event requires numeric '{field}'")


def _validate_model_progress(payload: dict[str, Any], event_type: str) -> None:
    _require_string(payload, "modelId", event_type)
    _require_number(payload, "progress", event_type)
    _require_string(payload, "status", event_type)
    message = payload.get("message")
    if message is not None and not isinstance(message, str):
        raise WSContractError(f"{event_type} event requires string 'message' when present")


def validate_event_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise WSContractError("WebSocket payload must be a dict")
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise WSContractError("WebSocket payload requires non-empty string 'type'")
    api_version = payload.get("apiVersion")
    if api_version != WS_API_VERSION:
        raise WSContractError(f"WebSocket payload requires apiVersion '{WS_API_VERSION}'")

    if event_type == "state":
        _require_bool(payload, "listening", event_type)
        _require_string(payload, "status", event_type)
        _require_bool(payload, "backgroundProcessing", event_type)
        _require_string(payload, "recordingState", event_type)
        _require_bool(payload, "transcribing", event_type)
    elif event_type == "status":
        _require_string(payload, "status", event_type)
        _require_bool(payload, "listening", event_type)
    elif event_type == "audio_level":
        _require_number(payload, "rms", event_type)
    elif event_type == "input_warning":
        _require_bool(payload, "active", event_type)
        _require_string(payload, "message", event_type)
        code = payload.get("code")
        if code is not None and not isinstance(code, str):
            raise WSContractError("input_warning event requires string 'code' when present")
        actions = payload.get("actions")
        if actions is not None:
            if not isinstance(actions, list):
                raise WSContractError("input_warning event requires list 'actions' when present")
            for action in actions:
                if not isinstance(action, dict):
                    raise WSContractError("input_warning actions must be objects")
                if not isinstance(action.get("id"), str):
                    raise WSContractError("input_warning action requires string 'id'")
                if not isinstance(action.get("label"), str):
                    raise WSContractError("input_warning action requires string 'label'")
                if not isinstance(action.get("uri"), str):
                    raise WSContractError("input_warning action requires string 'uri'")
    elif event_type == "transcript":
        _require_string(payload, "text", event_type)
        _require_bool(payload, "isFinal", event_type)
    elif event_type == "error":
        _require_string(payload, "message", event_type)
        for field in ("title", "provider", "providerLabel", "category", "code"):
            if field in payload and not isinstance(payload.get(field), str):
                raise WSContractError(f"error event requires string '{field}' when present")
        if "retryable" in payload and not isinstance(payload.get("retryable"), bool):
            raise WSContractError("error event requires bool 'retryable' when present")
    elif event_type == "history_updated":
        for field in ("transcriptId", "transcriptType", "status", "step", "summaryStatus", "updatedAt", "reason"):
            if field in payload and not isinstance(payload.get(field), str):
                raise WSContractError(f"history_updated event requires string '{field}' when present")
    elif event_type in {"settings_updated", "transcribing", "nemo_models_updated"}:
        pass
    elif event_type in {"session_started", "session_finished"}:
        if not isinstance(payload.get("session"), dict):
            raise WSContractError(f"{event_type} event requires object 'session'")
    elif event_type == "microphones_updated":
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise WSContractError("microphones_updated event requires list 'devices'")
        if not isinstance(payload.get("favoriteMicRestored"), bool):
            raise WSContractError("microphones_updated event requires bool 'favoriteMicRestored'")
    elif event_type in {"onnx_download_progress", "nemo_download_progress"}:
        _validate_model_progress(payload, event_type)
    elif event_type == "onnx_models_updated":
        _require_string(payload, "modelId", event_type)
    else:
        raise WSContractError(f"Unknown WebSocket event type: {event_type}")

    session_id = payload.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise WSContractError("sessionId must be a string when present")


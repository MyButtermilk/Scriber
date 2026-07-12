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


def meeting_state_event(meeting: dict[str, Any]) -> dict[str, Any]:
    return version_event_payload({"type": "meeting_state", "meeting": dict(meeting)})


def meeting_segment_event(meeting_id: str, segment: dict[str, Any]) -> dict[str, Any]:
    segment_payload = dict(segment)
    start_ms = segment_payload.get("startMs")
    end_ms = segment_payload.get("endMs")
    if (
        "durationMs" not in segment_payload
        and isinstance(start_ms, (int, float))
        and not isinstance(start_ms, bool)
        and isinstance(end_ms, (int, float))
        and not isinstance(end_ms, bool)
    ):
        segment_payload["durationMs"] = max(0, end_ms - start_ms)
    return version_event_payload(
        {"type": "meeting_segment", "meetingId": str(meeting_id), "segment": segment_payload}
    )


def meeting_checkpoint_event(
    meeting_id: str, checkpoint: dict[str, Any]
) -> dict[str, Any]:
    """Publish redacted checkpoint metadata without transcript snapshot text."""
    return version_event_payload(
        {
            "type": "meeting_checkpoint",
            "meetingId": str(meeting_id),
            "checkpoint": {
                "id": str(checkpoint.get("id", "")),
                "meetingId": str(checkpoint.get("meetingId") or meeting_id),
                "sequence": int(checkpoint.get("sequence", 0)),
                "cutoffMs": max(0, int(checkpoint.get("cutoffMs", 0))),
                "segmentCount": max(0, int(checkpoint.get("segmentCount", 0))),
                "sources": [str(value) for value in checkpoint.get("sources", [])],
                "frontiers": dict(checkpoint.get("frontiers", {})),
                "commitOrdinal": max(0, int(checkpoint.get("commitOrdinal", 0))),
                "snapshotSha256": str(checkpoint.get("snapshotSha256", "")),
                "createdAt": str(checkpoint.get("createdAt", "")),
                "updatedAt": str(checkpoint.get("updatedAt", "")),
            },
        }
    )


def meeting_transcript_edited_event(
    meeting_id: str,
    segment: dict[str, Any],
    *,
    transcript_edit_version: int,
    outputs_stale: bool,
) -> dict[str, Any]:
    return version_event_payload(
        {
            "type": "meeting_transcript_edited",
            "meetingId": str(meeting_id),
            "segment": dict(segment),
            "transcriptEditVersion": max(0, int(transcript_edit_version)),
            "outputsStale": bool(outputs_stale),
        }
    )


def meeting_note_event(meeting_id: str, note: dict[str, Any]) -> dict[str, Any]:
    return version_event_payload(
        {"type": "meeting_note", "meetingId": str(meeting_id), "note": dict(note)}
    )


def meeting_audio_level_event(meeting_id: str, source: str, rms: float) -> dict[str, Any]:
    return version_event_payload(
        {"type": "meeting_audio_level", "meetingId": str(meeting_id), "source": str(source), "rms": float(rms)}
    )


def meeting_live_status_event(
    meeting_id: str, source: str, status: str, reconnect_count: int
) -> dict[str, Any]:
    return version_event_payload(
        {
            "type": "meeting_live_status",
            "meetingId": str(meeting_id),
            "source": str(source),
            "status": str(status),
            "reconnectCount": max(0, int(reconnect_count)),
        }
    )


def meeting_progress_event(meeting_id: str, phase: str, progress: float, status: str) -> dict[str, Any]:
    event_type = "meeting_analysis_progress" if phase == "analysis" else "meeting_finalize_progress"
    return version_event_payload(
        {
            "type": event_type,
            "meetingId": str(meeting_id),
            "progress": max(0.0, min(1.0, float(progress))),
            "status": str(status),
        }
    )


def meeting_import_progress_event(
    import_id: str,
    phase: str,
    progress: float,
    status: str,
    *,
    received_bytes: int = 0,
    expected_bytes: int | None = None,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "meeting_import_progress",
        "importId": str(import_id),
        "phase": str(phase),
        "progress": max(0.0, min(1.0, float(progress))),
        "status": str(status)[:160],
        "receivedBytes": max(0, int(received_bytes)),
    }
    if expected_bytes is not None:
        payload["expectedBytes"] = max(0, int(expected_bytes))
    if meeting_id:
        payload["meetingId"] = str(meeting_id)
    return version_event_payload(payload)


def meeting_chat_delta_event(meeting_id: str, thread_id: str, delta: str) -> dict[str, Any]:
    return version_event_payload(
        {
            "type": "meeting_chat_delta",
            "meetingId": str(meeting_id),
            "threadId": str(thread_id),
            "delta": str(delta),
        }
    )


def meeting_delivery_updated_event(meeting_id: str, delivery: dict[str, Any]) -> dict[str, Any]:
    return version_event_payload(
        {"type": "meeting_delivery_updated", "meetingId": str(meeting_id), "delivery": dict(delivery)}
    )


def meeting_detected_event(
    detection_id: str,
    label: str,
    *,
    source: str,
    meeting_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "meeting_detected",
        "detectionId": str(detection_id),
        "label": str(label),
        "source": str(source),
    }
    if meeting_id:
        payload["meetingId"] = str(meeting_id)
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
    elif event_type in {"settings_updated", "transcribing"}:
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
    elif event_type == "onnx_download_progress":
        _validate_model_progress(payload, event_type)
    elif event_type == "onnx_models_updated":
        _require_string(payload, "modelId", event_type)
    elif event_type == "meeting_state":
        meeting = payload.get("meeting")
        if not isinstance(meeting, dict):
            raise WSContractError("meeting_state event requires object 'meeting'")
        _require_string(meeting, "id", event_type)
        _require_string(meeting, "state", event_type)
    elif event_type in {"meeting_segment", "meeting_note", "meeting_checkpoint"}:
        _require_string(payload, "meetingId", event_type)
        field = (
            "segment" if event_type == "meeting_segment"
            else "checkpoint" if event_type == "meeting_checkpoint"
            else "note"
        )
        if not isinstance(payload.get(field), dict):
            raise WSContractError(f"{event_type} event requires object '{field}'")
        if event_type == "meeting_segment":
            segment = payload["segment"]
            for string_field in ("id", "revision", "source", "speakerLabel", "text"):
                _require_string(segment, string_field, event_type)
            for number_field in ("startMs", "endMs", "durationMs", "sequence"):
                _require_number(segment, number_field, event_type)
            if segment["endMs"] < segment["startMs"]:
                raise WSContractError("meeting_segment event endMs must not precede startMs")
            if segment["durationMs"] != segment["endMs"] - segment["startMs"]:
                raise WSContractError("meeting_segment event durationMs must equal endMs - startMs")
            _require_bool(segment, "isFinal", event_type)
        elif event_type == "meeting_checkpoint":
            checkpoint = payload["checkpoint"]
            for string_field in ("id", "meetingId", "snapshotSha256", "createdAt", "updatedAt"):
                _require_string(checkpoint, string_field, event_type)
            for number_field in ("sequence", "cutoffMs", "segmentCount", "commitOrdinal"):
                _require_number(checkpoint, number_field, event_type)
            if not isinstance(checkpoint.get("sources"), list):
                raise WSContractError("meeting_checkpoint event requires list 'sources'")
            if not isinstance(checkpoint.get("frontiers"), dict):
                raise WSContractError("meeting_checkpoint event requires object 'frontiers'")
    elif event_type == "meeting_audio_level":
        _require_string(payload, "meetingId", event_type)
        _require_string(payload, "source", event_type)
        _require_number(payload, "rms", event_type)
    elif event_type == "meeting_transcript_edited":
        _require_string(payload, "meetingId", event_type)
        if not isinstance(payload.get("segment"), dict):
            raise WSContractError("meeting_transcript_edited event requires object 'segment'")
        segment = payload["segment"]
        for string_field in ("id", "revision", "source", "speakerLabel", "text"):
            _require_string(segment, string_field, event_type)
        for number_field in ("startMs", "endMs", "durationMs", "sequence"):
            _require_number(segment, number_field, event_type)
        if segment["endMs"] < segment["startMs"]:
            raise WSContractError("meeting_transcript_edited event endMs must not precede startMs")
        if segment["durationMs"] != segment["endMs"] - segment["startMs"]:
            raise WSContractError(
                "meeting_transcript_edited event durationMs must equal endMs - startMs"
            )
        _require_bool(segment, "isFinal", event_type)
        for number_field in ("transcriptEditVersion",):
            _require_number(payload, number_field, event_type)
        _require_bool(payload, "outputsStale", event_type)
    elif event_type == "meeting_live_status":
        _require_string(payload, "meetingId", event_type)
        _require_string(payload, "source", event_type)
        _require_string(payload, "status", event_type)
        status = payload["status"]
        if status not in {"reconnecting", "recovered", "degraded"}:
            raise WSContractError(
                "meeting_live_status event status must be 'reconnecting', 'recovered', or 'degraded'"
            )
        reconnect_count = payload.get("reconnectCount")
        if not isinstance(reconnect_count, int) or isinstance(reconnect_count, bool) or reconnect_count < 0:
            raise WSContractError(
                "meeting_live_status event requires non-negative int 'reconnectCount'"
            )
    elif event_type in {"meeting_finalize_progress", "meeting_analysis_progress"}:
        _require_string(payload, "meetingId", event_type)
        _require_number(payload, "progress", event_type)
        _require_string(payload, "status", event_type)
    elif event_type == "meeting_import_progress":
        for field in ("importId", "phase", "status"):
            _require_string(payload, field, event_type)
        _require_number(payload, "progress", event_type)
        if not 0 <= payload["progress"] <= 1:
            raise WSContractError("meeting_import_progress progress must be between 0 and 1")
        received = payload.get("receivedBytes")
        if not isinstance(received, int) or isinstance(received, bool) or received < 0:
            raise WSContractError("meeting_import_progress requires non-negative int 'receivedBytes'")
        if "expectedBytes" in payload:
            expected = payload["expectedBytes"]
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise WSContractError("meeting_import_progress expectedBytes must be non-negative")
        if "meetingId" in payload:
            _require_string(payload, "meetingId", event_type)
    elif event_type == "meeting_detected":
        _require_string(payload, "detectionId", event_type)
        _require_string(payload, "label", event_type)
        _require_string(payload, "source", event_type)
        if "meetingId" in payload:
            _require_string(payload, "meetingId", event_type)
    elif event_type == "meeting_chat_delta":
        _require_string(payload, "meetingId", event_type)
        _require_string(payload, "threadId", event_type)
        _require_string(payload, "delta", event_type)
    elif event_type == "meeting_delivery_updated":
        _require_string(payload, "meetingId", event_type)
        if not isinstance(payload.get("delivery"), dict):
            raise WSContractError("meeting_delivery_updated event requires object 'delivery'")
    else:
        raise WSContractError(f"Unknown WebSocket event type: {event_type}")

    session_id = payload.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise WSContractError("sessionId must be a string when present")


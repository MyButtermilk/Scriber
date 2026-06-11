from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any

SHELL_IPC_PIPE_ENV = "SCRIBER_SHELL_IPC_PIPE"
SHELL_IPC_TOKEN_ENV = "SCRIBER_SHELL_IPC_TOKEN"
SHELL_IPC_API_VERSION_ENV = "SCRIBER_SHELL_IPC_API_VERSION"
DEFAULT_API_VERSION = "1"
DEFAULT_TIMEOUT_SECONDS = 0.75
_PIPE_NAME_PATTERN = re.compile(
    r"(?:\\\\){1,2}\.(?:\\){1,2}pipe(?:\\){1,2}scriber-shell-[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)

_lock = threading.Lock()
_last_command: str | None = None
_last_error: str | None = None
_last_error_code: str | None = None
_last_fallback_reason: str | None = None
_last_success: bool | None = None
_last_command_at: float | None = None
_last_response_summary: dict[str, Any] | None = None


def available() -> bool:
    return (
        sys.platform.startswith("win")
        and bool(_configured_pipe_name())
        and bool(_configured_token())
    )


def diagnostic_snapshot() -> dict[str, Any]:
    pipe_name = _configured_pipe_name()
    token = _configured_token()
    api_version = _configured_api_version()
    with _lock:
        last_command = _last_command
        last_error = _last_error
        last_error_code = _last_error_code
        last_fallback_reason = _last_fallback_reason
        last_success = _last_success
        last_command_at = _last_command_at
        last_response_summary = dict(_last_response_summary) if _last_response_summary else None
    return {
        "available": available(),
        "pipeConfigured": bool(pipe_name),
        "tokenConfigured": bool(token),
        "apiVersion": api_version,
        "pipeNameHash": _hash_pipe_name(pipe_name),
        "lastCommand": last_command,
        "lastSuccess": last_success,
        "lastError": last_error,
        "lastErrorCode": last_error_code,
        "lastFallbackReason": last_fallback_reason,
        "lastResponse": last_response_summary,
        "lastCommandAgoSeconds": (
            max(0.0, time.monotonic() - last_command_at)
            if last_command_at is not None
            else None
        ),
    }


def call_shell_ipc(
    command: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    cleaned_command = (command or "").strip()
    if not cleaned_command:
        _record_result(
            "",
            False,
            "empty command",
            error_code="invalidCommand",
            fallback_reason="empty command",
        )
        return _failure("invalidCommand", "empty command")

    pipe_name = _configured_pipe_name()
    token = _configured_token()
    api_version = _configured_api_version()
    if not available() or not pipe_name or not token:
        _record_result(
            cleaned_command,
            False,
            "shell IPC is not available",
            error_code="unavailable",
            fallback_reason="shell IPC is not available",
        )
        return _failure("unavailable", "shell IPC is not available")

    request = {
        "apiVersion": api_version,
        "requestId": uuid.uuid4().hex,
        "command": cleaned_command,
        "token": token,
        "payload": payload or {},
    }
    request_line = json.dumps(request, separators=(",", ":")) + "\n"
    try:
        response_line = _call_shell_ipc_windows(
            pipe_name,
            request_line,
            max(0.05, float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)),
        )
        response = json.loads(response_line)
        if not isinstance(response, dict):
            raise ValueError("shell IPC response was not an object")
        if response.get("apiVersion") != api_version:
            raise ValueError("shell IPC response apiVersion mismatch")
        if response.get("requestId") != request["requestId"]:
            raise ValueError("shell IPC response requestId mismatch")
        if not isinstance(response.get("success"), bool):
            raise ValueError("shell IPC response success must be bool")
        success = bool(response.get("success"))
        error_code = response.get("errorCode")
        fallback_reason = response.get("fallbackReason")
        if error_code is not None or fallback_reason is not None:
            response = dict(response)
            response["errorCode"] = _safe_optional_string(error_code)
            response["fallbackReason"] = _safe_optional_string(fallback_reason, max_len=240)
        error = None if success else str(error_code or "shell IPC failed")
        _record_result(
            cleaned_command,
            success,
            error,
            error_code=_safe_optional_string(error_code),
            fallback_reason=_safe_optional_string(fallback_reason),
            response=response,
        )
        return response
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _record_result(
            cleaned_command,
            False,
            message,
            error_code="transportError",
            fallback_reason=message,
        )
        return _failure("transportError", message)


def _configured_pipe_name() -> str:
    return os.getenv(SHELL_IPC_PIPE_ENV, "").strip()


def _configured_token() -> str:
    return os.getenv(SHELL_IPC_TOKEN_ENV, "").strip()


def _configured_api_version() -> str:
    return os.getenv(SHELL_IPC_API_VERSION_ENV, DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION


def _hash_pipe_name(pipe_name: str) -> str | None:
    if not pipe_name:
        return None
    return hashlib.sha256(pipe_name.encode("utf-8", errors="replace")).hexdigest()[:16]


def _failure(error_code: str, fallback_reason: str) -> dict[str, Any]:
    safe_fallback_reason = _safe_optional_string(fallback_reason, max_len=240) or ""
    return {
        "apiVersion": _configured_api_version(),
        "requestId": None,
        "success": False,
        "errorCode": error_code,
        "fallbackReason": safe_fallback_reason,
        "timingsMs": {"total": 0.0},
        "payload": {},
    }


def record_command_diagnostic(
    command: str,
    success: bool,
    *,
    error_code: str | None = None,
    fallback_reason: str | None = None,
    response: dict[str, Any] | None = None,
) -> None:
    error = None if success else " ".join(
        part for part in (error_code, fallback_reason) if part
    ) or "command failed"
    _record_result(
        command,
        success,
        error,
        error_code=error_code,
        fallback_reason=fallback_reason,
        response=response,
    )


def _record_result(
    command: str,
    success: bool,
    error: str | None,
    *,
    error_code: str | None = None,
    fallback_reason: str | None = None,
    response: dict[str, Any] | None = None,
) -> None:
    global _last_command, _last_error, _last_error_code, _last_fallback_reason
    global _last_success, _last_command_at, _last_response_summary
    with _lock:
        _last_command = command
        _last_error = _safe_optional_string(error, max_len=240)
        _last_error_code = _safe_optional_string(error_code)
        _last_fallback_reason = _safe_optional_string(fallback_reason)
        _last_success = success
        _last_command_at = time.monotonic()
        _last_response_summary = _response_summary(
            command,
            response,
            success=success,
            error_code=error_code,
            fallback_reason=fallback_reason,
        )


def _response_summary(
    command: str,
    response: dict[str, Any] | None,
    *,
    success: bool,
    error_code: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    response = response if isinstance(response, dict) else {}
    summary: dict[str, Any] = {
        "success": bool(success),
        "errorCode": _safe_optional_string(error_code if error_code is not None else response.get("errorCode")),
        "fallbackReason": _safe_optional_string(
            fallback_reason if fallback_reason is not None else response.get("fallbackReason"),
            max_len=240,
        ),
    }
    timings = _numeric_mapping(response.get("timingsMs"), {"total"})
    if timings:
        summary["timingsMs"] = timings
    if command == "injectText":
        payload = response.get("payload")
        summary["payload"] = _inject_text_payload_summary(payload if isinstance(payload, dict) else {})
    return summary


def _inject_text_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("method", "dispatch", "preDelayMode"):
        value = _safe_optional_string(payload.get(key), max_len=48)
        if value is not None:
            summary[key] = value
    requested_pre_delay_ms = payload.get("requestedPreDelayMs")
    if isinstance(requested_pre_delay_ms, (int, float)) and not isinstance(
        requested_pre_delay_ms, bool
    ):
        summary["requestedPreDelayMs"] = float(requested_pre_delay_ms)
    markers = payload.get("markers")
    if isinstance(markers, list):
        summary["markers"] = [
            str(marker)[:48]
            for marker in markers
            if isinstance(marker, str) and marker in {"clipboard_set", "paste"}
        ]
    for key in ("restoreScheduled", "foregroundChanged"):
        value = payload.get(key)
        if isinstance(value, bool):
            summary[key] = value
    restore = _restore_summary(payload.get("restore"))
    if restore:
        summary["restore"] = restore
    for key in ("foregroundBefore", "foregroundAfter"):
        foreground = _foreground_summary(payload.get(key))
        if foreground:
            summary[key] = foreground
    timings = _numeric_mapping(
        payload.get("timingsMs"),
        {"clipboardRead", "clipboardSet", "preDelay", "pasteDispatch", "total"},
    )
    if timings:
        summary["timingsMs"] = timings
    return summary


def _restore_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("scheduled", "attempted", "succeeded"):
        item = value.get(key)
        if isinstance(item, bool) or item is None:
            summary[key] = item
    for key in ("skippedReason", "errorCode"):
        item = _safe_optional_string(value.get(key), max_len=80)
        if item is not None:
            summary[key] = item
    return summary


def _foreground_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    available = value.get("available")
    if isinstance(available, bool):
        summary["available"] = available
    for key in ("windowHash", "titleHash", "processIdHash"):
        item = _safe_optional_string(value.get(key), max_len=32)
        if item is not None:
            summary[key] = item
    return summary


def _numeric_mapping(value: Any, allowed_keys: set[str]) -> dict[str, float | None]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, float | None] = {}
    for key in allowed_keys:
        item = value.get(key)
        if item is None:
            summary[key] = None
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            summary[key] = float(item)
    return summary


def _safe_optional_string(value: Any, *, max_len: int = 160) -> str | None:
    if value is None:
        return None
    return _redact_sensitive_text(str(value).replace("\x00", ""))[:max_len]


def _redact_sensitive_text(value: str) -> str:
    redacted = value
    pipe_name = _configured_pipe_name()
    if pipe_name:
        for variant in {pipe_name, pipe_name.replace("\\", "\\\\")}:
            if variant:
                redacted = redacted.replace(variant, "[REDACTED_PIPE]")
    redacted = _PIPE_NAME_PATTERN.sub("[REDACTED_PIPE]", redacted)
    token = _configured_token()
    if token:
        redacted = redacted.replace(token, "[REDACTED_TOKEN]")
    return redacted


def _call_shell_ipc_windows(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
    result_queue: queue.Queue[tuple[bool, str]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((True, _send_request_over_pipe(pipe_name, request_line)), block=False)
        except Exception as exc:
            result_queue.put((False, f"{type(exc).__name__}: {exc}"), block=False)

    thread = threading.Thread(target=worker, name="scriber-shell-ipc", daemon=True)
    thread.start()
    try:
        ok, result = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise TimeoutError(f"shell IPC timed out after {timeout_seconds:.3f}s") from exc
    if not ok:
        raise RuntimeError(result)
    return result


def _send_request_over_pipe(pipe_name: str, request_line: str) -> str:
    with open(pipe_name, "r+b", buffering=0) as pipe:
        pipe.write(request_line.encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = pipe.read(1)
            if not chunk:
                break
            if chunk == b"\n":
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


def _reset_diagnostics_for_tests() -> None:
    global _last_command, _last_error, _last_error_code, _last_fallback_reason
    global _last_success, _last_command_at, _last_response_summary
    with _lock:
        _last_command = None
        _last_error = None
        _last_error_code = None
        _last_fallback_reason = None
        _last_success = None
        _last_command_at = None
        _last_response_summary = None

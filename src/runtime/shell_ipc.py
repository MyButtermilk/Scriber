from __future__ import annotations

import hashlib
import json
import os
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
MAX_RESPONSE_BYTES = 512 * 1024
_RESPONSE_ACK_TYPE = "responseAck"
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

# Commands that mutate the same shell-owned resource must preserve caller order,
# but unrelated capabilities must never share one process-wide transport lock.
# In particular, a slow Outlook request must not delay microphone or overlay
# work.  The actual named-pipe transport remains fully concurrent.
_command_domain_locks = {
    "audio": threading.Lock(),
    "inject": threading.Lock(),
    "outlook": threading.Lock(),
    "overlay": threading.Lock(),
}


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
        response_line = _call_shell_ipc_ordered(
            cleaned_command,
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
    deadline_ms = payload.get("deadlineMs")
    if isinstance(deadline_ms, (int, float)) and not isinstance(deadline_ms, bool):
        summary["deadlineMs"] = float(deadline_ms)
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
    restore_kind = _safe_optional_string(value.get("restoreKind"), max_len=32)
    if restore_kind is not None:
        summary["restoreKind"] = restore_kind
    for key in ("formatCount", "unsupportedFormatCount", "totalBytes"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            summary[key] = float(item)
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


def _command_serialization_domain(command: str) -> str | None:
    """Return the narrow shell-ownership domain for an ordered command."""
    normalized = str(command or "").strip().lower()
    if normalized == "injecttext":
        return "inject"
    if normalized.startswith("overlay"):
        return "overlay"
    if normalized.startswith("outlook"):
        return "outlook"
    if normalized.startswith(("audiocapture", "audioprewarm", "audiomeeting")):
        return "audio"
    if normalized == "audioprobe":
        return "audio"
    return None


def _call_shell_ipc_ordered(
    command: str,
    pipe_name: str,
    request_line: str,
    timeout_seconds: float,
) -> str:
    """Serialize only commands that mutate the same native resource domain."""
    timeout_budget = max(0.05, float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
    domain = _command_serialization_domain(command)
    if domain is None:
        return _call_shell_ipc_windows(pipe_name, request_line, timeout_budget)

    domain_lock = _command_domain_locks[domain]
    deadline = time.monotonic() + timeout_budget
    acquired = domain_lock.acquire(timeout=timeout_budget)
    if not acquired:
        raise TimeoutError(
            f"shell IPC {domain} command queue timed out after {timeout_budget:.3f}s"
        )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"shell IPC {domain} command queue timed out after {timeout_budget:.3f}s"
            )
        return _call_shell_ipc_windows(
            pipe_name,
            request_line,
            max(0.05, remaining),
        )
    finally:
        domain_lock.release()


def _call_shell_ipc_windows(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
    """Run one independently bounded request against a multi-instance pipe.

    The transport used to create an extra synchronous worker and serialize every
    command behind one process-wide lock.  If Windows left that worker blocked in
    pipe I/O after its caller timed out, the lock survived and starved overlay,
    prewarm, capture, and Outlook commands together.  The Windows implementation
    below uses overlapped I/O with CancelIoEx, so each request owns its timeout and
    no abandoned worker or global transport lock can wedge later calls.
    """
    try:
        return _send_request_over_pipe(
            pipe_name,
            request_line,
            max(0.05, float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)),
        )
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def _send_request_over_pipe(
    pipe_name: str,
    request_line: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    if os.name == "nt":
        return _send_request_over_pipe_windows(pipe_name, request_line, timeout_seconds)
    return _send_request_over_pipe_file(pipe_name, request_line)


def _send_request_over_pipe_file(pipe_name: str, request_line: str) -> str:
    with open(pipe_name, "r+b", buffering=0) as pipe:
        pipe.write(request_line.encode("utf-8"))
        chunks: list[bytes] = []
        newline_received = False
        while True:
            chunk = pipe.read(1)
            if not chunk:
                break
            if chunk == b"\n":
                newline_received = True
                break
            chunks.append(chunk)
        response_line = b"".join(chunks).decode("utf-8", errors="replace")
        acknowledgement = _response_ack_line(
            request_line,
            response_line,
            newline_received=newline_received,
        )
        if acknowledgement is not None:
            pipe.write(acknowledgement.encode("utf-8"))
        return response_line


def _response_ack_line(
    request_line: str,
    response_line: str,
    *,
    newline_received: bool,
) -> str:
    """Build the request-bound ACK required by every complete shell response.

    A closed pipe is ambiguous: it can mean either that the caller consumed the
    response or that a timed-out OVERLAPPED read was cancelled.  Rust therefore
    retains cleanup ownership for successful audio starts until this request-bound
    acknowledgement arrives. Other commands use the same acknowledgement to keep
    the server from reclaiming a pipe while Python is still consuming the response,
    but never gain audio rollback semantics. The acknowledgement is created only
    after the complete newline-delimited response passes the envelope checks.
    """

    request = json.loads(request_line)
    if not isinstance(request, dict):
        raise ValueError("shell IPC request was not an object")
    if not newline_received:
        raise ValueError("shell IPC response was not newline delimited")

    api_version = request.get("apiVersion")
    request_id = request.get("requestId")
    if (
        not isinstance(api_version, str)
        or not api_version
        or len(api_version.encode("utf-8")) > 16
    ):
        raise ValueError("shell IPC request apiVersion was invalid")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id.encode("utf-8")) > 128
    ):
        raise ValueError("shell IPC request requestId was invalid")

    response = json.loads(response_line)
    if not isinstance(response, dict):
        raise ValueError("shell IPC response was not an object")
    if response.get("apiVersion") != api_version:
        raise ValueError("shell IPC response apiVersion mismatch")
    if response.get("requestId") != request_id:
        raise ValueError("shell IPC response requestId mismatch")
    if not isinstance(response.get("success"), bool):
        raise ValueError("shell IPC response success must be bool")

    return (
        json.dumps(
            {
                "apiVersion": api_version,
                "requestId": request_id,
                "type": _RESPONSE_ACK_TYPE,
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def _send_request_over_pipe_windows(
    pipe_name: str,
    request_line: str,
    timeout_seconds: float,
) -> str:
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    generic_read = 0x80000000
    generic_write = 0x40000000
    open_existing = 3
    file_attribute_normal = 0x80
    file_flag_overlapped = 0x40000000
    invalid_handle_value = ctypes.c_void_p(-1).value
    error_pipe_busy = 231
    error_io_pending = 997
    error_more_data = 234
    wait_object_0 = 0
    wait_timeout = 258
    wait_failed = 0xFFFFFFFF
    deadline = time.monotonic() + max(0.05, timeout_seconds)

    def remaining_ms() -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        return max(1, min(0xFFFFFFFE, int(remaining * 1000)))

    def wait_for_io(
        handle: int,
        overlapped: OVERLAPPED,
        operation: str,
    ) -> int:
        wait_ms = remaining_ms()
        if wait_ms <= 0:
            raise TimeoutError(
                f"shell IPC {operation} timed out after {timeout_seconds:.3f}s"
            )
        wait_result = kernel32.WaitForSingleObject(
            wintypes.HANDLE(overlapped.hEvent),
            wintypes.DWORD(wait_ms),
        )
        if wait_result == wait_timeout:
            raise TimeoutError(
                f"shell IPC {operation} timed out after {timeout_seconds:.3f}s"
            )
        if wait_result != wait_object_0:
            error = ctypes.get_last_error() if wait_result == wait_failed else int(wait_result)
            raise OSError(error, f"WaitForSingleObject failed during {operation}")
        transferred = wintypes.DWORD(0)
        if not kernel32.GetOverlappedResult(
            wintypes.HANDLE(handle),
            ctypes.byref(overlapped),
            ctypes.byref(transferred),
            wintypes.BOOL(False),
        ):
            error = ctypes.get_last_error()
            if error == error_more_data:
                raise ValueError("shell IPC response exceeded maximum size")
            raise OSError(error, f"GetOverlappedResult failed during {operation}")
        return int(transferred.value)

    def cancel_and_drain_io(handle: int, overlapped: OVERLAPPED) -> None:
        """Cancel one pending operation and retain its storage until completion.

        CancelIoEx only marks an operation for cancellation.  The OVERLAPPED
        structure, its event, and the associated buffer must remain alive until
        GetOverlappedResult observes final completion.  Calling it even when
        CancelIoEx reports ERROR_NOT_FOUND also closes the completion race where
        the operation finished between the timeout and cancellation request.
        """
        try:
            kernel32.CancelIoEx(
                wintypes.HANDLE(handle),
                ctypes.byref(overlapped),
            )
        finally:
            transferred = wintypes.DWORD(0)
            kernel32.GetOverlappedResult(
                wintypes.HANDLE(handle),
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                wintypes.BOOL(True),
            )

    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetOverlappedResult.restype = wintypes.BOOL
    kernel32.CancelIoEx.restype = wintypes.BOOL
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = invalid_handle_value
    while handle == invalid_handle_value:
        timeout_ms = remaining_ms()
        if timeout_ms <= 0 or not kernel32.WaitNamedPipeW(
            wintypes.LPCWSTR(pipe_name), wintypes.DWORD(timeout_ms)
        ):
            raise OSError(ctypes.get_last_error(), "WaitNamedPipeW failed")

        handle = kernel32.CreateFileW(
            wintypes.LPCWSTR(pipe_name),
            wintypes.DWORD(generic_read | generic_write),
            wintypes.DWORD(0),
            None,
            wintypes.DWORD(open_existing),
            wintypes.DWORD(file_attribute_normal | file_flag_overlapped),
            None,
        )
        if handle != invalid_handle_value:
            break
        create_error = ctypes.get_last_error()
        if create_error != error_pipe_busy:
            raise OSError(create_error, "CreateFileW failed")
        # WaitNamedPipe does not reserve an instance. Another concurrent client
        # may win between the wait and CreateFile, so retry within this request's
        # original deadline instead of surfacing a spurious transport failure.
        if remaining_ms() <= 0:
            raise OSError(create_error, "CreateFileW remained busy until timeout")

    event = kernel32.CreateEventW(None, wintypes.BOOL(True), wintypes.BOOL(False), None)
    if not event:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise OSError(ctypes.get_last_error(), "CreateEventW failed")

    pending_overlapped: OVERLAPPED | None = None
    try:
        request_bytes = request_line.encode("utf-8")
        request_buffer = ctypes.create_string_buffer(request_bytes)
        written = wintypes.DWORD(0)
        write_overlapped = OVERLAPPED(hEvent=wintypes.HANDLE(event))
        ok = kernel32.WriteFile(
            wintypes.HANDLE(handle),
            request_buffer,
            wintypes.DWORD(len(request_bytes)),
            ctypes.byref(written),
            ctypes.byref(write_overlapped),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error != error_io_pending:
                raise OSError(error, "WriteFile failed")
            pending_overlapped = write_overlapped
            written_count = wait_for_io(handle, write_overlapped, "write")
            pending_overlapped = None
        else:
            written_count = int(written.value)
        if written_count != len(request_bytes):
            raise OSError(0, "WriteFile returned a partial shell IPC request")

        if not kernel32.ResetEvent(wintypes.HANDLE(event)):
            raise OSError(ctypes.get_last_error(), "ResetEvent failed")
        response_buffer = ctypes.create_string_buffer(MAX_RESPONSE_BYTES + 1)
        bytes_read = wintypes.DWORD(0)
        read_overlapped = OVERLAPPED(hEvent=wintypes.HANDLE(event))
        ok = kernel32.ReadFile(
            wintypes.HANDLE(handle),
            response_buffer,
            wintypes.DWORD(MAX_RESPONSE_BYTES + 1),
            ctypes.byref(bytes_read),
            ctypes.byref(read_overlapped),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == error_more_data:
                raise ValueError("shell IPC response exceeded maximum size")
            if error != error_io_pending:
                raise OSError(error, "ReadFile failed")
            pending_overlapped = read_overlapped
            response_size = wait_for_io(handle, read_overlapped, "response")
            pending_overlapped = None
        else:
            response_size = int(bytes_read.value)
        if response_size > MAX_RESPONSE_BYTES:
            raise ValueError("shell IPC response exceeded maximum size")
        response = response_buffer.raw[:response_size]
        newline_at = response.find(b"\n")
        if newline_at >= 0:
            response = response[:newline_at]
        response_line = response.decode("utf-8", errors="replace")
        acknowledgement = _response_ack_line(
            request_line,
            response_line,
            newline_received=newline_at >= 0,
        )
        if acknowledgement is not None:
            if not kernel32.ResetEvent(wintypes.HANDLE(event)):
                raise OSError(ctypes.get_last_error(), "ResetEvent failed")
            acknowledgement_bytes = acknowledgement.encode("utf-8")
            acknowledgement_buffer = ctypes.create_string_buffer(acknowledgement_bytes)
            acknowledgement_written = wintypes.DWORD(0)
            acknowledgement_overlapped = OVERLAPPED(hEvent=wintypes.HANDLE(event))
            ok = kernel32.WriteFile(
                wintypes.HANDLE(handle),
                acknowledgement_buffer,
                wintypes.DWORD(len(acknowledgement_bytes)),
                ctypes.byref(acknowledgement_written),
                ctypes.byref(acknowledgement_overlapped),
            )
            if not ok:
                error = ctypes.get_last_error()
                if error != error_io_pending:
                    raise OSError(error, "WriteFile failed for shell IPC acknowledgement")
                pending_overlapped = acknowledgement_overlapped
                acknowledgement_written_count = wait_for_io(
                    handle,
                    acknowledgement_overlapped,
                    "acknowledgement",
                )
                pending_overlapped = None
            else:
                acknowledgement_written_count = int(acknowledgement_written.value)
            if acknowledgement_written_count != len(acknowledgement_bytes):
                raise OSError(
                    0,
                    "WriteFile returned a partial shell IPC acknowledgement",
                )
        return response_line
    finally:
        try:
            if pending_overlapped is not None:
                cancel_and_drain_io(handle, pending_overlapped)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(event))
            kernel32.CloseHandle(wintypes.HANDLE(handle))


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

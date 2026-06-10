from __future__ import annotations

import hashlib
import json
import os
import queue
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

_lock = threading.Lock()
_last_command: str | None = None
_last_error: str | None = None
_last_success: bool | None = None
_last_command_at: float | None = None


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
        last_success = _last_success
        last_command_at = _last_command_at
    return {
        "available": available(),
        "pipeConfigured": bool(pipe_name),
        "tokenConfigured": bool(token),
        "apiVersion": api_version,
        "pipeNameHash": _hash_pipe_name(pipe_name),
        "lastCommand": last_command,
        "lastSuccess": last_success,
        "lastError": last_error,
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
        _record_result("", False, "empty command")
        return _failure("invalidCommand", "empty command")

    pipe_name = _configured_pipe_name()
    token = _configured_token()
    api_version = _configured_api_version()
    if not available() or not pipe_name or not token:
        _record_result(cleaned_command, False, "shell IPC is not available")
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
        error = None if success else str(response.get("errorCode") or "shell IPC failed")
        _record_result(cleaned_command, success, error)
        return response
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _record_result(cleaned_command, False, message)
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
    return {
        "apiVersion": _configured_api_version(),
        "requestId": None,
        "success": False,
        "errorCode": error_code,
        "fallbackReason": fallback_reason,
        "timingsMs": {"total": 0.0},
        "payload": {},
    }


def _record_result(command: str, success: bool, error: str | None) -> None:
    global _last_command, _last_error, _last_success, _last_command_at
    with _lock:
        _last_command = command
        _last_error = error
        _last_success = success
        _last_command_at = time.monotonic()


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
    global _last_command, _last_error, _last_success, _last_command_at
    with _lock:
        _last_command = None
        _last_error = None
        _last_success = None
        _last_command_at = None

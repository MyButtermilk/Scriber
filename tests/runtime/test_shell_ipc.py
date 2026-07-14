from __future__ import annotations

import json
import threading
import time

import pytest

from src.runtime import shell_ipc


def test_shell_ipc_diagnostics_report_unavailable_without_environment(monkeypatch):
    monkeypatch.delenv(shell_ipc.SHELL_IPC_PIPE_ENV, raising=False)
    monkeypatch.delenv(shell_ipc.SHELL_IPC_TOKEN_ENV, raising=False)
    shell_ipc._reset_diagnostics_for_tests()

    snapshot = shell_ipc.diagnostic_snapshot()

    assert snapshot["available"] is False
    assert snapshot["pipeConfigured"] is False
    assert snapshot["tokenConfigured"] is False
    assert snapshot["pipeNameHash"] is None
    assert snapshot["lastCommand"] is None
    assert snapshot["lastResponse"] is None


def test_shell_ipc_call_uses_private_pipe_and_records_success(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    shell_ipc._reset_diagnostics_for_tests()
    captured: dict[str, object] = {}

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        captured["pipe_name"] = pipe_name
        captured["timeout_seconds"] = timeout_seconds
        captured["request"] = json.loads(request_line)
        return json.dumps(
            {
                "apiVersion": "1",
                "requestId": captured["request"]["requestId"],
                "success": True,
                "errorCode": None,
                "fallbackReason": None,
                "timingsMs": {"total": 1.0},
                "payload": {"pong": True},
            }
        )

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc("ping", timeout_seconds=0.25)
    snapshot = shell_ipc.diagnostic_snapshot()

    assert response["success"] is True
    assert captured["pipe_name"] == r"\\.\pipe\scriber-shell-test"
    assert captured["request"]["command"] == "ping"
    assert captured["request"]["token"] == "secret-token"
    assert snapshot["available"] is True
    assert snapshot["lastCommand"] == "ping"
    assert snapshot["lastSuccess"] is True
    assert snapshot["lastError"] is None
    assert snapshot["lastErrorCode"] is None
    assert snapshot["lastFallbackReason"] is None
    assert snapshot["lastResponse"]["success"] is True
    assert snapshot["lastResponse"]["timingsMs"]["total"] == 1.0
    assert snapshot["pipeNameHash"]


def test_shell_ipc_call_records_transport_error(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    shell_ipc._reset_diagnostics_for_tests()

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        return "not-json"

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc("capabilities")
    snapshot = shell_ipc.diagnostic_snapshot()

    assert response["success"] is False
    assert response["errorCode"] == "transportError"
    assert snapshot["lastCommand"] == "capabilities"
    assert snapshot["lastSuccess"] is False
    assert snapshot["lastErrorCode"] == "transportError"
    assert "JSONDecodeError" in snapshot["lastError"]


def test_shell_ipc_transport_error_redacts_pipe_name_and_token(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    pipe_name = r"\\.\pipe\scriber-shell-secret"
    token = "secret-token"
    escaped_pipe_name = pipe_name.replace("\\", "\\\\")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, pipe_name)
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, token)
    shell_ipc._reset_diagnostics_for_tests()

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        raise OSError(f"cannot open {pipe_name} / {escaped_pipe_name} with {token}")

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc("capabilities")
    snapshot = shell_ipc.diagnostic_snapshot()
    serialized = json.dumps({"response": response, "snapshot": snapshot}, sort_keys=True)

    assert response["success"] is False
    assert response["errorCode"] == "transportError"
    assert pipe_name not in serialized
    assert escaped_pipe_name not in serialized
    assert token not in serialized
    assert "[REDACTED_PIPE]" in serialized
    assert "[REDACTED_TOKEN]" in serialized
    assert snapshot["pipeNameHash"]


def test_shell_ipc_inject_text_diagnostics_are_redacted(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    shell_ipc._reset_diagnostics_for_tests()
    captured: dict[str, object] = {}

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        captured["request"] = json.loads(request_line)
        return json.dumps(
            {
                "apiVersion": "1",
                "requestId": captured["request"]["requestId"],
                "success": True,
                "errorCode": None,
                "fallbackReason": None,
                "timingsMs": {"total": 12.0},
                "payload": {
                    "method": "tauri",
                    "dispatch": "ctrlV",
                    "preDelayMode": "auto",
                    "requestedPreDelayMs": 80,
                    "deadlineMs": 2000,
                    "markers": ["clipboard_set", "paste", "ignored"],
                    "restoreScheduled": True,
                    "restore": {
                        "scheduled": True,
                        "attempted": False,
                        "succeeded": None,
                        "skippedReason": "scheduled",
                        "errorCode": None,
                        "restoreKind": "snapshot",
                        "formatCount": 3,
                        "unsupportedFormatCount": 1,
                        "totalBytes": 4096,
                    },
                    "foregroundBefore": {
                        "available": True,
                        "windowHash": "win-hash",
                        "titleHash": "title-hash",
                        "processIdHash": "pid-hash",
                    },
                    "foregroundAfter": {
                        "available": True,
                        "windowHash": "win-hash",
                        "titleHash": "title-hash",
                        "processIdHash": "pid-hash",
                    },
                    "foregroundChanged": False,
                    "timingsMs": {
                        "clipboardRead": 1.0,
                        "clipboardSet": 2.0,
                        "preDelay": 80.0,
                        "pasteDispatch": 3.0,
                        "total": 10.0,
                    },
                    "text": "private transcript text",
                },
            }
        )

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc(
        "injectText",
        {"text": "private transcript text", "dispatch": "ctrlV"},
    )
    snapshot = shell_ipc.diagnostic_snapshot()
    serialized = json.dumps(snapshot, sort_keys=True)

    assert response["success"] is True
    assert captured["request"]["payload"]["text"] == "private transcript text"
    assert "private transcript text" not in serialized
    inject_payload = snapshot["lastResponse"]["payload"]
    assert inject_payload["method"] == "tauri"
    assert inject_payload["preDelayMode"] == "auto"
    assert inject_payload["requestedPreDelayMs"] == 80.0
    assert inject_payload["deadlineMs"] == 2000.0
    assert inject_payload["markers"] == ["clipboard_set", "paste"]
    assert inject_payload["restore"]["skippedReason"] == "scheduled"
    assert inject_payload["restore"]["restoreKind"] == "snapshot"
    assert inject_payload["restore"]["formatCount"] == 3.0
    assert inject_payload["restore"]["unsupportedFormatCount"] == 1.0
    assert inject_payload["restore"]["totalBytes"] == 4096.0
    assert inject_payload["foregroundBefore"]["titleHash"] == "title-hash"
    assert inject_payload["timingsMs"]["clipboardSet"] == 2.0


def test_shell_ipc_call_rejects_request_id_mismatch(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    shell_ipc._reset_diagnostics_for_tests()

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "apiVersion": "1",
                "requestId": "wrong",
                "success": True,
                "errorCode": None,
                "fallbackReason": None,
                "timingsMs": {"total": 1.0},
                "payload": {},
            }
        )

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc("ping")

    assert response["success"] is False
    assert response["errorCode"] == "transportError"
    assert "requestId mismatch" in response["fallbackReason"]


def test_shell_ipc_call_rejects_api_version_mismatch(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    shell_ipc._reset_diagnostics_for_tests()

    def fake_transport(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        request = json.loads(request_line)
        return json.dumps(
            {
                "apiVersion": "0",
                "requestId": request["requestId"],
                "success": True,
                "errorCode": None,
                "fallbackReason": None,
                "timingsMs": {"total": 1.0},
                "payload": {},
            }
        )

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    response = shell_ipc.call_shell_ipc("ping")

    assert response["success"] is False
    assert response["errorCode"] == "transportError"
    assert "apiVersion mismatch" in response["fallbackReason"]


def test_shell_ipc_windows_transport_does_not_serialize_independent_requests(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []

    def fake_send(pipe_name: str, request_line: str, timeout_seconds: float) -> str:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return json.dumps({"pipeName": pipe_name, "requestLine": request_line})
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(shell_ipc, "_send_request_over_pipe", fake_send)

    def call_transport() -> None:
        try:
            results.append(shell_ipc._call_shell_ipc_windows("pipe", "request\n", 1.0))
        except BaseException as exc:  # pragma: no cover - failure diagnostic
            errors.append(exc)

    threads = [threading.Thread(target=call_transport) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert max_active == 2


def test_shell_ipc_serializes_commands_within_audio_ownership_domain(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    responses: list[dict] = []

    def fake_transport(_pipe_name: str, request_line: str, _timeout_seconds: float) -> str:
        nonlocal active, max_active
        request = json.loads(request_line)
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.04)
            return json.dumps(
                {
                    "apiVersion": "1",
                    "requestId": request["requestId"],
                    "success": True,
                    "errorCode": None,
                    "fallbackReason": None,
                    "timingsMs": {"total": 1.0},
                    "payload": {},
                }
            )
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    threads = [
        threading.Thread(
            target=lambda command=command: responses.append(
                shell_ipc.call_shell_ipc(command, timeout_seconds=0.5)
            )
        )
        for command in ("audioPrewarmStop", "audioCaptureStart")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(responses) == 2
    assert all(response["success"] for response in responses)
    assert max_active == 1


def test_shell_ipc_does_not_couple_audio_and_overlay_domains(monkeypatch):
    monkeypatch.setattr(shell_ipc.sys, "platform", "win32")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_PIPE_ENV, r"\\.\pipe\scriber-shell-test")
    monkeypatch.setenv(shell_ipc.SHELL_IPC_TOKEN_ENV, "secret-token")
    entered = threading.Barrier(2)
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    responses: list[dict] = []
    errors: list[BaseException] = []

    def fake_transport(_pipe_name: str, request_line: str, _timeout_seconds: float) -> str:
        nonlocal active, max_active
        request = json.loads(request_line)
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            entered.wait(timeout=0.4)
            return json.dumps(
                {
                    "apiVersion": "1",
                    "requestId": request["requestId"],
                    "success": True,
                    "errorCode": None,
                    "fallbackReason": None,
                    "timingsMs": {"total": 1.0},
                    "payload": {},
                }
            )
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(shell_ipc, "_call_shell_ipc_windows", fake_transport)

    def invoke(command: str) -> None:
        try:
            responses.append(shell_ipc.call_shell_ipc(command, timeout_seconds=0.5))
        except BaseException as exc:  # pragma: no cover - failure diagnostic
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=("audioCaptureStart",)),
        threading.Thread(target=invoke, args=("overlayShow",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(responses) == 2
    assert all(response["success"] for response in responses)
    assert max_active == 2


def test_shell_ipc_lifecycle_ack_is_request_bound_and_success_only():
    request = json.dumps(
        {
            "apiVersion": "1",
            "requestId": "request-123",
            "command": "audioCaptureStart",
        }
    )
    success = json.dumps(
        {
            "apiVersion": "1",
            "requestId": "request-123",
            "success": True,
            "payload": {"streamId": "stream-123"},
        }
    )
    acknowledgement = shell_ipc._lifecycle_start_ack_line(
        request,
        success,
        newline_received=True,
    )

    assert json.loads(acknowledgement) == {
        "apiVersion": "1",
        "requestId": "request-123",
        "type": "responseAck",
    }

    failure = success.replace('"success": true', '"success": false')
    assert (
        shell_ipc._lifecycle_start_ack_line(
            request,
            failure,
            newline_received=True,
        )
        is None
    )
    with pytest.raises(ValueError, match="requestId mismatch"):
        shell_ipc._lifecycle_start_ack_line(
            request,
            success.replace("request-123", "wrong-response", 1),
            newline_received=True,
        )
    with pytest.raises(ValueError, match="not newline delimited"):
        shell_ipc._lifecycle_start_ack_line(
            request,
            success,
            newline_received=False,
        )

    malformed_success = json.loads(success)
    malformed_success["payload"] = {}
    assert (
        shell_ipc._lifecycle_start_ack_line(
            request,
            json.dumps(malformed_success),
            newline_received=True,
        )
        is None
    )
    malformed_success["payload"] = {"streamId": "   "}
    assert (
        shell_ipc._lifecycle_start_ack_line(
            request,
            json.dumps(malformed_success),
            newline_received=True,
        )
        is None
    )
    malformed_success["payload"] = {"streamId": "x" * 97}
    assert (
        shell_ipc._lifecycle_start_ack_line(
            request,
            json.dumps(malformed_success),
            newline_received=True,
        )
        is None
    )
    # Rust's String::len boundary is UTF-8 bytes, not Unicode code points.
    malformed_success["payload"] = {"streamId": "ä" * 49}
    assert (
        shell_ipc._lifecycle_start_ack_line(
            request,
            json.dumps(malformed_success),
            newline_received=True,
        )
        is None
    )


def test_shell_ipc_windows_transport_writes_bounded_lifecycle_ack(monkeypatch):
    import ctypes
    from ctypes import wintypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    request_line = (
        json.dumps(
            {
                "apiVersion": "1",
                "requestId": "ack-request",
                "command": "audioPrewarmStart",
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    response_line = (
        json.dumps(
            {
                "apiVersion": "1",
                "requestId": "ack-request",
                "success": True,
                "payload": {"prewarmId": "prewarm-1"},
            },
            separators=(",", ":"),
        )
        + "\n"
    )

    class FakeKernel32:
        def __init__(self):
            self.writes: list[bytes] = []
            self.WaitNamedPipeW = FakeCall(lambda *_args: True)
            self.CreateFileW = FakeCall(lambda *_args: 123)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(self._read_file)
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 0)
            self.GetOverlappedResult = FakeCall(lambda *_args: True)
            self.CancelIoEx = FakeCall(lambda *_args: True)
            self.CloseHandle = FakeCall(lambda *_args: True)

        def _write_file(self, _handle, data, length, written_ptr, _overlapped):
            length_value = int(getattr(length, "value", length))
            self.writes.append(ctypes.string_at(data, length_value))
            ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length_value
            return True

        @staticmethod
        def _read_file(_handle, buffer, _buffer_len, bytes_read_ptr, _overlapped):
            encoded = response_line.encode("utf-8")
            ctypes.memmove(buffer, encoded, len(encoded))
            ctypes.cast(bytes_read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(encoded)
            return True

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)

    response = shell_ipc._send_request_over_pipe_windows(
        r"\\.\pipe\scriber-shell-test",
        request_line,
        1.0,
    )

    assert response == response_line.rstrip("\n")
    assert fake_kernel32.writes[0] == request_line.encode("utf-8")
    assert json.loads(fake_kernel32.writes[1]) == {
        "apiVersion": "1",
        "requestId": "ack-request",
        "type": "responseAck",
    }


def test_shell_ipc_windows_transport_cancels_and_drains_timed_out_ack(monkeypatch):
    import ctypes
    from ctypes import wintypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    request_line = (
        '{"apiVersion":"1","requestId":"ack-timeout",'
        '"command":"audioMeetingResume"}\n'
    )
    response_line = (
        '{"apiVersion":"1","requestId":"ack-timeout","success":true,'
        '"payload":{"captureId":"meeting-1"}}\n'
    ).encode("utf-8")

    class FakeKernel32:
        def __init__(self):
            self.last_error = 0
            self.write_calls = 0
            self.cleanup_calls: list[tuple[str, object]] = []
            self.WaitNamedPipeW = FakeCall(lambda *_args: True)
            self.CreateFileW = FakeCall(lambda *_args: 123)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(self._read_file)
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 258)
            self.GetOverlappedResult = FakeCall(self._get_overlapped_result)
            self.CancelIoEx = FakeCall(self._cancel_io)
            self.CloseHandle = FakeCall(self._close_handle)

        def _write_file(self, _handle, _data, length, written_ptr, _overlapped):
            self.write_calls += 1
            if self.write_calls == 1:
                length_value = int(getattr(length, "value", length))
                ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length_value
                return True
            self.last_error = 997
            return False

        @staticmethod
        def _read_file(_handle, buffer, _buffer_len, bytes_read_ptr, _overlapped):
            ctypes.memmove(buffer, response_line, len(response_line))
            ctypes.cast(bytes_read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(
                response_line
            )
            return True

        def _cancel_io(self, handle, _overlapped):
            self.cleanup_calls.append(("cancel", int(getattr(handle, "value", handle))))
            return True

        def _get_overlapped_result(self, _handle, _overlapped, _transferred, wait):
            self.cleanup_calls.append(("drain", bool(getattr(wait, "value", wait))))
            self.last_error = 995
            return False

        def _close_handle(self, handle):
            self.cleanup_calls.append(("close", int(getattr(handle, "value", handle))))
            return True

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: fake_kernel32.last_error,
        raising=False,
    )

    with pytest.raises(TimeoutError, match="acknowledgement timed out"):
        shell_ipc._send_request_over_pipe_windows(
            r"\\.\pipe\scriber-shell-test",
            request_line,
            0.05,
        )

    assert fake_kernel32.cleanup_calls == [
        ("cancel", 123),
        ("drain", True),
        ("close", 456),
        ("close", 123),
    ]


def test_shell_ipc_windows_transport_reads_large_overlapped_response(monkeypatch):
    import ctypes
    from ctypes import wintypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    class FakeKernel32:
        def __init__(self, response: bytes):
            self.response = response
            self.written = b""
            self.WaitNamedPipeW = FakeCall(lambda *_args: True)
            self.CreateFileW = FakeCall(lambda *_args: 123)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(self._read_file)
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 0)
            self.GetOverlappedResult = FakeCall(lambda *_args: True)
            self.CancelIoEx = FakeCall(lambda *_args: True)
            self.CloseHandle = FakeCall(lambda *_args: True)

        def _write_file(self, _handle, data, length, written_ptr, _overlapped):
            length_value = int(getattr(length, "value", length))
            self.written = ctypes.string_at(data, length_value)
            ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length_value
            return True

        def _read_file(self, _handle, buffer, _buffer_len, bytes_read_ptr, _overlapped):
            ctypes.memmove(buffer, self.response, len(self.response))
            ctypes.cast(bytes_read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(
                self.response
            )
            return True

    response_line = (
        json.dumps(
            {
                "apiVersion": "1",
                "requestId": "request-id",
                "success": True,
                "errorCode": None,
                "fallbackReason": None,
                "timingsMs": {"total": 1.0},
                "payload": {"diagnostics": "x" * 5000},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    response_bytes = response_line.encode("utf-8")
    assert len(response_bytes) > 4096
    fake_kernel32 = FakeKernel32(response_bytes)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)

    response = shell_ipc._send_request_over_pipe_windows(
        r"\\.\pipe\scriber-shell-test",
        "{}\n",
        1.0,
    )

    assert fake_kernel32.written == b"{}\n"
    assert response == response_line.rstrip("\n")
    assert json.loads(response)["payload"]["diagnostics"] == "x" * 5000


def test_shell_ipc_windows_transport_retries_create_file_pipe_busy(monkeypatch):
    import ctypes
    from ctypes import wintypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    class FakeKernel32:
        def __init__(self):
            self.last_error = 0
            self.wait_calls = 0
            self.create_calls = 0
            self.response = b'{"success":true}\n'
            self.WaitNamedPipeW = FakeCall(self._wait_named_pipe)
            self.CreateFileW = FakeCall(self._create_file)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(self._read_file)
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 0)
            self.GetOverlappedResult = FakeCall(lambda *_args: True)
            self.CancelIoEx = FakeCall(lambda *_args: True)
            self.CloseHandle = FakeCall(lambda *_args: True)

        def _wait_named_pipe(self, *_args):
            self.wait_calls += 1
            return True

        def _create_file(self, *_args):
            self.create_calls += 1
            if self.create_calls == 1:
                self.last_error = 231
                return ctypes.c_void_p(-1).value
            self.last_error = 0
            return 123

        @staticmethod
        def _write_file(_handle, _data, length, written_ptr, _overlapped):
            length_value = int(getattr(length, "value", length))
            ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length_value
            return True

        def _read_file(self, _handle, buffer, _buffer_len, bytes_read_ptr, _overlapped):
            ctypes.memmove(buffer, self.response, len(self.response))
            ctypes.cast(bytes_read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(
                self.response
            )
            return True

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: fake_kernel32.last_error,
        raising=False,
    )

    response = shell_ipc._send_request_over_pipe_windows(
        r"\\.\pipe\scriber-shell-test",
        "{}\n",
        1.0,
    )

    assert response == '{"success":true}'
    assert fake_kernel32.wait_calls == 2
    assert fake_kernel32.create_calls == 2


def test_shell_ipc_windows_transport_response_read_has_deadline(monkeypatch):
    import ctypes
    from ctypes import wintypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    class FakeKernel32:
        def __init__(self):
            self.last_error = 0
            self.cleanup_calls: list[tuple[str, object]] = []
            self.WaitNamedPipeW = FakeCall(lambda *_args: True)
            self.CreateFileW = FakeCall(lambda *_args: 123)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(self._read_file)
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 258)
            self.GetOverlappedResult = FakeCall(self._get_overlapped_result)
            self.CancelIoEx = FakeCall(self._cancel_io)
            self.CloseHandle = FakeCall(self._close_handle)

        @staticmethod
        def _write_file(_handle, _data, length, written_ptr, _overlapped):
            length_value = int(getattr(length, "value", length))
            ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length_value
            return True

        def _read_file(self, *_args):
            self.last_error = 997
            return False

        def _cancel_io(self, handle, _overlapped):
            self.cleanup_calls.append(("cancel", int(getattr(handle, "value", handle))))
            # Model ERROR_NOT_FOUND: completion may have raced cancellation, so
            # the client must still wait on GetOverlappedResult before freeing.
            self.last_error = 1168
            return False

        def _get_overlapped_result(
            self,
            _handle,
            _overlapped,
            _transferred,
            wait,
        ):
            self.cleanup_calls.append(("drain", bool(getattr(wait, "value", wait))))
            self.last_error = 0
            return True

        def _close_handle(self, handle):
            self.cleanup_calls.append(("close", int(getattr(handle, "value", handle))))
            return True

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: fake_kernel32.last_error,
        raising=False,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="response timed out"):
        shell_ipc._send_request_over_pipe_windows(
            r"\\.\pipe\scriber-shell-test",
            "{}\n",
            0.05,
        )

    assert time.monotonic() - started < 0.25
    assert fake_kernel32.cleanup_calls == [
        ("cancel", 123),
        ("drain", True),
        ("close", 456),
        ("close", 123),
    ]


def test_shell_ipc_windows_transport_drains_timed_out_write_before_cleanup(monkeypatch):
    import ctypes

    class FakeCall:
        def __init__(self, func):
            self.func = func
            self.restype = None

        def __call__(self, *args):
            return self.func(*args)

    class FakeKernel32:
        def __init__(self):
            self.last_error = 0
            self.cleanup_calls: list[tuple[str, object]] = []
            self.WaitNamedPipeW = FakeCall(lambda *_args: True)
            self.CreateFileW = FakeCall(lambda *_args: 123)
            self.CreateEventW = FakeCall(lambda *_args: 456)
            self.WriteFile = FakeCall(self._write_file)
            self.ReadFile = FakeCall(
                lambda *_args: pytest.fail("ReadFile must not run after write timeout")
            )
            self.ResetEvent = FakeCall(lambda *_args: True)
            self.WaitForSingleObject = FakeCall(lambda *_args: 258)
            self.GetOverlappedResult = FakeCall(self._get_overlapped_result)
            self.CancelIoEx = FakeCall(self._cancel_io)
            self.CloseHandle = FakeCall(self._close_handle)

        def _write_file(self, *_args):
            self.last_error = 997
            return False

        def _cancel_io(self, handle, _overlapped):
            self.cleanup_calls.append(("cancel", int(getattr(handle, "value", handle))))
            self.last_error = 0
            return True

        def _get_overlapped_result(
            self,
            _handle,
            _overlapped,
            _transferred,
            wait,
        ):
            self.cleanup_calls.append(("drain", bool(getattr(wait, "value", wait))))
            self.last_error = 995
            return False

        def _close_handle(self, handle):
            self.cleanup_calls.append(("close", int(getattr(handle, "value", handle))))
            return True

    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: fake_kernel32.last_error,
        raising=False,
    )

    with pytest.raises(TimeoutError, match="write timed out"):
        shell_ipc._send_request_over_pipe_windows(
            r"\\.\pipe\scriber-shell-test",
            "{}\n",
            0.05,
        )

    assert fake_kernel32.cleanup_calls == [
        ("cancel", 123),
        ("drain", True),
        ("close", 456),
        ("close", 123),
    ]

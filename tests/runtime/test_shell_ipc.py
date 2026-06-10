from __future__ import annotations

import json

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
    assert "JSONDecodeError" in snapshot["lastError"]


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

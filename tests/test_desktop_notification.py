import time
from unittest.mock import Mock

import pytest

import src.desktop_notification as desktop_notification


def test_fallback_notification_uses_bounded_shell_command(monkeypatch):
    call_shell = Mock(return_value={"success": True, "payload": {"accepted": True}})
    monkeypatch.setattr(desktop_notification, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(desktop_notification, "call_shell_ipc", call_shell)

    assert desktop_notification.show_post_processing_fallback_notification(
        "cerebras/gemma-4-31b",
        "minimax/minimax-m3",
        "post-processing-fallback:session-1",
    )
    call_shell.assert_called_once_with(
        "postProcessingFallbackNotify",
        {
            "eventId": "post-processing-fallback:session-1",
            "primaryModel": "cerebras/gemma-4-31b",
            "fallbackModel": "minimax/minimax-m3",
        },
        timeout_seconds=0.6,
    )


def test_fallback_notification_fails_closed_without_shell_or_valid_ack(monkeypatch):
    call_shell = Mock()
    monkeypatch.setattr(desktop_notification, "call_shell_ipc", call_shell)
    monkeypatch.setattr(desktop_notification, "shell_ipc_available", lambda: False)

    assert not desktop_notification.show_post_processing_fallback_notification(
        "primary/model", "fallback/model", "fallback:1"
    )
    call_shell.assert_not_called()

    monkeypatch.setattr(desktop_notification, "shell_ipc_available", lambda: True)
    call_shell.return_value = {"success": True, "payload": {"accepted": False}}
    assert not desktop_notification.show_post_processing_fallback_notification(
        "primary/model", "fallback/model", "fallback:1"
    )

    call_shell.side_effect = TimeoutError("fixture")
    assert not desktop_notification.show_post_processing_fallback_notification(
        "primary/model", "fallback/model", "fallback:1"
    )


def test_expired_desktop_fallback_notification_never_enters_shell_ipc(monkeypatch):
    call_shell = Mock()
    monkeypatch.setattr(desktop_notification, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(desktop_notification, "call_shell_ipc", call_shell)
    monkeypatch.setattr(desktop_notification.time, "monotonic", lambda: 10.0)

    assert not desktop_notification.show_post_processing_fallback_notification(
        "primary/model",
        "fallback/model",
        "post-processing-fallback:session-2",
        deadline_monotonic=9.0,
    )
    call_shell.assert_not_called()


@pytest.mark.asyncio
async def test_async_desktop_request_sets_deadline_before_executor_submission(monkeypatch):
    captured_deadlines: list[float] = []

    def accepted(_primary, _fallback, _event_id, *, deadline_monotonic):
        captured_deadlines.append(deadline_monotonic)
        return True

    monkeypatch.setattr(desktop_notification, "show_post_processing_fallback_notification", accepted)
    started = time.monotonic()

    assert await desktop_notification.request_post_processing_fallback_notification(
        "primary/model",
        "fallback/model",
        "post-processing-fallback:session-3",
    )
    assert len(captured_deadlines) == 1
    assert started < captured_deadlines[0] <= started + 1.0

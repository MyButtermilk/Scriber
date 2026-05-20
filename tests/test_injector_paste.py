from unittest.mock import MagicMock, patch

import pytest

from pipecat.frames.frames import EndFrame, TranscriptionFrame

from src.config import Config
from src.injector import (
    TextInjector,
    _CLIPBOARD_ACCESS_FAILED,
    _paste_text,
    _windows_clipboard_get_text,
)


@pytest.mark.asyncio
async def test_paste_injection_uses_ctrl_v_and_restores_clipboard(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 0)

    injector = TextInjector(inject_immediately=False)
    frame = TranscriptionFrame(text="hello world", user_id="user", timestamp="now")

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._windows_clipboard_get_text", side_effect=["old", "hello world "]) as get_clip,
        patch("src.injector._windows_clipboard_set_text", return_value=True) as set_clip,
        patch("src.injector.keyboard") as mock_kb,
        patch("src.injector.time.sleep", return_value=None),
    ):
        mock_kb.press_and_release.return_value = None

        await injector.process_frame(frame, MagicMock())
        await injector.process_frame(EndFrame(), MagicMock())

    assert get_clip.call_count >= 2
    assert set_clip.call_count >= 2
    mock_kb.press_and_release.assert_called_once_with("ctrl+v")


def test_paste_text_aborts_when_clipboard_read_fails():
    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_get_text", return_value=_CLIPBOARD_ACCESS_FAILED),
        patch("src.injector._windows_clipboard_set_text", return_value=True) as set_clip,
    ):
        assert _paste_text("new text") is False

    set_clip.assert_not_called()


def test_paste_text_restores_previous_text_when_set_fails():
    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_get_text", return_value="old text"),
        patch("src.injector._windows_clipboard_set_text", side_effect=[False, True]) as set_clip,
    ):
        assert _paste_text("new text") is False

    assert [call.args[0] for call in set_clip.call_args_list] == ["new text", "old text"]


def test_windows_clipboard_get_text_reports_open_failure():
    class _User32:
        def OpenClipboard(self, _owner):
            return False

    windll = type("_Windll", (), {"user32": _User32(), "kernel32": object()})()

    with (
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector.ctypes.windll", windll, create=True),
        patch("src.injector.time.sleep", return_value=None),
    ):
        result = _windows_clipboard_get_text(retries=2, delay_secs=0)

    assert result is _CLIPBOARD_ACCESS_FAILED


def test_windows_clipboard_get_text_distinguishes_missing_text_format():
    class _User32:
        def OpenClipboard(self, _owner):
            return True

        def IsClipboardFormatAvailable(self, _format):
            return False

        def CloseClipboard(self):
            return True

    windll = type("_Windll", (), {"user32": _User32(), "kernel32": object()})()

    with (
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector.ctypes.windll", windll, create=True),
    ):
        result = _windows_clipboard_get_text()

    assert result is None

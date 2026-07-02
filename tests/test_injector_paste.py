from unittest.mock import MagicMock, patch

import pytest

from pipecat.frames.frames import EndFrame, TranscriptionFrame

from src.config import Config
from src.injector import (
    TextInjector,
    _CLIPBOARD_ACCESS_FAILED,
    _ClipboardFormatSnapshot,
    _ClipboardSnapshot,
    _paste_text,
    _windows_clipboard_get_text,
    _windows_clipboard_set_text,
)


def _snapshot() -> _ClipboardSnapshot:
    return _ClipboardSnapshot(
        formats=[_ClipboardFormatSnapshot(format_id=13, data=b"old\x00")],
        total_bytes=4,
    )


@pytest.mark.asyncio
async def test_paste_injection_uses_ctrl_v_and_restores_clipboard(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 0)

    injector = TextInjector(inject_immediately=False)
    frame = TranscriptionFrame(text="hello world", user_id="user", timestamp="now")

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._windows_clipboard_snapshot", return_value=_snapshot()) as get_clip,
        patch("src.injector._windows_clipboard_set_text", return_value=True) as set_clip,
        patch("src.injector._windows_clipboard_sequence_number", side_effect=[100, 100]),
        patch("src.injector._windows_clipboard_restore_snapshot", return_value=True) as restore_clip,
        patch("src.injector.keyboard") as mock_kb,
        patch("src.injector.time.sleep", return_value=None),
    ):
        mock_kb.press_and_release.return_value = None

        await injector.process_frame(frame, MagicMock())
        await injector.process_frame(EndFrame(), MagicMock())

    get_clip.assert_called_once()
    set_clip.assert_called_once_with("hello world ")
    restore_clip.assert_called_once()
    mock_kb.press_and_release.assert_called_once_with("ctrl+v")


def test_paste_text_refuses_to_overwrite_when_clipboard_snapshot_fails():
    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_snapshot", return_value=_CLIPBOARD_ACCESS_FAILED),
        patch("src.injector._windows_clipboard_set_text", return_value=True) as set_clip,
        patch("src.injector.keyboard") as mock_kb,
    ):
        mock_kb.press_and_release.return_value = None
        assert _paste_text("new text") is False

    set_clip.assert_not_called()
    mock_kb.press_and_release.assert_not_called()


def test_paste_text_emits_clipboard_and_paste_markers(monkeypatch):
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 0)
    markers: list[str] = []
    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_snapshot", return_value=_snapshot()),
        patch("src.injector._windows_clipboard_set_text", return_value=True),
        patch("src.injector._windows_clipboard_sequence_number", side_effect=[100, 100]),
        patch("src.injector._windows_clipboard_restore_snapshot", return_value=True),
        patch("src.injector.keyboard") as mock_kb,
        patch("src.injector.time.sleep", return_value=None),
    ):
        mock_kb.press_and_release.return_value = None
        assert _paste_text("new text", on_marker=markers.append) is True

    assert markers == ["clipboard_set", "paste"]
    mock_kb.press_and_release.assert_called_once_with("ctrl+v")


def test_paste_text_does_not_restore_when_clipboard_sequence_changes(monkeypatch):
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 0)

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_snapshot", return_value=_snapshot()),
        patch("src.injector._windows_clipboard_set_text", return_value=True),
        patch("src.injector._windows_clipboard_sequence_number", side_effect=[100, 101]),
        patch("src.injector._windows_clipboard_restore_snapshot", return_value=True) as restore_clip,
        patch("src.injector.keyboard") as mock_kb,
        patch("src.injector.time.sleep", return_value=None),
    ):
        mock_kb.press_and_release.return_value = None
        assert _paste_text("new text") is True

    restore_clip.assert_not_called()


def test_paste_text_rechecks_target_title_before_ctrl_v(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 0)
    markers: list[str] = []

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch(
            "src.injector._active_window_title",
            side_effect=["Scriber Hot Path Text Target", "Codex"],
        ),
        patch("src.injector._get_pre_delay_for_window", return_value=0),
        patch("src.injector._windows_clipboard_snapshot", return_value=_snapshot()),
        patch("src.injector._windows_clipboard_set_text", return_value=True) as set_clip,
        patch("src.injector._windows_clipboard_sequence_number", side_effect=[100, 100]),
        patch("src.injector._windows_clipboard_restore_snapshot", return_value=True) as restore_clip,
        patch("src.injector.keyboard") as mock_kb,
    ):
        assert _paste_text("new text", on_marker=markers.append) is False

    assert markers == ["clipboard_set"]
    set_clip.assert_called_once_with("new text")
    restore_clip.assert_called_once()
    mock_kb.press_and_release.assert_not_called()


def test_paste_text_restores_previous_text_when_set_fails():
    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector._windows_clipboard_snapshot", return_value=_snapshot()),
        patch("src.injector._windows_clipboard_set_text", side_effect=[False, True]) as set_clip,
        patch("src.injector._windows_clipboard_restore_snapshot", return_value=True) as restore_clip,
    ):
        assert _paste_text("new text") is False

    set_clip.assert_called_once_with("new text")
    restore_clip.assert_called_once()


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


def test_windows_clipboard_set_text_reports_open_failure_without_gui_fallback():
    class _User32:
        def OpenClipboard(self, _owner):
            return False

    windll = type("_Windll", (), {"user32": _User32(), "kernel32": object()})()

    with (
        patch("src.injector.sys.platform", "win32"),
        patch("src.injector.ctypes.windll", windll, create=True),
        patch("src.injector.time.sleep", return_value=None),
    ):
        result = _windows_clipboard_set_text("new text", retries=2, delay_secs=0)

    assert result is False

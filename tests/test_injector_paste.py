from unittest.mock import MagicMock, patch

import pytest

from pipecat.frames.frames import EndFrame, TranscriptionFrame

from src.config import Config
from src.injector import TextInjector


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

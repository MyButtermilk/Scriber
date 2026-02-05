from unittest.mock import MagicMock, patch

from src.config import Config
from src.injector import TextInjector


def test_inject_method_type_does_not_use_sendinput_or_paste(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "type")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_called_once_with("hello ", delay=0.01)


def test_inject_method_paste_is_strict_without_sendinput_fallback(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=False) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_called_once_with("hello ", skip_clipboard_restore=False)
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_auto_falls_back_to_sendinput(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "auto")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=False) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_called_once_with("hello ", skip_clipboard_restore=False)
    sendinput_mock.assert_called_once_with("hello ")
    keyboard_mock.write.assert_not_called()

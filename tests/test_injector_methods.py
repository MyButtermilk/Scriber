from unittest.mock import MagicMock, patch

from src.config import Config
from src.injector import TextInjector
from src.runtime import shell_ipc


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
        patch("src.injector.call_shell_ipc") as ipc_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_called_once_with("hello ", skip_clipboard_restore=False)
    sendinput_mock.assert_called_once_with("hello ")
    keyboard_mock.write.assert_not_called()
    ipc_mock.assert_not_called()


def test_disable_text_injection_skips_all_os_input(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", True)
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
    keyboard_mock.write.assert_not_called()


def test_inject_method_tauri_uses_shell_ipc_and_forwards_markers(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    shell_ipc._reset_diagnostics_for_tests()
    markers = []
    injected = []
    injector = TextInjector(on_injected=injected.append, on_injection_marker=markers.append)

    with (
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.call_shell_ipc") as ipc_mock,
    ):
        ipc_mock.return_value = {
            "success": True,
            "payload": {
                "method": "tauri",
                "markers": ["clipboard_set", "paste"],
            },
        }
        injector._inject_text("hello ")

    ipc_mock.assert_called_once()
    command, payload = ipc_mock.call_args.args[:2]
    assert command == "injectText"
    assert payload["text"] == "hello "
    assert payload["dispatch"] == "ctrlV"
    assert payload["deadlineMs"] < 2500
    assert markers == ["clipboard_set", "paste"]
    assert injected == ["hello "]
    snapshot = shell_ipc.diagnostic_snapshot()
    assert snapshot["lastCommand"] == "injectText"
    assert snapshot["lastSuccess"] is True
    assert snapshot["lastResponse"]["payload"]["markers"] == ["clipboard_set", "paste"]
    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()


def test_inject_method_tauri_is_strict_without_python_fallback(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    injector = TextInjector()

    with (
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
        patch("src.injector.call_shell_ipc") as ipc_mock,
    ):
        ipc_mock.return_value = {
            "success": False,
            "errorCode": "transportError",
            "fallbackReason": "test failure",
            "payload": {},
        }
        injector._inject_text("hello ")

    ipc_mock.assert_called_once()
    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_tauri_ipc_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    injector = TextInjector()

    with (
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
        patch("src.injector.call_shell_ipc", side_effect=TimeoutError("ipc timeout")) as ipc_mock,
    ):
        injector._inject_text("hello ")

    ipc_mock.assert_called_once()
    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_tauri_rejects_success_without_paste_marker(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    shell_ipc._reset_diagnostics_for_tests()
    injected = []
    markers = []
    injector = TextInjector(on_injected=injected.append, on_injection_marker=markers.append)

    with patch("src.injector.call_shell_ipc") as ipc_mock:
        ipc_mock.return_value = {
            "success": True,
            "payload": {
                "method": "tauri",
                "markers": ["clipboard_set"],
            },
        }
        injector._inject_text("hello ")

    assert injected == []
    assert markers == []
    snapshot = shell_ipc.diagnostic_snapshot()
    assert snapshot["lastCommand"] == "injectText"
    assert snapshot["lastSuccess"] is False
    assert snapshot["lastErrorCode"] == "missingPasteMarker"
    assert snapshot["lastResponse"]["success"] is False
    assert snapshot["lastResponse"]["payload"]["markers"] == ["clipboard_set"]

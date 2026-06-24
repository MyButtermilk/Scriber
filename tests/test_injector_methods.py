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


def test_inject_target_title_guard_skips_wrong_foreground(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._active_window_title", return_value="Codex"),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_target_title_guard_skips_tauri_ipc_wrong_foreground(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector._active_window_title", return_value="Codex"),
        patch("src.injector.call_shell_ipc") as ipc_mock,
    ):
        injector._inject_text("hello ")

    ipc_mock.assert_not_called()


def test_inject_target_title_guard_allows_expected_foreground(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._active_window_title", return_value="Scriber Hot Path Text Target"),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_called_once_with("hello ", skip_clipboard_restore=False)
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_auto_stops_fallback_after_focus_change(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "auto")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch(
            "src.injector._active_window_title",
            side_effect=["Scriber Hot Path Text Target", "Codex"],
        ),
        patch("src.injector._paste_text", return_value=False) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    paste_mock.assert_called_once_with("hello ", skip_clipboard_restore=False)
    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_sendinput_rechecks_target_before_dispatch(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "sendinput")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch(
            "src.injector._active_window_title",
            side_effect=["Scriber Hot Path Text Target", "Codex"],
        ),
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    sendinput_mock.assert_not_called()
    keyboard_mock.write.assert_not_called()


def test_inject_method_type_rechecks_target_before_keyboard_dispatch(monkeypatch):
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "type")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    injector = TextInjector()

    with (
        patch("src.injector.HAS_GUI", True),
        patch(
            "src.injector._active_window_title",
            side_effect=["Scriber Hot Path Text Target", "Codex"],
        ),
        patch("src.injector.keyboard", new=MagicMock()) as keyboard_mock,
    ):
        injector._inject_text("hello ")

    keyboard_mock.write.assert_not_called()


def test_inject_method_tauri_uses_shell_ipc_and_forwards_markers(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "Scriber Hot Path Text Target")
    shell_ipc._reset_diagnostics_for_tests()
    markers = []
    injected = []
    injector = TextInjector(on_injected=injected.append, on_injection_marker=markers.append)

    with (
        patch("src.injector._paste_text", return_value=True) as paste_mock,
        patch("src.injector._send_input_text", return_value=True) as sendinput_mock,
        patch(
            "src.injector._active_window_title",
            return_value="Scriber Hot Path Text Target",
        ),
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
    assert payload["preDelayMode"] == "auto"
    assert payload["preDelayMs"] == Config.PASTE_PRE_DELAY_MS
    assert payload["expectedForegroundTitle"] == "Scriber Hot Path Text Target"
    assert payload["deadlineMs"] < 2500
    assert markers == ["clipboard_set", "paste"]
    assert injected == ["hello "]
    snapshot = shell_ipc.diagnostic_snapshot()
    assert snapshot["lastCommand"] == "injectText"
    assert snapshot["lastSuccess"] is True
    assert snapshot["lastResponse"]["payload"]["markers"] == ["clipboard_set", "paste"]
    paste_mock.assert_not_called()
    sendinput_mock.assert_not_called()


def test_inject_method_tauri_forwards_estimated_marker_timestamps(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    shell_ipc._reset_diagnostics_for_tests()
    markers = []
    injector = TextInjector(
        on_injected=lambda _text: None,
        on_injection_marker=lambda marker, timestamp_ns=None: markers.append(
            (marker, timestamp_ns)
        ),
    )

    with (
        patch("src.injector.call_shell_ipc") as ipc_mock,
        patch(
            "src.injector.time.perf_counter_ns",
            side_effect=[1_000_000_000, 1_100_000_000],
        ),
    ):
        ipc_mock.return_value = {
            "success": True,
            "payload": {
                "method": "tauri",
                "markers": ["clipboard_set", "paste"],
                "timingsMs": {
                    "clipboardSet": 10.0,
                    "pasteDispatch": 45.0,
                    "total": 50.0,
                },
            },
        }
        injector._inject_text("hello ")

    assert markers == [
        ("clipboard_set", 1_060_000_000),
        ("paste", 1_095_000_000),
    ]


def test_inject_method_tauri_leaves_auto_pre_delay_deadline_to_rust(monkeypatch):
    monkeypatch.setattr(Config, "INJECT_METHOD", "tauri")
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "PASTE_PRE_DELAY_MS", 5000)
    injector = TextInjector()

    with patch("src.injector.call_shell_ipc") as ipc_mock:
        ipc_mock.return_value = {
            "success": False,
            "errorCode": "deadlineBeforePaste",
            "fallbackReason": "deadline would be exceeded",
            "payload": {},
        }
        injector._inject_text("hello ")

    ipc_mock.assert_called_once()
    _, payload = ipc_mock.call_args.args[:2]
    assert payload["preDelayMode"] == "auto"
    assert payload["preDelayMs"] == 5000


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


def test_inject_method_tauri_rejects_success_without_clipboard_set_marker(monkeypatch):
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
                "markers": ["paste"],
            },
        }
        injector._inject_text("hello ")

    assert injected == []
    assert markers == []
    snapshot = shell_ipc.diagnostic_snapshot()
    assert snapshot["lastCommand"] == "injectText"
    assert snapshot["lastSuccess"] is False
    assert snapshot["lastErrorCode"] == "missingInjectionMarker"
    assert snapshot["lastFallbackReason"] == "missing marker(s): clipboard_set"
    assert snapshot["lastResponse"]["success"] is False
    assert snapshot["lastResponse"]["payload"]["markers"] == ["paste"]

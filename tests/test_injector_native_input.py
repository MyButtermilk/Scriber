import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import injector


@pytest.fixture
def input_api(monkeypatch):
    api = SimpleNamespace(SendInput=Mock(), GetAsyncKeyState=Mock(return_value=0))
    monkeypatch.setattr(injector.sys, "platform", "win32")
    monkeypatch.setattr(injector.ctypes, "windll", SimpleNamespace(user32=api), raising=False)
    monkeypatch.setattr(injector.ctypes, "WinDLL", lambda *_args, **_kwargs: api, raising=False)
    return api


def test_unicode_input_preserves_surrogate_pairs_in_one_batch(input_api):
    input_api.SendInput.side_effect = lambda count, _inputs, _size: count
    assert injector._send_input_text("Ä😀.")
    count, events, size = input_api.SendInput.call_args.args
    assert count == 8
    assert [events[index].ki.wScan for index in range(0, count, 2)] == [0xC4, 0xD83D, 0xDE00, 0x2E]
    assert size == ctypes.sizeof(injector.INPUT)


@pytest.mark.parametrize("control_held, expected", [(False, [17, 86, 86, 17]), (True, [86, 86])])
def test_paste_chord_keeps_existing_control_key_state(input_api, control_held, expected):
    input_api.GetAsyncKeyState.return_value = 0x8000 if control_held else 0
    input_api.SendInput.side_effect = lambda count, _inputs, _size: count
    injector._send_paste_shortcut()
    count, events, _size = input_api.SendInput.call_args.args
    assert [events[index].ki.wVk for index in range(count)] == expected


def test_partial_paste_is_an_uncertain_terminal_dispatch(input_api):
    input_api.SendInput.return_value = 1
    with pytest.raises(OSError, match="pasteInputDispatchUncertain"):
        injector._send_paste_shortcut()


@pytest.mark.parametrize(
    "sent, control_held, expected", [(1, False, [17]), (2, False, [86, 17]), (3, False, [17]), (1, True, [86])]
)
def test_partial_paste_releases_only_our_unmatched_synthetic_downs(input_api, sent, control_held, expected):
    input_api.GetAsyncKeyState.return_value = 0x8000 if control_held else 0
    input_api.SendInput.side_effect = [sent, len(expected)]
    with pytest.raises(OSError, match="pasteInputDispatchUncertain"):
        injector._send_paste_shortcut()
    assert input_api.SendInput.call_count == 2
    count, events, _size = input_api.SendInput.call_args.args
    assert [events[index].ki.wVk for index in range(count)] == expected
    assert all(events[index].ki.dwFlags == injector.KEYEVENTF_KEYUP for index in range(count))


def test_partial_unicode_input_suppresses_outer_typing_retry(input_api):
    input_api.SendInput.return_value = 1
    assert injector._send_input_text("word") is True
    input_api.SendInput.return_value = 0
    assert injector._send_input_text("word") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ABI")
def test_input_structure_matches_windows_pointer_width():
    assert ctypes.sizeof(injector.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

"""Opt-in Win32 clipboard probe; uses only an invisible, dedicated Edit target.

Run with SCRIBER_RUN_WINDOWS_CLIPBOARD_SMOKE=1. The real clipboard is saved
and restored; no keyboard input is sent to the user's foreground application.
"""

from __future__ import annotations

import ctypes
import json
import os
import statistics
import sys
import time
from ctypes import wintypes
from unittest.mock import Mock

import pytest

from src import injector
from src.config import Config
from src.runtime.windows_clipboard_lease import WindowsClipboardLease

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("SCRIBER_RUN_WINDOWS_CLIPBOARD_SMOKE") != "1",
    reason="requires explicit opt-in to the real Windows clipboard probe",
)


@pytest.fixture
def native_edit(monkeypatch):
    user32 = ctypes.windll.user32
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    parent = user32.CreateWindowExW(0, "Static", "Scriber isolated probe", 0, 0, 0, 1, 1, None, None, None, None)
    edit = user32.CreateWindowExW(0, "Edit", "", 0x40000000 | 4 | 0xC0, 0, 0, 1000, 500, parent, None, None, None)
    assert edit
    original = injector._windows_clipboard_snapshot()
    assert isinstance(original, injector._ClipboardSnapshot)
    keyboard = Mock()
    keyboard.press_and_release.side_effect = lambda _keys: user32.SendMessageW(edit, 0x0302, 0, 0)
    monkeypatch.setattr(injector, "keyboard", keyboard)
    monkeypatch.setattr(injector, "_send_paste_shortcut", lambda: user32.SendMessageW(edit, 0x0302, 0, 0))
    monkeypatch.setattr(injector, "_ensure_gui_modules", lambda: True)
    monkeypatch.setattr(injector, "_get_pre_delay_for_window", lambda: 0)
    monkeypatch.setattr(injector, "_foreground_target_guard_allows_dispatch", lambda *_a, **_k: True)
    monkeypatch.setattr(
        injector,
        "_active_foreground_target_snapshot",
        lambda: injector._ForegroundTargetSnapshot("Scriber isolated clipboard probe", os.getpid(), 1, edit),
    )
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 1500)
    try:
        yield user32, edit
    finally:
        # Allow the old implementation's delayed restore to finish before the
        # original snapshot is restored. Never replace a concurrent user copy.
        time.sleep(1.6)
        current = injector._windows_clipboard_get_text()
        if isinstance(current, str) and current.startswith("SCRIBER_CLIPBOARD_PROBE_"):
            injector._windows_clipboard_restore_snapshot(original)
        user32.DestroyWindow(edit)
        user32.DestroyWindow(parent)


def test_fast_followup_paste_uses_previous_clipboard(native_edit):
    user32, edit = native_edit
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    new = "SCRIBER_CLIPBOARD_PROBE_DICTATION"
    assert injector._windows_clipboard_set_text(old)
    assert injector._windows_clipboard_get_text() == old
    started = time.perf_counter()
    markers = {}
    assert injector._paste_text(new, on_marker=lambda marker: markers.setdefault(marker, time.perf_counter()))
    dispatched_ms = (time.perf_counter() - started) * 1000
    pasted_at = markers["paste"]
    deadline = pasted_at + 0.1
    restored_ms = None
    while time.perf_counter() < deadline:
        if injector._windows_clipboard_get_text() == old:
            restored_ms = (time.perf_counter() - pasted_at) * 1000
            break
        time.sleep(0.002)
    user32.SendMessageW(edit, 0x0302, 0, 0)
    buffer = ctypes.create_unicode_buffer(2048)
    user32.GetWindowTextW(edit, buffer, len(buffer))
    print(
        json.dumps(
            {
                "dispatchMs": round(dispatched_ms, 3),
                "restoreMs": restored_ms,
                "duplicateDictation": buffer.value == new + new,
                "correctText": buffer.value == new + old,
            }
        )
    )
    assert buffer.value == new + old, "a fast follow-up paste duplicated the dictation"
    assert restored_ms is not None and restored_ms < 100


def test_preserves_all_restorable_clipboard_formats(native_edit):
    user32, _ = native_edit
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    format_id = user32.RegisterClipboardFormatW("ScriberClipboardProbePayload")
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    original = injector._ClipboardSnapshot(
        formats=[
            injector._ClipboardFormatSnapshot(13, old.encode("utf-16-le") + b"\0\0"),
            injector._ClipboardFormatSnapshot(format_id, b"registered-format-payload\0"),
        ]
    )
    assert injector._windows_clipboard_restore_snapshot(original)
    before = injector._windows_clipboard_snapshot()
    assert injector._paste_text("SCRIBER_CLIPBOARD_PROBE_DICTATION")
    assert injector._ACTIVE_CLIPBOARD_LEASE.wait(1)
    after = injector._windows_clipboard_snapshot()
    assert [(part.format_id, part.data) for part in after.formats] == [
        (part.format_id, part.data) for part in before.formats
    ]


@pytest.mark.parametrize("library_name, class_name", [("RICHED20.dll", "RichEdit20W"), ("Msftedit.dll", "RICHEDIT50W")])
def test_rich_edit_keeps_fallback_when_reader_cannot_be_identified(native_edit, monkeypatch, library_name, class_name):
    user32, _ = native_edit
    library = ctypes.WinDLL(library_name)
    rich_edit = user32.CreateWindowExW(0, class_name, "", 4 | 0xC0, 0, 0, 1000, 500, None, None, None, None)
    assert rich_edit
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    new = "SCRIBER_CLIPBOARD_PROBE_DICTATION"
    try:
        monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 250)
        assert injector._windows_clipboard_set_text(old)
        monkeypatch.setattr(injector, "_send_paste_shortcut", lambda: user32.SendMessageW(rich_edit, 0x0302, 0, 0))
        assert injector._paste_text(new)
        assert injector._ACTIVE_CLIPBOARD_LEASE.wait(1)
        assert injector._ACTIVE_CLIPBOARD_LEASE.status == "fallbackDelay"
        user32.SendMessageW(rich_edit, 0x0302, 0, 0)
        buffer = ctypes.create_unicode_buffer(2048)
        user32.GetWindowTextW(rich_edit, buffer, len(buffer))
        assert buffer.value == new + old
    finally:
        user32.DestroyWindow(rich_edit)
        del library


def test_new_user_copy_always_wins(native_edit, monkeypatch):
    assert injector._windows_clipboard_set_text("SCRIBER_CLIPBOARD_PROBE_OLD")
    newer = "SCRIBER_CLIPBOARD_PROBE_USER_COPY"
    monkeypatch.setattr(injector, "_send_paste_shortcut", lambda: injector._windows_clipboard_set_text(newer))
    assert injector._paste_text("SCRIBER_CLIPBOARD_PROBE_DICTATION")
    assert injector._ACTIVE_CLIPBOARD_LEASE.wait(1)
    assert injector._windows_clipboard_get_text() == newer
    assert injector._ACTIVE_CLIPBOARD_LEASE.status == "clipboardChanged"


def test_unknown_reader_retains_the_conservative_fallback(native_edit, monkeypatch):
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    new = "SCRIBER_CLIPBOARD_PROBE_DICTATION"
    assert injector._windows_clipboard_set_text(old)
    monkeypatch.setattr(Config, "PASTE_RESTORE_DELAY_MS", 250)
    received = []
    # OpenClipboard(NULL), as used by an unidentifiable clipboard manager.
    monkeypatch.setattr(
        injector, "_send_paste_shortcut", lambda: received.append(injector._windows_clipboard_get_text())
    )
    assert injector._paste_text(new)
    assert received == [new]
    assert injector._windows_clipboard_get_text() == new
    assert injector._ACTIVE_CLIPBOARD_LEASE.wait(1)
    assert injector._windows_clipboard_get_text() == old
    assert injector._ACTIVE_CLIPBOARD_LEASE.status == "fallbackDelay"


def test_acknowledged_reader_must_release_clipboard_before_restore(native_edit, monkeypatch):
    user32, edit = native_edit
    assert injector._windows_clipboard_set_text("SCRIBER_CLIPBOARD_PROBE_OLD")

    def consume_and_hold():
        assert user32.OpenClipboard(edit)
        assert user32.GetClipboardData(13)

    monkeypatch.setattr(injector, "_send_paste_shortcut", consume_and_hold)
    try:
        assert injector._paste_text("SCRIBER_CLIPBOARD_PROBE_DICTATION")
        assert not injector._ACTIVE_CLIPBOARD_LEASE.wait(0.02)
    finally:
        user32.CloseClipboard()
    assert injector._ACTIVE_CLIPBOARD_LEASE.wait(1)
    assert injector._windows_clipboard_get_text() == "SCRIBER_CLIPBOARD_PROBE_OLD"


def test_copy_between_snapshot_and_publication_is_preserved(native_edit):
    assert injector._windows_clipboard_set_text("SCRIBER_CLIPBOARD_PROBE_OLD")
    before = injector._windows_clipboard_snapshot()
    lease = WindowsClipboardLease(
        "SCRIBER_CLIPBOARD_PROBE_DICTATION",
        [(part.format_id, part.data) for part in before.formats],
        target_process_id=os.getpid(),
        restore_delay_ms=1500,
        expected_sequence=before.sequence_number,
    )
    assert injector._windows_clipboard_set_text("SCRIBER_CLIPBOARD_PROBE_NEW_COPY")
    assert lease.publish() is False
    assert lease.wait(1)
    assert injector._windows_clipboard_get_text() == "SCRIBER_CLIPBOARD_PROBE_NEW_COPY"


def test_cancelled_paste_restores_even_without_any_render_request(native_edit):
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    assert injector._windows_clipboard_set_text(old)
    before = injector._windows_clipboard_snapshot()
    lease = WindowsClipboardLease(
        "SCRIBER_CLIPBOARD_PROBE_DICTATION",
        [(part.format_id, part.data) for part in before.formats],
        target_process_id=os.getpid(),
        restore_delay_ms=1500,
        expected_sequence=before.sequence_number,
    )
    assert lease.publish()
    lease.finish(dispatched=False)
    assert lease.wait(1)
    assert injector._windows_clipboard_get_text() == old


def test_repeated_native_paste_comparison(native_edit, monkeypatch):
    """A/B the real clipboard lifetime; no wall-clock claim comes from mocks."""
    user32, edit = native_edit
    old = "SCRIBER_CLIPBOARD_PROBE_OLD"
    new = "SCRIBER_CLIPBOARD_PROBE_DICTATION"
    results = {}
    for acknowledged in (False, True):
        durations = []
        duplicate_count = 0
        monkeypatch.setattr(Config, "PASTE_ACKNOWLEDGED_RESTORE", acknowledged)
        for _ in range(10):
            user32.SetWindowTextW(edit, "")
            assert injector._windows_clipboard_set_text(old)
            markers = {}
            assert injector._paste_text(
                new, on_marker=lambda marker, times=markers: times.setdefault(marker, time.perf_counter())
            )
            started = markers["paste"]
            restored = None
            # Issue the second paste 100 ms after the first paste, retaining
            # the same consumer and output across both implementations.
            while time.perf_counter() - started < 0.1:
                if restored is None and injector._windows_clipboard_get_text() == old:
                    restored = (time.perf_counter() - started) * 1000
                time.sleep(0.002)
            user32.SendMessageW(edit, 0x0302, 0, 0)
            buffer = ctypes.create_unicode_buffer(2048)
            user32.GetWindowTextW(edit, buffer, len(buffer))
            duplicate_count += buffer.value == new + new
            assert buffer.value in {new + new, new + old}
            deadline = started + 2.5
            while restored is None and time.perf_counter() < deadline:
                if injector._windows_clipboard_get_text() == old:
                    restored = (time.perf_counter() - started) * 1000
                else:
                    time.sleep(0.002)
            assert restored is not None
            durations.append(restored)
        results["acknowledged" if acknowledged else "legacy"] = {
            "runs": len(durations),
            "duplicateCount": duplicate_count,
            "restoreP50Ms": round(statistics.median(durations), 3),
            "restoreP95Ms": round(sorted(durations)[-1], 3),
        }
    print(json.dumps({"clipboardComparison": results}))
    assert results["legacy"]["duplicateCount"] == 10
    assert results["acknowledged"]["duplicateCount"] == 0
    assert results["acknowledged"]["restoreP95Ms"] < 100
    assert results["acknowledged"]["restoreP50Ms"] < results["legacy"]["restoreP50Ms"] * 0.1

"""Opt-in real Word probes; exclusively create and discard our own document.

SCRIBER_RUN_WINDOWS_OFFICE_SMOKE=1 permits a brief focus change to this test
document. The previous foreground window is restored if the test still owns
focus. No existing document is attached, changed, saved, or closed.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from contextlib import suppress
from ctypes import wintypes

import pytest

from src import injector
from src.runtime import office_text_insert as office

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("SCRIBER_RUN_WINDOWS_OFFICE_SMOKE") != "1",
    reason="requires explicit opt-in to an isolated visible Word test document",
)


@pytest.fixture(scope="module")
def word_document():
    import comtypes.client

    api = office._user32()
    api.SetForegroundWindow.argtypes = [wintypes.HWND]
    previous = api.GetForegroundWindow()
    application = document = None
    owned = False
    hwnd = None
    try:
        application = comtypes.client.CreateObject("Word.Application", dynamic=True)
        assert application.Documents.Count == 0, "test requires an isolated Word instance"
        owned = True
        application.DisplayAlerts = 0
        document = application.Documents.Add()
        application.Visible = True
        document.ActiveWindow.Activate()
        hwnd = int(document.ActiveWindow.Hwnd)
        api.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        assert api.GetForegroundWindow() == hwnd
        assert office._focused_editor(api) is not None
        yield document, api, hwnd
    finally:
        restore_focus = hwnd and api.GetForegroundWindow() == hwnd
        # The test-only dynamic automation client can retain temporary ctypes
        # pointers until cyclic collection; release those while its own server
        # is still alive. Product calls release their apartment before return.
        gc.collect()
        if document is not None:
            with suppress(Exception):
                document.Close(0)
        if owned:
            with suppress(Exception):
                application.Quit(0)
        if restore_focus and previous:
            api.SetForegroundWindow(previous)


def test_real_word_preserves_unicode_paragraphs_selection_style_and_clipboard(word_document):
    document, api, hwnd = word_document
    before = injector._windows_clipboard_snapshot()
    assert isinstance(before, injector._ClipboardSnapshot)
    measurements = []
    texts = ["Scriber: Grüße, € — 日本語 😀.", "Absatz eins\nAbsatz zwei\n\nAbsatz vier", "Einfügen " * 167]
    for text in texts:
        assert api.GetForegroundWindow() == hwnd
        document.Content.Text = "prefix REPLACE suffix"
        selected = document.Range(7, 14)
        selected.Font.Bold = True
        selected.Select()
        started = time.perf_counter()
        result = office.try_insert_office_text(text, validate_target=lambda: api.GetForegroundWindow() == hwnd)
        elapsed = (time.perf_counter() - started) * 1000
        assert result is office.OfficeInsertOutcome.INSERTED
        expected = "prefix " + text.replace("\n", "\r") + " suffix\r"
        assert document.Content.Text == expected
        assert document.Range(7, 8).Font.Bold == -1
        assert document.ActiveWindow.Selection.Start == document.ActiveWindow.Selection.End
        assert injector._windows_clipboard_sequence_number() == before.sequence_number
        measurements.append({"characters": len(text), "insertMs": round(elapsed, 3), "clipboardUntouched": True})
    print(json.dumps({"wordNativeInsertion": measurements}))


def test_real_word_target_change_cancels_without_writing(word_document):
    document, api, hwnd = word_document
    document.Content.Text = "unchanged"
    document.Range(0, 0).Select()
    result = office.try_insert_office_text("must not be written", validate_target=lambda: False)
    assert result is office.OfficeInsertOutcome.CANCELLED
    assert document.Content.Text == "unchanged\r"
    assert api.GetForegroundWindow() == hwnd


def test_real_word_auto_completion_leaves_previous_clipboard_available(word_document, monkeypatch):
    document, api, hwnd = word_document
    before = injector._windows_clipboard_snapshot()
    assert isinstance(before, injector._ClipboardSnapshot)
    monkeypatch.setattr(injector.Config, "DISABLE_TEXT_INJECTION", False)
    document.Content.Text = ""
    document.Range(0, 0).Select()
    completed = []
    text_injector = injector.TextInjector(injection_method="auto", on_injected=completed.append)
    text_injector._inject_text("Scriber product path 😀.\nSecond paragraph.")
    assert completed == ["Scriber product path 😀.\nSecond paragraph."]
    assert document.Content.Text == "Scriber product path 😀.\rSecond paragraph.\r"
    assert injector._windows_clipboard_sequence_number() == before.sequence_number
    assert api.GetForegroundWindow() == hwnd

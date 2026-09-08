import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src import injector
from src.runtime import office_text_insert as office


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(office, "_unicode_variant", lambda text: text)
    selection = SimpleNamespace(
        Document=SimpleNamespace(ReadOnly=False, ProtectionType=-1),
        Type=1,
        Comments=SimpleNamespace(Count=0),
        Application=SimpleNamespace(Options=SimpleNamespace(ReplaceSelection=True)),
        Text="selected",
        TypeText=Mock(),
    )
    return SimpleNamespace(Selection=selection)


def test_office_rechecks_focus_before_any_mutation(window):
    result = office._insert_into_window(window, "dictation", still_focused=lambda: False, on_marker=None)
    assert result is office.OfficeInsertOutcome.CANCELLED
    window.Selection.TypeText.assert_not_called()


def test_expiry_between_com_calls_prevents_the_next_read(window):
    expired = False

    class SlowWindow:
        @property
        def Selection(self):
            nonlocal expired
            expired = True
            return window.Selection

    with pytest.raises(office._OfficeDeadlineExpired):
        office._insert_into_window(
            SlowWindow(),
            "dictation",
            still_focused=lambda: True,
            on_marker=None,
            expired=lambda: expired,
        )
    window.Selection.TypeText.assert_not_called()


def test_watchdog_retries_when_deadline_lands_between_com_calls():
    stopped = threading.Event()
    expired = threading.Event()
    attempts = []

    def cancel():
        attempts.append(1)
        if len(attempts) == 3:
            stopped.set()
        else:
            raise OSError("E_NOINTERFACE: no active COM call yet")

    worker = threading.Thread(
        target=lambda: office._cancel_after_deadline(stopped, expired, cancel, timeout=0.01, interval=0.005),
    )
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert expired.is_set()
    assert len(attempts) == 3


@pytest.mark.parametrize("unsupported", ["read_only", "protected", "comments", "column", "table_cell", "no_replace"])
def test_unsupported_selection_keeps_clipboard_fallback(window, unsupported):
    selection = window.Selection
    if unsupported == "read_only":
        selection.Document.ReadOnly = True
    elif unsupported == "protected":
        selection.Document.ProtectionType = 3
    elif unsupported == "comments":
        selection.Comments.Count = 1
    elif unsupported == "column":
        selection.Type = 4
    elif unsupported == "table_cell":
        selection.Text = "cell\r\x07"
    else:
        selection.Application.Options.ReplaceSelection = False
    result = office._insert_into_window(window, "dictation", still_focused=lambda: True, on_marker=None)
    assert result is office.OfficeInsertOutcome.UNAVAILABLE
    selection.TypeText.assert_not_called()


def test_unicode_and_paragraphs_pass_as_one_operation(window):
    markers = []
    result = office._insert_into_window(
        window, "Grüße 😀.\n\nAbsatz\r\n日本語", still_focused=lambda: True, on_marker=markers.append
    )
    assert result is office.OfficeInsertOutcome.INSERTED
    window.Selection.TypeText.assert_called_once_with("Grüße 😀.\r\rAbsatz\r日本語")
    assert markers == ["paste_requested", "paste"]


def test_failure_after_write_admission_is_terminal(window):
    window.Selection.TypeText.side_effect = OSError("COM disconnected after accepting text")
    result = office._insert_into_window(window, "dictation", still_focused=lambda: True, on_marker=None)
    assert result is office.OfficeInsertOutcome.UNCERTAIN
    window.Selection.TypeText.assert_called_once()


@pytest.mark.parametrize("result", [office.OfficeInsertOutcome.UNCERTAIN, office.OfficeInsertOutcome.CANCELLED])
def test_auto_never_retries_an_uncertain_or_cancelled_office_write(monkeypatch, result):
    monkeypatch.setattr(injector, "HAS_GUI", True)
    monkeypatch.setattr(injector.Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(injector, "_foreground_target_guard_allows_dispatch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(injector, "try_insert_office_text", lambda *_args, **_kwargs: result)
    completed = Mock()
    text_injector = injector.TextInjector(injection_method="auto", on_injected=completed)
    with patch.object(injector, "_paste_text") as paste, patch.object(injector, "_send_input_text") as send:
        text_injector._inject_text("dictation")
    paste.assert_not_called()
    send.assert_not_called()
    completed.assert_not_called()


def test_auto_success_notifies_once_without_clipboard(monkeypatch):
    monkeypatch.setattr(injector, "HAS_GUI", True)
    monkeypatch.setattr(injector.Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(injector, "_foreground_target_guard_allows_dispatch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        injector, "try_insert_office_text", lambda *_args, **_kwargs: office.OfficeInsertOutcome.INSERTED
    )
    completed = Mock()
    text_injector = injector.TextInjector(injection_method="auto", on_injected=completed)
    with patch.object(injector, "_paste_text") as paste, patch.object(injector, "_send_input_text") as send:
        text_injector._inject_text("dictation")
    completed.assert_called_once_with("dictation")
    paste.assert_not_called()
    send.assert_not_called()

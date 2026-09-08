"""Clipboard-free insertion into the exact focused classic Office editor.

The native object model is obtained from the focused _WwG HWND, never from a
global active Word/Outlook instance. Preflight is read-only. Once TypeText is
invoked, every error is terminal: retrying an uncertain COM write could duplicate
the dictation. Unsupported editors keep the existing clipboard path.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from src.core.logging_setup import emit_event


class OfficeInsertOutcome(Enum):
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    INSERTED = "inserted"
    UNCERTAIN = "uncertain"


class _OfficeDeadlineExpired(Exception):
    pass


def _cancel_after_deadline(
    stopped: threading.Event,
    expired: threading.Event,
    cancel: Callable[[], None],
    *,
    timeout: float = 0.75,
    interval: float = 0.05,
) -> None:
    if stopped.wait(timeout):
        return
    expired.set()
    # The deadline can land between calls (E_NOINTERFACE). Keep cancelling
    # until the owner unwinds; later calls must still receive cancellation.
    while not stopped.is_set():
        with suppress(OSError):
            cancel()
        stopped.wait(interval)


@dataclass(frozen=True)
class _EditorIdentity:
    root: int
    focus: int
    process_id: int
    host_class: str


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    for name, args, result in (
        ("GetForegroundWindow", [], wintypes.HWND),
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        ("GetGUIThreadInfo", [wintypes.DWORD, ctypes.POINTER(_GUIThreadInfo)], wintypes.BOOL),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ):
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _focused_editor(api) -> _EditorIdentity | None:
    root = api.GetForegroundWindow()
    if not root:
        return None
    class_name = ctypes.create_unicode_buffer(128)
    api.GetClassNameW(root, class_name, len(class_name))
    host_class = class_name.value.casefold()
    if host_class not in {"opusapp", "rctrl_renwnd32"}:
        return None
    pid = wintypes.DWORD()
    tid = api.GetWindowThreadProcessId(root, ctypes.byref(pid))
    info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
    if not tid or not api.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None
    # Menus, dialogs and ribbon/search/address fields must never receive body
    # text through a previously selected document behind them.
    if info.flags & 0x1E or not info.hwndFocus or api.GetAncestor(info.hwndFocus, 2) != root:
        return None
    api.GetClassNameW(info.hwndFocus, class_name, len(class_name))
    if class_name.value != "_WwG":
        return None
    return _EditorIdentity(int(root), int(info.hwndFocus), int(pid.value), host_class)


def _unicode_variant(text: str):
    from comtypes.automation import VARIANT, VT_BSTR  # type: ignore[import-untyped]

    api = ctypes.WinDLL("oleaut32")
    api.SysAllocStringLen.argtypes = [wintypes.LPCWSTR, ctypes.c_uint]
    api.SysAllocStringLen.restype = ctypes.c_void_p
    # comtypes 1.4.16 passes Python len(str) to SysAllocStringLen. Windows
    # expects UTF-16 units, so astral characters would truncate the string.
    value = VARIANT()
    value.vt = VT_BSTR
    value._.c_void_p = api.SysAllocStringLen(text, len(text.encode("utf-16-le")) // 2)
    if not value._.c_void_p:
        raise MemoryError("officeTextAllocationFailed")
    return value


def _native_window(editor: _EditorIdentity):
    from comtypes import GUID  # type: ignore[import-untyped]
    from comtypes.automation import IDispatch
    from comtypes.client.dynamic import Dispatch  # type: ignore[import-untyped]

    api = ctypes.OleDLL("oleacc")
    api.AccessibleObjectFromWindow.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.POINTER(IDispatch)),
    ]
    dispatch = ctypes.POINTER(IDispatch)()
    api.AccessibleObjectFromWindow(editor.focus, 0xFFFFFFF0, ctypes.byref(IDispatch._iid_), ctypes.byref(dispatch))
    return Dispatch(dispatch)


def _insert_into_window(
    window,
    text: str,
    *,
    still_focused: Callable[[], bool],
    on_marker,
    expired: Callable[[], bool] = lambda: False,
) -> OfficeInsertOutcome:
    def read(operation):
        if expired():
            raise _OfficeDeadlineExpired
        return operation()

    selection = read(lambda: window.Selection)
    document = read(lambda: selection.Document)
    if (
        read(lambda: document.ReadOnly)
        or read(lambda: document.ProtectionType) != -1
        or read(lambda: selection.Type) not in {1, 2}
    ):
        return OfficeInsertOutcome.UNAVAILABLE
    comments = read(lambda: selection.Comments)
    if read(lambda: comments.Count):
        return OfficeInsertOutcome.UNAVAILABLE
    application = read(lambda: selection.Application)
    options = read(lambda: application.Options)
    if not read(lambda: options.ReplaceSelection):
        return OfficeInsertOutcome.UNAVAILABLE
    # Cell/row selection contains a structural marker; TypeText has special
    # behavior there. Keep that case on the established clipboard path.
    if "\x07" in (read(lambda: selection.Text) or ""):
        return OfficeInsertOutcome.UNAVAILABLE
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    if any(character in normalized for character in ("\0", "\x07")):
        return OfficeInsertOutcome.UNAVAILABLE
    value = _unicode_variant(normalized)
    dispatch = read(lambda: selection.TypeText)
    if not still_focused():
        return OfficeInsertOutcome.CANCELLED
    if on_marker:
        on_marker("paste_requested")
    try:
        dispatch(value)
        if on_marker:
            on_marker("paste")
    except Exception:
        return OfficeInsertOutcome.UNCERTAIN
    return OfficeInsertOutcome.INSERTED


def try_insert_office_text(
    text: str,
    *,
    validate_target: Callable[[], bool],
    on_marker: Callable[[str], None] | None = None,
) -> OfficeInsertOutcome:
    if sys.platform != "win32":
        return OfficeInsertOutcome.UNAVAILABLE
    api = _user32()
    editor = _focused_editor(api)
    if editor is None:
        return OfficeInsertOutcome.UNAVAILABLE
    started = time.perf_counter()
    stopped = threading.Event()
    expired = threading.Event()
    initialized = cancellation_enabled = False
    watchdog = None
    outcome = OfficeInsertOutcome.UNAVAILABLE
    window = None
    try:
        import comtypes

        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        initialized = True
        ole32 = ctypes.OleDLL("ole32")
        ole32.CoEnableCallCancellation(None)
        cancellation_enabled = True
        thread_id = threading.get_native_id()

        def cancel_if_stuck() -> None:
            _cancel_after_deadline(stopped, expired, lambda: ole32.CoCancelCall(thread_id, 0))

        watchdog = threading.Thread(target=cancel_if_stuck, name="scriber-office-call", daemon=True)
        watchdog.start()
        window = _native_window(editor)
        if expired.is_set():
            raise _OfficeDeadlineExpired
        model_hwnd = int(window.Hwnd)
        model_pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(model_hwnd, ctypes.byref(model_pid))
        if model_pid.value != editor.process_id:
            return OfficeInsertOutcome.UNAVAILABLE
        if api.GetAncestor(model_hwnd, 2) != editor.root:
            # Classic Outlook embeds a Word editor whose Window.Hwnd names a
            # hidden OpusApp host in that same Outlook process. The NativeOM
            # pointer still came directly from the focused message-body HWND.
            model_class = ctypes.create_unicode_buffer(128)
            api.GetClassNameW(model_hwnd, model_class, len(model_class))
            if editor.host_class != "rctrl_renwnd32" or model_class.value != "OpusApp":
                return OfficeInsertOutcome.UNAVAILABLE

        def still_focused() -> bool:
            return not expired.is_set() and _focused_editor(api) == editor and validate_target()

        outcome = _insert_into_window(
            window,
            text,
            still_focused=still_focused,
            on_marker=on_marker,
            expired=expired.is_set,
        )
        # Release apartment-owned COM objects before CoUninitialize.
        window = None
        return outcome
    except Exception as exc:
        outcome = OfficeInsertOutcome.CANCELLED if expired.is_set() else OfficeInsertOutcome.UNAVAILABLE
        # Do not retain apartment-owned objects in an exception traceback past
        # CoUninitialize, including a failed preflight property access.
        exc.__traceback__ = None
        return outcome
    finally:
        window = None
        stopped.set()
        if watchdog is not None:
            watchdog.join()
        if cancellation_enabled:
            with suppress(OSError):
                ole32.CoDisableCallCancellation(None)
        if initialized:
            comtypes.CoUninitialize()
        emit_event(
            logger,
            "Office text insertion finished",
            level="DEBUG",
            event="injector.office.complete",
            workflow="live_mic",
            stage="text_injection",
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            outcome="succeeded"
            if outcome is OfficeInsertOutcome.INSERTED
            else "skipped"
            if outcome in {OfficeInsertOutcome.UNAVAILABLE, OfficeInsertOutcome.CANCELLED}
            else "failed",
            meta={"restore_mode": "clipboardUntouched", "consumer_confirmed": outcome is OfficeInsertOutcome.INSERTED},
        )

"""A bounded Windows clipboard lease with a target-consumption receipt.

The message-only owner renders Unicode text on demand. Only a render request
from the intended process after paste dispatch permits early restoration, and
restoration acquires the clipboard before checking ownership and sequence.
Unknown readers retain the conservative fallback delay. No text or window
identity is logged, and the temporary text is excluded from Windows history.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes

from loguru import logger

from src.core.logging_setup import emit_event

_WM_RENDERFORMAT = 0x0305
_WM_RENDERALLFORMATS = 0x0306
_WM_DESTROYCLIPBOARD = 0x0307
_CF_UNICODETEXT = 13
_SINGLE_READ_CONTROL_CLASSES = {"edit"}


class WindowsClipboardLease:
    """Own one temporary clipboard publication until consumed or timed out."""

    def __init__(
        self,
        text: str,
        snapshot: Sequence[tuple[int, bytes]],
        *,
        target_process_id: int,
        restore_delay_ms: int,
        expected_sequence: int | None = None,
    ) -> None:
        self._text_bytes = text.encode("utf-16-le") + b"\0\0"
        self._snapshot = tuple(snapshot)
        self._target_process_id = target_process_id
        self._restore_delay = max(0, restore_delay_ms) / 1000
        self._expected_initial_sequence = expected_sequence
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._cancelled = threading.Event()
        self._dispatch_at: float | None = None
        self._restore_at: float | None = None
        self._consumed = False
        self._rendered = False
        self._published = False
        self._mutated = False
        self._sequence = 0
        self._hwnd = None
        self._created_at = time.perf_counter()
        self.status = "starting"

    @property
    def mutated(self) -> bool:
        return self._mutated

    def publish(self) -> bool:
        if sys.platform != "win32":
            return False
        threading.Thread(target=self._run, name="scriber-clipboard-lease", daemon=True).start()
        if not self._ready.wait(2):
            self._cancelled.set()
            # A late owner must observe cancellation before publication. If it
            # has already published, keep it responsible for restoring data.
            self.finish(dispatched=False)
            self.status = "ownerStartTimeout"
            return False
        return self._published

    def arm_for_paste(self) -> None:
        self._dispatch_at = time.perf_counter()

    def finish(self, *, dispatched: bool) -> None:
        self._restore_at = time.perf_counter() + (self._restore_delay if dispatched else 0)

    def wait(self, timeout: float) -> bool:
        return self._finished.wait(timeout)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(pointer, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, pointer)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        signatures = {
            "RegisterClassW": ([ctypes.POINTER(WNDCLASS)], wintypes.ATOM),
            "UnregisterClassW": ([wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            "CreateWindowExW": (
                [
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
                ],
                wintypes.HWND,
            ),
            "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, ctypes.c_size_t, pointer], pointer),
            "DestroyWindow": ([wintypes.HWND], wintypes.BOOL),
            "OpenClipboard": ([wintypes.HWND], wintypes.BOOL),
            "CloseClipboard": ([], wintypes.BOOL),
            "EmptyClipboard": ([], wintypes.BOOL),
            "GetClipboardOwner": ([], wintypes.HWND),
            "GetOpenClipboardWindow": ([], wintypes.HWND),
            "GetClipboardSequenceNumber": ([], wintypes.DWORD),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "SetClipboardData": ([wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
            "IsClipboardFormatAvailable": ([wintypes.UINT], wintypes.BOOL),
            "RegisterClipboardFormatW": ([wintypes.LPCWSTR], wintypes.UINT),
            "PeekMessageW": (
                [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT],
                wintypes.BOOL,
            ),
            "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], pointer),
            "MsgWaitForMultipleObjectsEx": (
                [wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD],
                wintypes.DWORD,
            ),
        }
        for name, (args, result) in signatures.items():
            function = getattr(user32, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
            ("GlobalAlloc", [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL),
            ("GlobalLock", [wintypes.HGLOBAL], wintypes.LPVOID),
            ("GlobalUnlock", [wintypes.HGLOBAL], wintypes.BOOL),
            ("GlobalFree", [wintypes.HGLOBAL], wintypes.HGLOBAL),
        ):
            function = getattr(kernel32, name)
            function.argtypes, function.restype = args, result

        def allocate(data: bytes):
            handle = kernel32.GlobalAlloc(0x0002, max(1, len(data)))
            if not handle:
                raise OSError("clipboardAllocationFailed")
            address = kernel32.GlobalLock(handle)
            if not address:
                kernel32.GlobalFree(handle)
                raise OSError("clipboardAllocationLockFailed")
            ctypes.memmove(address, data, len(data))
            kernel32.GlobalUnlock(handle)
            return handle

        def put(format_id: int, data: bytes) -> None:
            handle = allocate(data)
            if not user32.SetClipboardData(format_id, handle):
                kernel32.GlobalFree(handle)
                raise OSError("clipboardSetFailed")

        def render() -> None:
            if not self._rendered:
                put(_CF_UNICODETEXT, self._text_bytes)
                self._rendered = True
                self._sequence = int(user32.GetClipboardSequenceNumber())

        @wndproc_type
        def window_proc(hwnd, message, wparam, lparam):
            try:
                if message == _WM_RENDERFORMAT and wparam == _CF_UNICODETEXT:
                    reader = user32.GetOpenClipboardWindow()
                    reader_pid = wintypes.DWORD()
                    if reader:
                        user32.GetWindowThreadProcessId(reader, ctypes.byref(reader_pid))
                    reader_class = ctypes.create_unicode_buffer(128)
                    if reader:
                        user32.GetClassNameW(reader, reader_class, len(reader_class))
                    render()
                    self._consumed = bool(
                        self._dispatch_at is not None
                        and reader_pid.value
                        and reader_pid.value == self._target_process_id
                        and reader_class.value.casefold() in _SINGLE_READ_CONTROL_CLASSES
                    )
                    return 0
                if message == _WM_RENDERALLFORMATS:
                    if user32.OpenClipboard(hwnd):
                        try:
                            if user32.GetClipboardOwner() == hwnd:
                                render()
                        finally:
                            user32.CloseClipboard()
                    return 0
                if message == _WM_DESTROYCLIPBOARD:
                    return 0
            except Exception:
                self.status = "clipboardRenderFailed"
                logger.warning("Clipboard delayed rendering failed")
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        class_name = f"ScriberPasteLease_{id(self):x}"
        instance = kernel32.GetModuleHandleW(None)
        registered = False
        try:
            definition = WNDCLASS(lpfnWndProc=window_proc, hInstance=instance, lpszClassName=class_name)
            if not user32.RegisterClassW(ctypes.byref(definition)):
                raise OSError("clipboardOwnerRegistrationFailed")
            registered = True
            self._hwnd = user32.CreateWindowExW(0, class_name, None, 0, 0, 0, 0, 0, -3, None, instance, None)
            if not self._hwnd:
                raise OSError("clipboardOwnerCreationFailed")
            if self._cancelled.is_set():
                return
            for _ in range(5):
                if user32.OpenClipboard(self._hwnd):
                    break
                time.sleep(0.005)
            else:
                raise OSError("clipboardOpenFailed")
            try:
                if self._cancelled.is_set():
                    return
                if self._expected_initial_sequence and (
                    int(user32.GetClipboardSequenceNumber()) != self._expected_initial_sequence
                ):
                    raise OSError("clipboardChangedBeforePublish")
                if not user32.EmptyClipboard():
                    raise OSError("clipboardEmptyFailed")
                self._mutated = True
                # Windows clipboard history otherwise may consume the delayed
                # format before the intended target has a chance to paste it.
                for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                    format_id = user32.RegisterClipboardFormatW(name)
                    if format_id:
                        put(format_id, b"\0\0\0\0")
                user32.SetClipboardData(_CF_UNICODETEXT, None)
                if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
                    raise OSError("clipboardDelayedPublishFailed")
                self._sequence = int(user32.GetClipboardSequenceNumber())
                self._published = True
                self.status = "published"
            finally:
                user32.CloseClipboard()
            # Delayed publication advances the sequence when the clipboard is
            # closed. A pre-close number is stale until the first render.
            self._sequence = int(user32.GetClipboardSequenceNumber())
            self._ready.set()

            message = wintypes.MSG()
            # A missing finish call cannot leave an owner thread alive forever.
            hard_deadline = time.perf_counter() + self._restore_delay + 5
            while time.perf_counter() < hard_deadline:
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                if user32.GetClipboardOwner() != self._hwnd:
                    self.status = "clipboardChanged"
                    break
                restore_due = self._restore_at is not None and time.perf_counter() >= self._restore_at
                acknowledged = self._consumed and self._restore_at is not None
                if (acknowledged or restore_due or self._cancelled.is_set()) and user32.OpenClipboard(self._hwnd):
                    try:
                        # Both checks run under the same clipboard lock as
                        # EmptyClipboard. A newer user copy always wins.
                        if (
                            user32.GetClipboardOwner() != self._hwnd
                            or not self._sequence
                            or int(user32.GetClipboardSequenceNumber()) != self._sequence
                        ):
                            self.status = "clipboardChanged"
                            break
                        self._restore_snapshot(user32, kernel32, allocate)
                        self.status = "consumerAcknowledged" if self._consumed else "fallbackDelay"
                    finally:
                        user32.CloseClipboard()
                    break
                user32.MsgWaitForMultipleObjectsEx(0, None, 2 if self._consumed else 10, 0x04FF, 4)
            else:
                self.status = "clipboardBusyTimeout"
                # Preserve readable text when a stuck consumer prevents restore.
        except Exception as exc:
            self.status = str(exc) if isinstance(exc, OSError) else "clipboardOwnerFailed"
            if self._mutated and self._hwnd and user32.OpenClipboard(self._hwnd):
                try:
                    if user32.GetClipboardOwner() == self._hwnd:
                        self._restore_snapshot(user32, kernel32, allocate)
                except Exception:
                    logger.warning("Clipboard snapshot recovery failed")
                finally:
                    user32.CloseClipboard()
            emit_event(
                logger,
                "Clipboard lease did not complete",
                level="WARNING",
                event="injector.clipboard.failure",
                workflow="live_mic",
                stage="clipboard_restore",
                outcome="failed",
                error_category=self.status,
            )
        finally:
            self._ready.set()
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
            if registered:
                user32.UnregisterClassW(class_name, instance)
            self._snapshot = ()
            self._text_bytes = b""
            self._finished.set()
            if self._published:
                emit_event(
                    logger,
                    "Clipboard lease finished",
                    level="DEBUG",
                    event="injector.clipboard.restore",
                    workflow="live_mic",
                    stage="clipboard_restore",
                    duration_ms=round((time.perf_counter() - self._created_at) * 1000, 3),
                    outcome=(
                        "succeeded"
                        if self.status in {"consumerAcknowledged", "fallbackDelay"}
                        else "skipped"
                        if self.status == "clipboardChanged"
                        else "failed"
                    ),
                    meta={"restore_mode": self.status, "consumer_confirmed": self._consumed},
                )

    def _restore_snapshot(self, user32, kernel32, allocate: Callable) -> None:
        # Allocate before clearing, so allocation failure retains the current
        # readable clipboard instead of destroying it halfway through restore.
        handles = []
        try:
            for format_id, data in self._snapshot:
                handles.append([format_id, allocate(data)])
            if not user32.EmptyClipboard():
                raise OSError("clipboardRestoreEmptyFailed")
            # The owner HWND now contains the restored snapshot. Destruction
            # must not answer WM_RENDERALLFORMATS with the old temporary text.
            self._rendered = True
            for item in handles:
                if not user32.SetClipboardData(item[0], item[1]):
                    raise OSError("clipboardRestoreSetFailed")
                item[1] = None
        finally:
            for _, handle in handles:
                if handle:
                    kernel32.GlobalFree(handle)

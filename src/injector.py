import sys
import asyncio
import math
import os
import time
import threading
import ctypes
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Optional
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    TextFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
    StartFrame,
    EndFrame,
    StopFrame,
    CancelFrame,
)

from src.config import Config
from src.runtime.shell_ipc import call_shell_ipc, record_command_diagnostic

try:
    if sys.platform.startswith("linux") and "DISPLAY" not in os.environ:
        raise ImportError("Headless Linux detected")
    import pyautogui
    import keyboard
    HAS_GUI = True
except (ImportError, KeyError, OSError) as e:
    HAS_GUI = False
    pyautogui = None
    keyboard = None
    logger.warning(f"GUI libraries not available: {e}. Text injection will be mocked.")


class _ClipboardAccessFailed:
    pass


_CLIPBOARD_ACCESS_FAILED = _ClipboardAccessFailed()


@dataclass
class _ClipboardFormatSnapshot:
    format_id: int
    data: bytes


@dataclass
class _ClipboardSnapshot:
    formats: list[_ClipboardFormatSnapshot]
    unsupported_format_count: int = 0
    total_bytes: int = 0


_MAX_CLIPBOARD_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_CLIPBOARD_SNAPSHOT_FORMATS = 64

# Only these standard clipboard formats are HGLOBAL-backed and safe to copy
# with GlobalSize/GlobalLock. Formats such as CF_BITMAP and CF_ENHMETAFILE
# return GDI handles; treating them like HGLOBAL can crash the process.
_RESTORABLE_STANDARD_CLIPBOARD_FORMATS = {
    1,   # CF_TEXT
    7,   # CF_OEMTEXT
    8,   # CF_DIB
    13,  # CF_UNICODETEXT
    15,  # CF_HDROP
    16,  # CF_LOCALE
    17,  # CF_DIBV5
}


def _windows_clipboard_format_is_restorable(format_id: int) -> bool:
    return int(format_id) in _RESTORABLE_STANDARD_CLIPBOARD_FORMATS


# =============================================================================
# SendInput API for instant keystroke injection (Windows only)
# =============================================================================

# Windows input event constants
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


def _send_input_text(text: str) -> bool:
    """
    Inject text instantly using Windows SendInput API with Unicode events.
    This batches all characters into a single system call for maximum speed.

    Performance: ~10ms for any text length (vs 10ms PER CHARACTER with keyboard.write)
    For 500 chars: keyboard.write = 5000ms, SendInput = ~10ms (500x faster)
    """
    if sys.platform != "win32":
        return False

    try:
        user32 = ctypes.windll.user32

        # Build input events: each character needs key-down + key-up
        inputs = []
        for char in text:
            # Key down
            ki_down = KEYBDINPUT(
                wVk=0,
                wScan=ord(char),
                dwFlags=KEYEVENTF_UNICODE,
                time=0,
                dwExtraInfo=None,
            )
            input_down = INPUT(type=INPUT_KEYBOARD)
            input_down.ki = ki_down
            inputs.append(input_down)

            # Key up
            ki_up = KEYBDINPUT(
                wVk=0,
                wScan=ord(char),
                dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=None,
            )
            input_up = INPUT(type=INPUT_KEYBOARD)
            input_up.ki = ki_up
            inputs.append(input_up)

        if not inputs:
            return True

        # Convert to array and send all at once
        input_array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), input_array, ctypes.sizeof(INPUT))

        if sent != len(inputs):
            logger.warning(f"SendInput: sent {sent}/{len(inputs)} events")
            return False

        return True
    except Exception as e:
        logger.debug(f"SendInput failed: {e}")
        return False


def _active_window_title() -> str:
    if not HAS_GUI or not pyautogui:
        return ""
    try:
        return pyautogui.getActiveWindowTitle() or ""
    except Exception:
        return ""


def _expected_injection_target_title() -> str:
    configured = getattr(Config, "INJECT_TARGET_TITLE", "") or os.getenv(
        "SCRIBER_INJECT_TARGET_TITLE",
        "",
    )
    return str(configured or "").strip()


def _active_window_matches_expected_target(expected_title: str) -> bool:
    if not expected_title:
        return True
    return _active_window_title() == expected_title


def _foreground_target_guard_allows_dispatch(expected_title: str, *, phase: str) -> bool:
    if _active_window_matches_expected_target(expected_title):
        return True
    logger.warning(
        "Text injection skipped because foreground target title did not match "
        f"(phase={phase})"
    )
    return False


def _is_slow_app(title: str) -> bool:
    """Check if the active window is a known slow app that needs special handling."""
    title_lower = title.lower()
    # Word/Outlook are slow with per-keystroke injection and need pre-paste delays
    return title_lower.endswith(" - word") or title_lower.endswith(" - outlook")


def _should_paste_for_active_window() -> bool:
    """Check if clipboard paste should be used for the active window."""
    title = _active_window_title()
    if not title:
        return False
    return _is_slow_app(title)


def _get_pre_delay_for_window() -> int:
    """
    Get the appropriate pre-paste delay for the active window.
    Returns 0 for most apps (fast), or configured delay for slow apps like Word/Outlook.
    """
    title = _active_window_title()
    if title and _is_slow_app(title):
        # Slow apps need the full configured delay
        return max(0, int(getattr(Config, "PASTE_PRE_DELAY_MS", 80) or 80))
    # Fast path: no delay needed for most applications
    return 0


def _windows_clipboard_get_text(
    *,
    retries: int = 5,
    delay_secs: float = 0.005,
) -> str | None | _ClipboardAccessFailed:
    """
    Get text from Windows clipboard with optimized retry loop.
    OPTIMIZED: Reduced from 10 retries @ 20ms to 5 retries @ 5ms (200ms -> 25ms worst case)
    """
    if sys.platform != "win32":
        return None

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(delay_secs)
            continue
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return _CLIPBOARD_ACCESS_FAILED
            kernel32.GlobalLock.restype = wintypes.LPVOID
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return _CLIPBOARD_ACCESS_FAILED
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    logger.warning("Clipboard read access failed after retries")
    return _CLIPBOARD_ACCESS_FAILED


def _windows_clipboard_set_text(text: str, *, retries: int = 5, delay_secs: float = 0.005) -> bool:
    """
    Set text to Windows clipboard with optimized retry loop.
    OPTIMIZED: Reduced from 10 retries @ 20ms to 5 retries @ 5ms (200ms -> 25ms worst case)
    """
    if sys.platform != "win32":
        return False

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    data = text.encode("utf-16-le") + b"\x00\x00"

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(delay_secs)
            continue
        try:
            if not user32.EmptyClipboard():
                return False

            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not hglobal:
                return False

            kernel32.GlobalLock.restype = wintypes.LPVOID
            ptr = kernel32.GlobalLock(hglobal)
            if not ptr:
                kernel32.GlobalFree(hglobal)
                return False

            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(hglobal)

            # After SetClipboardData, the system owns the memory handle.
            if not user32.SetClipboardData(CF_UNICODETEXT, hglobal):
                kernel32.GlobalFree(hglobal)
                return False

            return True
        finally:
            user32.CloseClipboard()

    return False


def _windows_clipboard_sequence_number() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def _windows_clipboard_snapshot(
    *,
    retries: int = 5,
    delay_secs: float = 0.005,
) -> _ClipboardSnapshot | _ClipboardAccessFailed:
    if sys.platform != "win32":
        return _CLIPBOARD_ACCESS_FAILED

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(delay_secs)
            continue
        try:
            snapshot = _ClipboardSnapshot(formats=[])
            seen_formats: set[int] = set()
            format_id = int(user32.EnumClipboardFormats(0))
            while format_id:
                if format_id in seen_formats:
                    logger.warning("Clipboard format enumeration repeated a format; stopping snapshot")
                    break
                if len(seen_formats) >= _MAX_CLIPBOARD_SNAPSHOT_FORMATS:
                    logger.warning("Clipboard format enumeration exceeded snapshot limit; stopping snapshot")
                    break
                seen_formats.add(format_id)
                if not _windows_clipboard_format_is_restorable(format_id):
                    snapshot.unsupported_format_count += 1
                    format_id = int(user32.EnumClipboardFormats(format_id))
                    continue

                handle = user32.GetClipboardData(format_id)
                if not handle:
                    snapshot.unsupported_format_count += 1
                    format_id = int(user32.EnumClipboardFormats(format_id))
                    continue

                byte_len = int(kernel32.GlobalSize(handle))
                if byte_len <= 0:
                    snapshot.unsupported_format_count += 1
                    format_id = int(user32.EnumClipboardFormats(format_id))
                    continue
                if snapshot.total_bytes + byte_len > _MAX_CLIPBOARD_SNAPSHOT_BYTES:
                    logger.warning("Clipboard snapshot too large; refusing to overwrite clipboard")
                    return _CLIPBOARD_ACCESS_FAILED

                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    snapshot.unsupported_format_count += 1
                    format_id = int(user32.EnumClipboardFormats(format_id))
                    continue
                try:
                    data = ctypes.string_at(ptr, byte_len)
                finally:
                    kernel32.GlobalUnlock(handle)

                snapshot.formats.append(_ClipboardFormatSnapshot(format_id=format_id, data=data))
                snapshot.total_bytes += byte_len
                format_id = int(user32.EnumClipboardFormats(format_id))

            if not snapshot.formats and snapshot.unsupported_format_count:
                logger.warning("Clipboard contains only unsupported formats; refusing to overwrite clipboard")
                return _CLIPBOARD_ACCESS_FAILED
            return snapshot
        finally:
            user32.CloseClipboard()

    logger.warning("Clipboard snapshot access failed after retries")
    return _CLIPBOARD_ACCESS_FAILED


def _windows_clipboard_restore_snapshot(
    snapshot: _ClipboardSnapshot,
    *,
    retries: int = 5,
    delay_secs: float = 0.005,
) -> bool:
    if sys.platform != "win32":
        return False

    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(delay_secs)
            continue
        try:
            if not user32.EmptyClipboard():
                return False

            restored_any = False
            for item in snapshot.formats:
                if not _windows_clipboard_format_is_restorable(item.format_id):
                    logger.debug(f"Clipboard restore skipped format {item.format_id}: unsupported handle type")
                    continue
                hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(item.data))
                if not hglobal:
                    logger.debug(f"Clipboard restore skipped format {item.format_id}: GlobalAlloc failed")
                    continue
                ptr = kernel32.GlobalLock(hglobal)
                if not ptr:
                    kernel32.GlobalFree(hglobal)
                    logger.debug(f"Clipboard restore skipped format {item.format_id}: GlobalLock failed")
                    continue
                try:
                    ctypes.memmove(ptr, item.data, len(item.data))
                finally:
                    kernel32.GlobalUnlock(hglobal)

                # After SetClipboardData succeeds, the system owns the handle.
                if not user32.SetClipboardData(item.format_id, hglobal):
                    kernel32.GlobalFree(hglobal)
                    logger.debug(f"Clipboard restore skipped format {item.format_id}: SetClipboardData failed")
                    continue
                restored_any = True
            return restored_any or not snapshot.formats
        finally:
            user32.CloseClipboard()

    return False


def _paste_text(
    text: str,
    *,
    skip_clipboard_restore: bool = False,
    on_marker: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Inject text via clipboard paste (Ctrl+V).

    OPTIMIZATIONS:
    - App-specific pre-delay: 0ms for most apps, configured delay only for Word/Outlook
    - Optional skip_clipboard_restore for maximum speed (saves ~25ms + background thread)
    - Faster clipboard retry loops (25ms worst case vs 200ms before)
    """
    if not HAS_GUI:
        return False
    if sys.platform != "win32":
        return False

    expected_target_title = _expected_injection_target_title()
    if not _foreground_target_guard_allows_dispatch(
        expected_target_title,
        phase="before_clipboard_set",
    ):
        return False

    # Only save previous clipboard if we're going to restore it. This uses a
    # full format snapshot so image/file/HTML clipboard assets are not replaced
    # permanently by the transcript text.
    previous_clipboard = None if skip_clipboard_restore else _windows_clipboard_snapshot()

    if previous_clipboard is _CLIPBOARD_ACCESS_FAILED:
        logger.warning("Current clipboard could not be snapshotted; skipping paste to avoid overwriting it")
        return False

    if not _windows_clipboard_set_text(text):
        if isinstance(previous_clipboard, _ClipboardSnapshot):
            _windows_clipboard_restore_snapshot(previous_clipboard)
        return False
    clipboard_sequence_after_set = _windows_clipboard_sequence_number()
    if on_marker:
        on_marker("clipboard_set")

    paste_dispatched = False
    try:
        # OPTIMIZED: App-specific pre-delay (0ms for most apps, ~80ms only for Word/Outlook)
        pre_delay_ms = _get_pre_delay_for_window()
        if pre_delay_ms:
            time.sleep(pre_delay_ms / 1000.0)

        if not _foreground_target_guard_allows_dispatch(
            expected_target_title,
            phase="before_paste_dispatch",
        ):
            return False

        try:
            if keyboard and hasattr(keyboard, "press_and_release"):
                keyboard.press_and_release("ctrl+v")
            else:
                raise RuntimeError("keyboard.press_and_release unavailable")
        except Exception:
            if pyautogui and hasattr(pyautogui, "hotkey"):
                pyautogui.hotkey("ctrl", "v", interval=0.05)
            else:
                return False
        paste_dispatched = True
        if on_marker:
            on_marker("paste")

        if Config.DEBUG:
            logger.info(
                f"Injected via clipboard paste (pre_delay={pre_delay_ms}ms, restore={not skip_clipboard_restore})"
            )
        return True
    finally:
        if skip_clipboard_restore or not isinstance(previous_clipboard, _ClipboardSnapshot):
            pass  # Skip restoration for speed or no previous content
        else:
            restore_delay_ms = (
                max(0, int(getattr(Config, "PASTE_RESTORE_DELAY_MS", 0) or 0))
                if paste_dispatched
                else 0
            )

            def _restore_if_unchanged():
                try:
                    current_sequence = _windows_clipboard_sequence_number()
                    if (
                        clipboard_sequence_after_set is not None
                        and current_sequence is not None
                        and current_sequence != clipboard_sequence_after_set
                    ):
                        return
                    _windows_clipboard_restore_snapshot(previous_clipboard)
                except Exception:
                    pass

            if restore_delay_ms <= 0:
                _restore_if_unchanged()
            else:
                t = threading.Timer(restore_delay_ms / 1000.0, _restore_if_unchanged)
                t.daemon = True
                t.start()


def _tauri_inject_text(
    text: str,
    *,
    on_marker: Optional[Callable[..., None]] = None,
) -> bool:
    client_timeout_seconds = 2.5
    deadline_ms = 2000
    pre_delay_ms = max(0, int(getattr(Config, "PASTE_PRE_DELAY_MS", 80) or 80))
    expected_target_title = _expected_injection_target_title()

    payload = {
        "text": text,
        "restoreClipboard": True,
        "restoreDelayMs": max(0, int(getattr(Config, "PASTE_RESTORE_DELAY_MS", 1500) or 0)),
        "preDelayMode": "auto",
        "preDelayMs": pre_delay_ms,
        "dispatch": "ctrlV",
        "maxClipboardRetries": 5,
        "clipboardRetryDelayMs": 5,
        "deadlineMs": deadline_ms,
    }
    if expected_target_title:
        payload["expectedForegroundTitle"] = expected_target_title
    try:
        call_started_ns = time.perf_counter_ns()
        response = call_shell_ipc("injectText", payload, timeout_seconds=client_timeout_seconds)
        call_finished_ns = time.perf_counter_ns()
    except Exception as exc:
        logger.warning(f"Tauri text injection failed: {type(exc).__name__}")
        record_command_diagnostic(
            "injectText",
            False,
            error_code="transportException",
            fallback_reason=f"{type(exc).__name__}: {exc}",
        )
        return False
    if not response.get("success"):
        error_code = response.get("errorCode") or "unknown"
        fallback_reason = response.get("fallbackReason") or ""
        logger.warning(f"Tauri text injection failed: {error_code} {fallback_reason}".strip())
        record_command_diagnostic(
            "injectText",
            False,
            error_code=str(error_code),
            fallback_reason=str(fallback_reason),
            response=response,
        )
        return False

    response_payload = response.get("payload")
    if not isinstance(response_payload, dict) or response_payload.get("method") != "tauri":
        logger.warning("Tauri text injection failed: invalid shell IPC payload")
        record_command_diagnostic(
            "injectText",
            False,
            error_code="invalidPayload",
            fallback_reason="invalid shell IPC payload",
            response=response,
        )
        return False
    markers = response_payload.get("markers") if isinstance(response_payload, dict) else None
    required_markers = {"clipboard_set", "paste"}
    marker_set = (
        {marker for marker in markers if isinstance(marker, str)}
        if isinstance(markers, list)
        else set()
    )
    missing_markers = sorted(required_markers - marker_set)
    if missing_markers:
        missing_label = ", ".join(missing_markers)
        logger.warning(f"Tauri text injection failed: missing marker(s): {missing_label}")
        record_command_diagnostic(
            "injectText",
            False,
            error_code=(
                "missingPasteMarker"
                if missing_markers == ["paste"]
                else "missingInjectionMarker"
            ),
            fallback_reason=f"missing marker(s): {missing_label}",
            response=response,
        )
        return False
    if on_marker:
        for marker in markers:
            if marker in {"clipboard_set", "paste"}:
                timestamp_ns = _tauri_marker_timestamp_ns(
                    response_payload,
                    marker,
                    call_started_ns=call_started_ns,
                    call_finished_ns=call_finished_ns,
                )
                if timestamp_ns is None:
                    on_marker(marker)
                else:
                    on_marker(marker, timestamp_ns)
    record_command_diagnostic("injectText", True, response=response)
    return True


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _tauri_marker_timestamp_ns(
    response_payload: dict,
    marker: str,
    *,
    call_started_ns: int,
    call_finished_ns: int,
) -> int | None:
    timings = response_payload.get("timingsMs")
    if not isinstance(timings, dict):
        return None
    timing_key = {"clipboard_set": "clipboardSet", "paste": "pasteDispatch"}.get(
        marker
    )
    if not timing_key:
        return None
    total_ms = _finite_number(timings.get("total"))
    marker_ms = _finite_number(timings.get(timing_key))
    if total_ms is None or marker_ms is None:
        return None
    remaining_ms = max(0.0, total_ms - marker_ms)
    estimated_ns = int(call_finished_ns - (remaining_ms * 1_000_000))
    return max(call_started_ns, min(call_finished_ns, estimated_ns))


class TextInjector(FrameProcessor):
    def __init__(
        self,
        inject_immediately: bool = False,
        on_injected: Optional[Callable[[str], None]] = None,
        on_injection_marker: Optional[Callable[..., None]] = None,
    ):
        super().__init__()
        self.inject_immediately = inject_immediately
        self.on_injected = on_injected
        self.on_injection_marker = on_injection_marker
        self._buffer = []
        self._last_injected = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            # Skip interim injections to avoid cursor jitter.
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TranscriptionFrame):
            if frame.text and frame.text != self._last_injected:
                if self.inject_immediately:
                    self._inject_text(frame.text.strip() + " ")
                else:
                    # Buffer finalized transcript segments; inject as one block at end of utterance.
                    self._buffer.append(frame.text.strip())
                self._last_injected = frame.text
        elif isinstance(frame, StartFrame):
            self._buffer = []
            self._last_injected = ""
        elif isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            self.flush()

        await self.push_frame(frame, direction)

    def flush(self):
        if self._buffer:
            text = " ".join(self._buffer).strip()
            if text:
                logger.debug(f"TextInjector flush: injecting {len(text)} chars")
                self._inject_text(text + " ")
            else:
                logger.debug("TextInjector flush: buffer joined to empty string")
        else:
            logger.debug("TextInjector flush: buffer is empty")
        self._buffer = []

    def _inject_text(self, text: str):
        """
        Inject text into the active application.

        INJECTION PRIORITY (most reliable to fastest):
        1. Clipboard paste (~25-50ms) - RELIABLE, works in all apps including elevated ones
        2. SendInput API (~10ms) - faster but often blocked by UIPI privilege isolation
        3. keyboard.write (10ms/char) - legacy fallback, very slow

        For 500 chars:
        - Clipboard: ~50ms (RELIABLE)
        - SendInput: ~10ms (often fails due to UIPI)
        - keyboard.write: 5000ms (5 seconds)

        NOTE: Clipboard is the default because SendInput frequently fails when:
        - Target app runs with higher privileges (Admin)
        - UIPI (User Interface Privilege Isolation) blocks the input
        - App has certain security features enabled
        
        The ~20-40ms speed difference is negligible compared to reliability.
        """
        if getattr(Config, "DISABLE_TEXT_INJECTION", False):
            logger.info("Text injection disabled via SCRIBER_DISABLE_TEXT_INJECTION")
            return

        try:
            method = (getattr(Config, "INJECT_METHOD", "auto") or "auto").lower().strip()
        except Exception:
            method = "auto"
        if method not in {"auto", "type", "paste", "sendinput", "tauri"}:
            method = "auto"

        expected_target_title = _expected_injection_target_title()
        if not _foreground_target_guard_allows_dispatch(
            expected_target_title,
            phase="before_injection",
        ):
            return

        if method == "tauri":
            if _tauri_inject_text(text, on_marker=self._notify_injection_marker):
                self._notify_injected(text)
            return

        if not HAS_GUI:
            logger.info(f"[MOCK INJECT] {len(text)} chars")
            return

        paste_kwargs = {"skip_clipboard_restore": False}
        if self.on_injection_marker:
            paste_kwargs["on_marker"] = self._notify_injection_marker

        # Explicit modes are strict. Only "auto" falls back across methods.
        if method == "paste":
            if not _paste_text(text, **paste_kwargs):
                logger.warning("Clipboard paste injection failed")
            else:
                self._notify_injected(text)
            return

        if method == "sendinput":
            if not _foreground_target_guard_allows_dispatch(
                expected_target_title,
                phase="before_sendinput_dispatch",
            ):
                return
            if _send_input_text(text):
                if Config.DEBUG:
                    logger.info(f"Injected via SendInput ({len(text)} chars, instant)")
                self._notify_injected(text)
            else:
                logger.warning("SendInput injection failed")
            return

        if method == "auto":
            if _paste_text(text, **paste_kwargs):
                self._notify_injected(text)
                return
            logger.debug("Clipboard paste failed; trying SendInput")
            if not _foreground_target_guard_allows_dispatch(
                expected_target_title,
                phase="before_sendinput_fallback",
            ):
                return
            if _send_input_text(text):
                if Config.DEBUG:
                    logger.info(f"Injected via SendInput ({len(text)} chars, instant)")
                self._notify_injected(text)
                return
            logger.debug("SendInput also failed; falling back to keystroke typing")

        # Last resort: character-by-character typing (slow but most compatible)
        if not _foreground_target_guard_allows_dispatch(
            expected_target_title,
            phase="before_keyboard_dispatch",
        ):
            return
        try:
            keyboard.write(text, delay=0.01)  # 10ms per char
            self._notify_injected(text)
        except Exception:
            try:
                logger.warning("keyboard.write failed, falling back to pyautogui.")
                pyautogui.write(text, interval=0.01)
                self._notify_injected(text)
            except Exception as e:
                logger.error(f"Text injection failed with all methods: {e}")

    def _notify_injected(self, text: str) -> None:
        if not self.on_injected:
            return
        try:
            self.on_injected(text)
        except Exception as exc:
            logger.debug(f"TextInjector on_injected callback failed: {exc}")

    def _notify_injection_marker(
        self, marker: str, timestamp_ns: int | None = None
    ) -> None:
        if not self.on_injection_marker:
            return
        try:
            if timestamp_ns is None:
                self.on_injection_marker(marker)
            else:
                self.on_injection_marker(marker, timestamp_ns)
        except TypeError as exc:
            if timestamp_ns is None:
                logger.debug(f"TextInjector on_injection_marker callback failed: {exc}")
                return
            try:
                self.on_injection_marker(marker)
            except Exception as fallback_exc:
                logger.debug(f"TextInjector on_injection_marker callback failed: {fallback_exc}")
        except Exception as exc:
            logger.debug(f"TextInjector on_injection_marker callback failed: {exc}")

import sys
import asyncio
import os
import time
import threading
import ctypes
from ctypes import wintypes
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


def _windows_clipboard_get_text(*, retries: int = 5, delay_secs: float = 0.005) -> str | None:
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
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            kernel32.GlobalLock.restype = wintypes.LPVOID
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    return None


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


def _paste_text(text: str, *, skip_clipboard_restore: bool = False) -> bool:
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

    # Only save previous clipboard if we're going to restore it
    previous_text = None if skip_clipboard_restore else _windows_clipboard_get_text()

    if not _windows_clipboard_set_text(text):
        return False

    try:
        # OPTIMIZED: App-specific pre-delay (0ms for most apps, ~80ms only for Word/Outlook)
        pre_delay_ms = _get_pre_delay_for_window()
        if pre_delay_ms:
            time.sleep(pre_delay_ms / 1000.0)

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

        if Config.DEBUG:
            logger.info(
                f"Injected via clipboard paste (pre_delay={pre_delay_ms}ms, restore={not skip_clipboard_restore})"
            )
        return True
    finally:
        if skip_clipboard_restore or previous_text is None:
            pass  # Skip restoration for speed or no previous content
        else:
            restore_delay_ms = max(0, int(getattr(Config, "PASTE_RESTORE_DELAY_MS", 0) or 0))

            def _restore_if_unchanged():
                try:
                    current = _windows_clipboard_get_text()
                    # Only restore if the clipboard still contains our injected text
                    if current == text:
                        _windows_clipboard_set_text(previous_text)
                except Exception:
                    pass

            if restore_delay_ms <= 0:
                _restore_if_unchanged()
            else:
                t = threading.Timer(restore_delay_ms / 1000.0, _restore_if_unchanged)
                t.daemon = True
                t.start()


class TextInjector(FrameProcessor):
    def __init__(self, inject_immediately: bool = False):
        super().__init__()
        self.inject_immediately = inject_immediately
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
                self._inject_text(text + " ")
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
        if not HAS_GUI:
            logger.info(f"[MOCK INJECT] {text}")
            return

        try:
            method = (getattr(Config, "INJECT_METHOD", "auto") or "auto").lower().strip()
        except Exception:
            method = "auto"
        if method not in {"auto", "type", "paste", "sendinput"}:
            method = "auto"

        # Determine best method based on active window and config
        if method == "auto":
            # Default to clipboard paste - most reliable
            # SendInput often fails due to UIPI privilege isolation
            method = "paste"

        # Try clipboard paste first (reliable for all apps)
        if method in {"paste", "auto"}:
            if _paste_text(text, skip_clipboard_restore=False):
                return
            logger.debug("Clipboard paste failed; falling back to SendInput")

        # Try SendInput as fallback (faster but often blocked)
        if method in {"sendinput", "type", "paste"}:
            if _send_input_text(text):
                if Config.DEBUG:
                    logger.info(f"Injected via SendInput ({len(text)} chars, instant)")
                return
            logger.debug("SendInput also failed; falling back to keystroke typing")

        # Last resort: character-by-character typing (slow but most compatible)
        try:
            keyboard.write(text, delay=0.01)  # 10ms per char
        except Exception:
            try:
                logger.warning("keyboard.write failed, falling back to pyautogui.")
                pyautogui.write(text, interval=0.01)
            except Exception as e:
                logger.error(f"Text injection failed with all methods: {e}")

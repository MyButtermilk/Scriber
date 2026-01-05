import sys
import asyncio
import os
import time
import threading
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


def _active_window_title() -> str:
    if not HAS_GUI or not pyautogui:
        return ""
    try:
        return pyautogui.getActiveWindowTitle() or ""
    except Exception:
        return ""


def _should_paste_for_active_window() -> bool:
    # Word/Outlook can be very slow when receiving per-keystroke injection; prefer clipboard paste there.
    title = _active_window_title().lower()
    if not title:
        return False
    return title.endswith(" - word") or title.endswith(" - outlook")


def _windows_clipboard_get_text(*, retries: int = 10, delay_secs: float = 0.02) -> str | None:
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

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


def _windows_clipboard_set_text(text: str, *, retries: int = 10, delay_secs: float = 0.02) -> bool:
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

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


def _paste_text(text: str) -> bool:
    if not HAS_GUI:
        return False
    if sys.platform != "win32":
        return False

    previous_text = _windows_clipboard_get_text()
    if not _windows_clipboard_set_text(text):
        return False

    try:
        pre_delay_ms = max(0, int(getattr(Config, "PASTE_PRE_DELAY_MS", 0) or 0))
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
                f"Injected via clipboard paste (restore_delay_ms={getattr(Config, 'PASTE_RESTORE_DELAY_MS', None)})"
            )
        return True
    finally:
        if previous_text is None:
            return

        restore_delay_ms = max(0, int(getattr(Config, "PASTE_RESTORE_DELAY_MS", 0) or 0))

        def _restore_if_unchanged():
            try:
                current = _windows_clipboard_get_text()
                # Only restore if the clipboard still contains our injected text; don't clobber user clipboard changes.
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
                logger.debug(f"TextInjector: injecting TranscriptionFrame ({len(frame.text)} chars)")
                if self.inject_immediately:
                    self._inject_text(frame.text.strip() + " ")
                else:
                    # Buffer finalized transcript segments; inject as one block at end of utterance.
                    self._buffer.append(frame.text.strip())
                self._last_injected = frame.text
            else:
                logger.debug(f"TextInjector: skipping duplicate TranscriptionFrame ({len(frame.text) if frame.text else 0} chars)")
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
        if not HAS_GUI:
            logger.info(f"[MOCK INJECT] {text}")
            return

        try:
            method = (getattr(Config, "INJECT_METHOD", "auto") or "auto").lower().strip()
        except Exception:
            method = "auto"
        if method not in {"auto", "type", "paste"}:
            method = "auto"
        if method == "auto":
            method = "paste" if _should_paste_for_active_window() else "type"

        if method == "paste":
            if _paste_text(text):
                return
            logger.debug("Clipboard paste injection failed; falling back to keystroke typing")

        try:
            keyboard.write(text)
        except Exception:
            try:
                logger.warning("keyboard.write failed, falling back to pyautogui.")
                pyautogui.write(text)
            except Exception as e:
                logger.error(f"Text injection failed with both libraries: {e}")

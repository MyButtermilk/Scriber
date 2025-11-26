import sys
import asyncio
import os
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame, InterimTranscriptionFrame

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

class TextInjector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._last_interim_len = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            self._inject_text(frame.text + " ")
            self._last_interim_len = 0
        elif isinstance(frame, InterimTranscriptionFrame):
            # Skip interim injections to avoid cursor jitter in target apps.
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    def _clear_interim(self):
        if not HAS_GUI or self._last_interim_len == 0:
            return
        try:
            for _ in range(self._last_interim_len):
                keyboard.press_and_release('backspace')
        except Exception:
            logger.error("Failed to clear interim text.")
        self._last_interim_len = 0

    def _inject_text(self, text: str):
        if not HAS_GUI:
            logger.info(f"[MOCK INJECT] {text}")
            return

        try:
            keyboard.write(text)
        except Exception:
            try:
                logger.warning("keyboard.write failed, falling back to pyautogui.")
                pyautogui.write(text)
            except Exception as e:
                logger.error(f"Text injection failed with both libraries: {e}")

import sys
import asyncio
import os
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame, InterimTranscriptionFrame, StartFrame, EndFrame

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
        self._buffer = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            # Skip interim injections to avoid cursor jitter.
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TranscriptionFrame):
            # Accumulate final pieces; inject later.
            self._buffer.append(frame.text)
        elif isinstance(frame, StartFrame):
            self._buffer = []
        elif isinstance(frame, EndFrame):
            if self._buffer:
                full_text = " ".join(self._buffer).strip() + " "
                self._inject_text(full_text)
                self._buffer = []

        await self.push_frame(frame, direction)

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

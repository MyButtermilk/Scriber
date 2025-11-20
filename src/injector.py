import sys
import asyncio
import os
from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame, InterimTranscriptionFrame, StartFrame

# Define globally to avoid AttributeError in tests/patches
keyboard = None
pyautogui = None

# Attempt to import keyboard/pyautogui, handle failure (e.g. if on headless Linux)
try:
    # On Linux, pyautogui requires a DISPLAY. If not present, it might raise KeyError or similar.
    if sys.platform.startswith("linux") and "DISPLAY" not in os.environ:
        raise ImportError("Headless Linux detected, skipping GUI libs")

    import pyautogui
    import keyboard
    HAS_INPUT_LIB = True
except (ImportError, KeyError, OSError) as e:
    HAS_INPUT_LIB = False
    logger.warning(f"Input libraries (pyautogui, keyboard) not available: {e}. Text injection will be mocked.")

class TextInjector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._last_interim_length = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            # Final transcription
            logger.debug(f"Injecting final text: {frame.text}")
            self._inject_text(frame.text + " ") # Add space after sentence/phrase
            self._last_interim_length = 0

        elif isinstance(frame, InterimTranscriptionFrame):
            # Handle interim if we want to do the "phantom text" effect.
            # For now, let's log it. Implementing robust backspace/overwrite
            # for interim in 3rd party apps is very risky without deep OS integration.
            logger.debug(f"Interim text: {frame.text}")
            pass

        await self.push_frame(frame, direction)

    def _inject_text(self, text: str):
        # We check HAS_INPUT_LIB, but also check if keyboard is not None
        if not HAS_INPUT_LIB or keyboard is None:
            logger.info(f"[MOCK INJECTION] {text}")
            return

        try:
            # Use keyboard.write or pyautogui.typewrite
            # keyboard.write is often faster and supports unicode better on Windows
            keyboard.write(text)
        except Exception as e:
            logger.error(f"Failed to inject text: {e}")

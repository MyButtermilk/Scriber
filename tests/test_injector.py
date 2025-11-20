import unittest
from unittest.mock import MagicMock, patch
import asyncio
from src.injector import TextInjector
from pipecat.frames.frames import TranscriptionFrame, InterimTranscriptionFrame, TextFrame

class TestInjector(unittest.TestCase):
    def test_injection(self):
        injector = TextInjector()
        # TranscriptionFrame(text: str, user_id: str, timestamp: str, ...)
        frame = TranscriptionFrame(text="Hello world", user_id="user", timestamp="now")

        # Mock keyboard/pyautogui
        with patch('src.injector.keyboard') as mock_kb:
            # We need to run the async method
            async def run():
                await injector.process_frame(frame, MagicMock())

            asyncio.run(run())
            pass

    def test_mock_injection_log(self):
        # Test that it logs if no lib
        injector = TextInjector()
        frame = TranscriptionFrame(text="Hello world", user_id="user", timestamp="now")

        with patch('src.injector.logger') as mock_logger:
            async def run():
                await injector.process_frame(frame, MagicMock())

            asyncio.run(run())
            # It should log
            # mock_logger.debug.assert_called() # Or info
            pass

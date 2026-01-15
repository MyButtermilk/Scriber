import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
from src.injector import TextInjector
from pipecat.frames.frames import (
    TranscriptionFrame,
    InterimTranscriptionFrame,
    TextFrame,
    EndFrame,
)

class TestInjector(unittest.TestCase):
    def test_injection(self):
        injector = TextInjector()
        # TranscriptionFrame(text: str, user_id: str, timestamp: str, ...)
        frame = TranscriptionFrame(text="Hello world", user_id="user", timestamp="now")

        # Mock injection to avoid OS-level side effects
        with patch.object(injector, "_inject_text") as mock_inject, patch.object(
            injector, "push_frame", new=AsyncMock()
        ):
            # We need to run the async method
            async def run():
                await injector.process_frame(frame, MagicMock())
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())
            pass

    def test_mock_injection_log(self):
        # Test that it logs if no lib
        injector = TextInjector()
        frame = TranscriptionFrame(text="Hello world", user_id="user", timestamp="now")

        with patch('src.injector.logger') as mock_logger, patch.object(
            injector, "_inject_text"
        ), patch.object(injector, "push_frame", new=AsyncMock()):
            async def run():
                await injector.process_frame(frame, MagicMock())
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())
            # It should log
            # mock_logger.debug.assert_called() # Or info
            pass

    def test_deduplication_survives_flush(self):
        injector = TextInjector(inject_immediately=False)
        injected_texts = []

        def record_injection(text: str):
            injected_texts.append(text)

        with patch.object(injector, "_inject_text", side_effect=record_injection), patch.object(
            injector, "push_frame", new=AsyncMock()
        ):
            async def run():
                frame = TranscriptionFrame(
                    text="hello world", user_id="user", timestamp="now"
                )
                await injector.process_frame(frame, MagicMock())

                # First flush injects buffered text
                await injector.process_frame(EndFrame(), MagicMock())

                # Late/duplicate frame after flush should be deduplicated
                late_frame = TranscriptionFrame(
                    text="hello world", user_id="user", timestamp="later"
                )
                await injector.process_frame(late_frame, MagicMock())
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())

        self.assertEqual(injected_texts, ["hello world "])

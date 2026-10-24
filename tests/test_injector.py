import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import (
    EndFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)

from src.injector import TextInjector


class TestInjector(unittest.TestCase):
    def test_deduplication_survives_flush(self):
        injector = TextInjector(inject_immediately=False)
        injected_texts = []

        def record_injection(text: str):
            injected_texts.append(text)

        with (
            patch.object(injector, "_inject_text", side_effect=record_injection),
            patch.object(injector, "push_frame", new=AsyncMock()),
        ):

            async def run():
                frame = TranscriptionFrame(text="hello world", user_id="user", timestamp="now")
                await injector.process_frame(frame, MagicMock())

                # First flush injects buffered text
                await injector.process_frame(EndFrame(), MagicMock())

                # Late/duplicate frame after flush should be deduplicated
                late_frame = TranscriptionFrame(text="hello world", user_id="user", timestamp="later")
                await injector.process_frame(late_frame, MagicMock())
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())

        self.assertEqual(injected_texts, ["hello world "])

    def test_injection_failure_does_not_raise_from_flush(self):
        injector = TextInjector(inject_immediately=False)

        with (
            patch.object(
                injector,
                "_inject_text",
                side_effect=OverflowError("int too long to convert"),
            ),
            patch.object(injector, "push_frame", new=AsyncMock()) as push_frame,
        ):

            async def run():
                frame = TranscriptionFrame(text="hello world", user_id="user", timestamp="now")
                await injector.process_frame(frame, MagicMock())
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())

        self.assertEqual(push_frame.await_count, 2)

    def test_interim_transcript_is_never_buffered_or_injected(self):
        injector = TextInjector(inject_immediately=False)
        injected_texts = []

        with (
            patch.object(
                injector,
                "_inject_text",
                side_effect=injected_texts.append,
            ),
            patch.object(injector, "push_frame", new=AsyncMock()),
        ):

            async def run():
                await injector.process_frame(
                    InterimTranscriptionFrame(
                        text="unstable preview",
                        user_id="user",
                        timestamp="now",
                    ),
                    MagicMock(),
                )
                await injector.process_frame(
                    TranscriptionFrame(
                        text="committed final",
                        user_id="user",
                        timestamp="later",
                    ),
                    MagicMock(),
                )
                await injector.process_frame(EndFrame(), MagicMock())

            asyncio.run(run())

        self.assertEqual(injected_texts, ["committed final "])

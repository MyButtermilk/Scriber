import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)

from src import injector as injector_module
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


def _final(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="user", timestamp="now")


def test_immediate_injection_keeps_the_event_loop_responsive_and_ordered():
    injector = TextInjector(inject_immediately=True)
    injected: list[tuple[str, str]] = []

    def slow_injection(text: str) -> None:
        # Clipboard leases, Office COM, and app pre-delays block like this.
        time.sleep(0.15)
        injected.append((text, threading.current_thread().name))

    async def run() -> int:
        ticks = 0
        done = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        for text in ("first", "second", "third"):
            await injector.process_frame(_final(text), MagicMock())
        done.set()
        await beat
        return ticks

    with (
        patch.object(injector, "_inject_text", side_effect=slow_injection),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        ticks = asyncio.run(run())

    assert [text for text, _thread in injected] == ["first ", "second ", "third "]
    assert all(thread.startswith("scriber-text-injection") for _text, thread in injected)
    # Three 150 ms insertions on the loop would have allowed no heartbeat.
    assert ticks >= 10


def test_cancelled_flush_still_finishes_the_started_insertion():
    injector = TextInjector(inject_immediately=False)
    started = threading.Event()
    release = threading.Event()
    injected: list[str] = []

    def blocking_injection(text: str) -> None:
        started.set()
        release.wait(5)
        injected.append(text)

    async def run() -> bool:
        await injector.process_frame(_final("dictated text"), MagicMock())
        flush = asyncio.create_task(injector.process_frame(EndFrame(), MagicMock()))
        await asyncio.to_thread(started.wait, 5)
        flush.cancel()
        await asyncio.sleep(0.05)
        still_waiting = not flush.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await flush
        return still_waiting

    with (
        patch.object(injector, "_inject_text", side_effect=blocking_injection),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        still_waiting = asyncio.run(run())

    # A started paste cannot be withdrawn; cancellation surfaces only after it.
    assert still_waiting is True
    assert injected == ["dictated text "]


def test_inject_text_once_async_uses_the_injection_worker(monkeypatch):
    calls: list[tuple[str, str]] = []

    def inject_once(text: str, **kwargs) -> bool:
        calls.append((text, threading.current_thread().name))
        return True

    monkeypatch.setattr(injector_module, "inject_text_once", inject_once)

    assert asyncio.run(injector_module.inject_text_once_async("hello ", injection_method="paste")) is True
    assert calls[0][0] == "hello "
    assert calls[0][1].startswith("scriber-text-injection")


def test_cancelled_queued_insertion_is_withdrawn_before_dispatch():
    occupied = threading.Event()
    release = threading.Event()
    dispatched: list[str] = []

    def occupy_worker() -> bool:
        occupied.set()
        release.wait(5)
        dispatched.append("earlier session")
        return True

    def stale_insertion() -> bool:
        dispatched.append("cancelled session")
        return True

    async def run() -> None:
        earlier = asyncio.create_task(injector_module._run_text_injection(occupy_worker))
        await asyncio.to_thread(occupied.wait, 5)
        queued = asyncio.create_task(injector_module._run_text_injection(stale_insertion))
        await asyncio.sleep(0.05)
        queued.cancel()
        # Withdrawn work must not wait for the busy worker.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, timeout=1)
        release.set()
        assert await earlier is True
        # A later insertion still runs, proving the stale one was skipped.
        assert await injector_module._run_text_injection(lambda: True) is True

    asyncio.run(run())

    # By the time the worker frees up, focus may belong to another application.
    assert dispatched == ["earlier session"]

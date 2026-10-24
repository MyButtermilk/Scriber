import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    CancelFrame,
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
        await injector.process_frame(EndFrame(), MagicMock())
        done.set()
        await beat
        return ticks

    with (
        patch.object(injector, "_inject_text", side_effect=slow_injection),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        ticks = asyncio.run(run())

    # Finals that arrive before the worker picks them up are inserted together.
    assert [text for text, _thread in injected] == ["first second third "]
    assert all(thread.startswith("scriber-text-injection") for _text, thread in injected)
    # Insertions on the loop would have allowed no heartbeat.
    assert ticks >= 10


def test_immediate_finals_behind_a_busy_insertion_are_coalesced_in_order():
    injector = TextInjector(inject_immediately=True)
    first_started = threading.Event()
    release_first = threading.Event()
    injected: list[str] = []

    def lease_bound_injection(text: str) -> None:
        if not injected:
            # A clipboard paste waits for the previous paste's lease.
            first_started.set()
            release_first.wait(5)
        injected.append(text)

    async def run() -> None:
        await injector.process_frame(_final("Erster Satz."), MagicMock())
        await asyncio.to_thread(first_started.wait, 5)
        for text in ("Zweiter Satz.", "Dritter Satz.", "Vierter Satz."):
            # Finals no longer wait behind the busy worker one by one.
            await asyncio.wait_for(injector.process_frame(_final(text), MagicMock()), timeout=0.5)
        release_first.set()
        await injector.process_frame(EndFrame(), MagicMock())

    with (
        patch.object(injector, "_inject_text", side_effect=lease_bound_injection),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        asyncio.run(run())

    # One lease wait for the backlog instead of one per final.
    assert injected == ["Erster Satz. ", "Zweiter Satz. Dritter Satz. Vierter Satz. "]


def test_cancelled_process_task_still_inserts_finals_that_arrived():
    injector = TextInjector(inject_immediately=True)
    occupied = threading.Event()
    release = threading.Event()
    injected: list[str] = []

    def occupy_worker() -> bool:
        occupied.set()
        release.wait(5)
        return True

    async def run() -> None:
        earlier = asyncio.create_task(injector_module._run_text_injection(occupy_worker))
        await asyncio.to_thread(occupied.wait, 5)
        # The Soniox manual stop ends the pipeline with CancelFrame, which
        # cancels the processor's in-flight process_frame.
        processing = asyncio.create_task(injector.process_frame(_final("letzte Worte"), MagicMock()))
        await asyncio.sleep(0.05)
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        release.set()
        await earlier
        await injector.process_frame(CancelFrame(), MagicMock())

    with (
        patch.object(injector, "_inject_text", side_effect=injected.append),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        asyncio.run(run())

    assert injected == ["letzte Worte "]


def test_cleanup_waits_for_pending_immediate_insertions():
    injector = TextInjector(inject_immediately=True)
    injected: list[str] = []

    def slow_injection(text: str) -> None:
        time.sleep(0.1)
        injected.append(text)

    async def run() -> None:
        await injector.process_frame(_final("tail"), MagicMock())
        await injector.cleanup()

    with (
        patch.object(injector, "_inject_text", side_effect=slow_injection),
        patch.object(injector, "push_frame", new=AsyncMock()),
    ):
        asyncio.run(run())

    # The pipeline task finishes only after its finals reached the target.
    assert injected == ["tail "]


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

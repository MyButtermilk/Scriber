"""Cancellation discipline for work a thread cannot abandon."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import pytest
from loguru import logger

from src.runtime.cancellation import (
    await_with_delayed_cancellation,
    remove_tree_if_exists,
    to_thread_cancellation_barrier,
)


@contextmanager
def _captured_loguru():
    """Collect loguru output; the module logs through loguru, not stdlib."""
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(str(message)), level="ERROR")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _blocking_mutation(started: threading.Event, release: threading.Event, finished: threading.Event):
    def run() -> str:
        started.set()
        assert release.wait(timeout=5.0)
        finished.set()
        return "committed"

    return run


@pytest.mark.asyncio
async def test_the_barrier_lets_a_thread_finish_before_cancellation_lands():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    task = asyncio.create_task(to_thread_cancellation_barrier(_blocking_mutation(started, release, finished)))
    assert await asyncio.to_thread(started.wait, 2.0)

    task.cancel()
    await asyncio.sleep(0)
    # The worker owns a durable boundary, so cancelling must not unwind yet.
    assert not task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_the_boundary():
    """Shutdown and an explicit cancel can both call Task.cancel."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    task = asyncio.create_task(to_thread_cancellation_barrier(_blocking_mutation(started, release, finished)))
    assert await asyncio.to_thread(started.wait, 2.0)

    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_the_barrier_returns_its_result_when_nobody_cancels():
    assert await to_thread_cancellation_barrier(lambda: "committed") == "committed"


@pytest.mark.asyncio
async def test_a_failing_mutation_propagates_when_nobody_cancels():
    def explode() -> None:
        raise RuntimeError("store is closed")

    with pytest.raises(RuntimeError, match="store is closed"):
        await to_thread_cancellation_barrier(explode)


@pytest.mark.asyncio
async def test_a_failing_mutation_still_reports_cancellation_to_a_cancelling_caller():
    """A caller that asked to stop unwinds through CancelledError.

    Callers of this barrier are already in a cancellation path and handle
    CancelledError there. Letting the mutation's own failure escape instead
    would skip that cleanup and surface an unexpected exception during
    shutdown, so the failure is logged and the pending cancel wins.
    """
    started, release = threading.Event(), threading.Event()

    def explode() -> None:
        started.set()
        assert release.wait(timeout=5.0)
        raise RuntimeError("store is closed")

    task = asyncio.create_task(to_thread_cancellation_barrier(explode))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    # The barrier absorbed the cancel and is still holding the boundary.
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_swallowed_failure_is_never_silent():
    """The pending cancel hides the failure from the caller, so it is logged."""
    started, release = threading.Event(), threading.Event()

    def explode() -> None:
        started.set()
        assert release.wait(timeout=5.0)
        raise RuntimeError("store is closed")

    task = asyncio.create_task(to_thread_cancellation_barrier(explode))
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()

    with _captured_loguru() as records, pytest.raises(asyncio.CancelledError):
        await task
    assert any("store is closed" in record for record in records)


@pytest.mark.asyncio
async def test_delayed_cancellation_hands_back_both_the_result_and_the_cancel():
    """The caller records the ownership it just acquired, then unwinds."""
    started, release = threading.Event(), threading.Event()
    captured: dict[str, object] = {}

    def acquire() -> str:
        started.set()
        assert release.wait(timeout=5.0)
        return "capture-7"

    async def caller() -> None:
        result, pending = await await_with_delayed_cancellation(asyncio.to_thread(acquire))
        captured["result"] = result
        captured["pending"] = pending
        if pending is not None:
            raise pending

    task = asyncio.create_task(caller())
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured["result"] == "capture-7"
    assert isinstance(captured["pending"], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_delayed_cancellation_reports_no_pending_cancel_on_the_happy_path():
    result, pending = await await_with_delayed_cancellation(asyncio.to_thread(lambda: 42))
    assert result == 42
    assert pending is None


@pytest.mark.asyncio
async def test_delayed_cancellation_accepts_an_already_scheduled_future():
    """create_task rejects a Future; the public contract accepts one."""

    async def work() -> str:
        return "done"

    result, pending = await await_with_delayed_cancellation(asyncio.gather(work()))
    assert result == ["done"]
    assert pending is None


@pytest.mark.asyncio
async def test_removing_a_tree_is_safe_when_it_is_already_gone(tmp_path):
    target = tmp_path / "workspace"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "audio.wav").write_bytes(b"x")

    await remove_tree_if_exists(target)
    assert not target.exists()

    # A second pass models cleanup running twice after a crash.
    await remove_tree_if_exists(target)
    assert not target.exists()


@pytest.mark.asyncio
async def test_delayed_cancellation_also_reports_a_failure_as_cancellation():
    """Same rule as the barrier: the caller asked to stop, so it unwinds that way."""
    started, release = threading.Event(), threading.Event()

    def explode() -> None:
        started.set()
        assert release.wait(timeout=5.0)
        raise RuntimeError("sidecar refused the claim")

    async def caller() -> None:
        await await_with_delayed_cancellation(asyncio.to_thread(explode))

    task = asyncio.create_task(caller())
    assert await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with _captured_loguru() as records, pytest.raises(asyncio.CancelledError):
        await task
    assert any("sidecar refused the claim" in record for record in records)

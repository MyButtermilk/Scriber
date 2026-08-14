"""Cancellation discipline for work a thread cannot abandon."""

from __future__ import annotations

import asyncio
import threading

import pytest

from src.runtime.cancellation import (
    await_with_delayed_cancellation,
    remove_tree_if_exists,
    to_thread_cancellation_barrier,
)


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
async def test_a_failing_mutation_propagates_its_failure_even_to_a_cancelling_caller():
    """Documents current behaviour, which is not what the source suggests.

    ``to_thread_cancellation_barrier`` ends with a handler that converts a
    failed mutation into the pending ``CancelledError`` when the caller was
    cancelling. That branch is unreachable: the loop awaits
    ``asyncio.shield(worker)``, which re-raises the worker's exception, so
    control leaves the loop before ``worker.result()`` is ever called. The
    caller therefore sees the mutation's own failure, and the accompanying
    "failed while its caller was canceling" log line never fires.

    Arguably the real failure is the more useful thing to surface, so this test
    pins the behaviour rather than the intent. Deciding between the two is a
    change of contract, not of structure.
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

    with pytest.raises(RuntimeError, match="store is closed"):
        await task


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

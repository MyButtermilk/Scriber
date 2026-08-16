from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from src.runtime.task_supervisor import AsyncTaskSupervisor


@pytest.mark.asyncio
async def test_drain_reports_pending_work_then_observes_completion() -> None:
    supervisor = AsyncTaskSupervisor(owner="test")
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()

    task = supervisor.spawn(work(), name="test-work")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert supervisor.pending_count == 1
    assert await supervisor.drain(timeout_seconds=0.0) == 1

    release.set()
    assert await supervisor.drain(timeout_seconds=1.0) == 0
    assert task.done()
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_cancelled_drain_finishes_owned_work() -> None:
    supervisor = AsyncTaskSupervisor(owner="test")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    supervisor.spawn(work(), name="test-cancel")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert await supervisor.drain(timeout_seconds=1.0, cancel=True) == 0
    assert cancelled.is_set()
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_zero_budget_cancel_still_requests_task_cancellation() -> None:
    supervisor = AsyncTaskSupervisor(owner="test")
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.Event().wait()

    task = supervisor.spawn(work(), name="zero-budget-cancel")
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert await supervisor.close(timeout_seconds=0.0, cancel=True) == 1
    assert task.cancelling()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_cancel_before_first_scheduler_step_closes_the_source_coroutine() -> None:
    supervisor = AsyncTaskSupervisor(owner="test")

    async def work() -> None:
        await asyncio.Event().wait()

    source = work()
    task = supervisor.spawn(source, name="cancel-before-start")
    try:
        assert task is not None
        assert await supervisor.drain(timeout_seconds=1.0, cancel=True) == 0
        assert task.cancelled()
        assert inspect.getcoroutinestate(source) == inspect.CORO_CLOSED
    finally:
        if inspect.getcoroutinestate(source) != inspect.CORO_CLOSED:
            source.close()


@pytest.mark.asyncio
async def test_close_waits_for_an_already_accepted_thread_submission() -> None:
    supervisor = AsyncTaskSupervisor(owner="test")
    loop = asyncio.get_running_loop()
    factory_called = False
    accepted: list[bool] = []

    async def work() -> None:
        await asyncio.Event().wait()

    def factory():
        nonlocal factory_called
        factory_called = True
        return work()

    thread = threading.Thread(
        target=lambda: accepted.append(supervisor.submit(loop, factory, name="thread-submission")),
    )
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert accepted == [True]

    assert await supervisor.close(timeout_seconds=1.0, cancel=True) == 0
    assert factory_called is True
    assert supervisor.pending_count == 0

    rejected_factory_called = False

    def rejected_factory():
        nonlocal rejected_factory_called
        rejected_factory_called = True
        return work()

    assert supervisor.submit(loop, rejected_factory, name="late-submission") is False
    await asyncio.sleep(0)
    assert rejected_factory_called is False


@pytest.mark.asyncio
async def test_task_failure_is_reported_through_the_loop_handler_once() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    supervisor = AsyncTaskSupervisor(owner="test-owner")

    async def fail() -> None:
        raise RuntimeError("synthetic failure")

    try:
        task = supervisor.spawn(fail(), name="test-failure")
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
    except RuntimeError:
        pass
    finally:
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)

    assert len(contexts) == 1
    assert contexts[0]["message"] == "test-owner background task failed"
    assert contexts[0]["task"] is task
    assert isinstance(contexts[0]["exception"], RuntimeError)
    assert supervisor.pending_count == 0

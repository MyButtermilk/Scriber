import asyncio

import pytest

from src.runtime.retry_scheduler import RetryScheduler


@pytest.mark.asyncio
async def test_retry_scheduler_coalesces_to_earliest_due_time():
    loop = asyncio.get_running_loop()
    calls: list[int] = []

    async def _trigger() -> None:
        calls.append(1)

    scheduler = RetryScheduler(loop=loop, trigger=_trigger)
    scheduler.schedule_in(0.05)
    scheduler.schedule_in(0.20)
    await asyncio.sleep(0.08)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_scheduler_can_be_rescheduled_earlier():
    loop = asyncio.get_running_loop()
    calls: list[int] = []

    async def _trigger() -> None:
        calls.append(1)

    scheduler = RetryScheduler(loop=loop, trigger=_trigger)
    scheduler.schedule_in(0.20)
    scheduler.schedule_in(0.01)
    await asyncio.sleep(0.05)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_scheduler_cancel_prevents_scheduled_trigger():
    loop = asyncio.get_running_loop()
    called = False

    async def _trigger() -> None:
        nonlocal called
        called = True

    scheduler = RetryScheduler(loop=loop, trigger=_trigger)
    scheduler.schedule_in(0.01)
    scheduler.cancel(cancel_running=True)
    await asyncio.sleep(0.03)

    assert called is False


@pytest.mark.asyncio
async def test_retry_scheduler_can_cancel_in_flight_trigger():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def _trigger() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    scheduler = RetryScheduler(loop=loop, trigger=_trigger)
    scheduler.schedule_in(0)
    await started.wait()
    scheduler.cancel(cancel_running=True)
    await asyncio.wait_for(canceled.wait(), timeout=1)

    assert scheduler._task is None or scheduler._task.cancelled()


@pytest.mark.asyncio
async def test_retry_scheduler_coalesces_requests_while_trigger_is_running():
    loop = asyncio.get_running_loop()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def _trigger() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()

    scheduler = RetryScheduler(loop=loop, trigger=_trigger)
    scheduler.schedule_in(0)
    await first_started.wait()
    first_task = scheduler._task
    scheduler.schedule_in(0)
    scheduler.schedule_in(0.05)
    scheduler.schedule_in(0.01)

    assert scheduler._task is first_task
    release_first.set()
    await asyncio.sleep(0.04)
    assert calls == 2


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


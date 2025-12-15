import asyncio

import pytest

from src.pipeline import ScriberPipeline


class _DummyTask:
    def __init__(self, done_event: asyncio.Event, *, set_done_on_stop: bool):
        self._done_event = done_event
        self._set_done_on_stop = set_done_on_stop
        self._finished = False
        self.stop_when_done_called = False
        self.cancel_called = False

    def has_finished(self) -> bool:
        return self._finished

    async def stop_when_done(self):
        self.stop_when_done_called = True
        if self._set_done_on_stop:
            await asyncio.sleep(0)
            self._finished = True
            self._done_event.set()

    async def cancel(self, *, reason: str | None = None):
        self.cancel_called = True
        self._finished = True
        self._done_event.set()


@pytest.mark.asyncio
async def test_stop_waits_for_start_done_gracefully():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=True)

    await pipeline.stop(timeout_secs=0.5)

    assert pipeline.task.stop_when_done_called is True
    assert pipeline.task.cancel_called is False


@pytest.mark.asyncio
async def test_stop_times_out_and_cancels():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    pipeline.is_active = True
    pipeline._start_done.clear()
    pipeline.task = _DummyTask(pipeline._start_done, set_done_on_stop=False)

    await pipeline.stop(timeout_secs=0.01)

    assert pipeline.task.stop_when_done_called is True
    assert pipeline.task.cancel_called is True


import asyncio

import pytest

from src.config import Config
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


class _DummyAudioInput:
    def __init__(self, events: list[str] | None = None) -> None:
        self.close_stream = None
        self._events = events

    async def stop(self, frame, *, close_stream=None):
        self.close_stream = close_stream
        if self._events is not None:
            self._events.append("audio_stop")


class _DummyPrewarmManager:
    def __init__(self, events: list[str] | None = None) -> None:
        self.resume_calls = 0
        self._events = events

    def resume_after_active_capture(self) -> bool:
        self.resume_calls += 1
        if self._events is not None:
            self._events.append("prewarm_resume")
        return True


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

    with pytest.raises(RuntimeError, match="Transcription did not finish within"):
        await pipeline.stop(timeout_secs=0.01)

    assert pipeline.task.stop_when_done_called is True
    assert pipeline.task.cancel_called is True


@pytest.mark.asyncio
async def test_cleanup_audio_input_forces_stream_close():
    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    audio_input = _DummyAudioInput()
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None


@pytest.mark.asyncio
async def test_cleanup_audio_input_resumes_always_on_prewarm_immediately(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    events: list[str] = []
    prewarm = _DummyPrewarmManager(events)
    pipeline = ScriberPipeline(
        service_name="soniox",
        on_status_change=None,
        mic_prewarm_manager=prewarm,
    )
    audio_input = _DummyAudioInput(events)
    pipeline.audio_input = audio_input

    await pipeline._cleanup_audio_input()

    assert audio_input.close_stream is True
    assert pipeline.audio_input is None
    assert prewarm.resume_calls == 1
    assert events == ["audio_stop", "prewarm_resume"]


def test_pipeline_delegates_audio_diagnostics_and_health():
    class _HealthAudioInput:
        def __init__(self):
            self.health_calls = []

        def diagnostic_snapshot(self):
            return {"running": True, "streamActive": False}

        def ensure_stream_health(self, **kwargs):
            self.health_calls.append(kwargs)
            return True

    pipeline = ScriberPipeline(service_name="soniox", on_status_change=None)
    audio_input = _HealthAudioInput()
    pipeline.audio_input = audio_input

    assert pipeline.audio_diagnostics() == {"running": True, "streamActive": False}
    assert pipeline.ensure_audio_health(
        reason="test",
        max_callback_gap_seconds=3.0,
    ) is True
    assert audio_input.health_calls == [
        {"reason": "test", "max_callback_gap_seconds": 3.0}
    ]

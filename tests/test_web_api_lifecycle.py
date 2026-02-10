import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.web_api import ScriberWebController, TranscriptRecord


def _make_record(session_id: str) -> TranscriptRecord:
    rec = TranscriptRecord(
        id=session_id,
        title="Live Mic",
        date="Today",
        duration="00:00",
        status="recording",
        type="mic",
        language="en",
    )
    rec.start()
    return rec


@pytest.mark.asyncio
async def test_on_pipeline_done_ignores_stale_task():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    stale_task = asyncio.create_task(asyncio.sleep(0))
    current_task = asyncio.create_task(asyncio.sleep(1))

    ctl._pipeline_task = current_task
    ctl._is_listening = True
    ctl._is_stopping = True
    ctl._session_id = "active-session"

    await stale_task
    ctl._on_pipeline_done(stale_task, session_id="old-session")
    await asyncio.sleep(0.01)

    assert ctl._pipeline_task is current_task
    assert ctl._is_listening is True
    assert ctl._is_stopping is True
    assert ctl._session_id == "active-session"

    current_task.cancel()
    await asyncio.gather(current_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_state_reports_background_processing_flag():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    pending = loop.create_future()
    ctl._running_tasks["bg-task"] = pending

    state = ctl.get_state()
    assert state["backgroundProcessing"] is True

    pending.set_result(None)
    state = ctl.get_state()
    assert state["backgroundProcessing"] is False


@pytest.mark.asyncio
async def test_on_pipeline_done_persists_failed_live_session():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "failed-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True

    async def _boom():
        raise RuntimeError("boom")

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    ctl._pipeline_task = task

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db", new=MagicMock()) as save_mock,
        patch("src.web_api.hide_recording_overlay"),
    ):
        ctl._on_pipeline_done(task, session_id=session_id)
        await asyncio.sleep(0.05)

    assert rec.status == "failed"
    assert ctl._current is None
    assert ctl._session_id is None
    assert rec in ctl._history
    save_mock.assert_called_once_with(rec)


class _StopOkPipeline:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_emergency_stop_clears_state_and_stopping_flag():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "emergency-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True
    ctl._is_stopping = True

    pipeline = _StopOkPipeline()
    ctl._pipeline = pipeline
    ctl._pipeline_task = asyncio.create_task(asyncio.sleep(10))

    await ctl._emergency_stop_pipeline(session_id=session_id)

    assert pipeline.stopped is True
    assert ctl._is_listening is False
    assert ctl._is_stopping is False
    assert ctl._pipeline is None
    assert ctl._pipeline_task is None
    assert ctl._session_id is None
    assert ctl._current is None


class _StopFailPipeline:
    service_name = "openai"

    async def stop(self):
        raise RuntimeError("stop failed")


@pytest.mark.asyncio
async def test_stop_listening_marks_failed_when_stop_raises():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "stop-fail-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True
    ctl._pipeline = _StopFailPipeline()
    ctl._pipeline_task = None

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
    ):
        await ctl.stop_listening()

    assert rec.status == "failed"
    assert "[Error] stop failed" in rec.content
    assert ctl._status == "Error"
    assert rec in ctl._history

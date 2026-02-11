import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.data.job_store import JobStatus, JobStore
from src.web_api import ScriberWebController


@pytest.mark.asyncio
async def test_start_youtube_transcription_persists_job_lifecycle(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)

    async def _fake_run(rec, *, provider):
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_youtube_transcription({"url": "https://youtube.com/watch?v=test123"})
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    job_id = ctl._job_ids_by_transcript[rec.id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.job_type.value == "youtube"


@pytest.mark.asyncio
async def test_cancel_transcript_marks_background_job_canceled(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.sleep(0)
        assert await ctl.cancel_transcript(rec.id) is True
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    job_id = ctl._job_ids_by_transcript[rec.id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CANCELED
    assert job.job_type.value == "file"

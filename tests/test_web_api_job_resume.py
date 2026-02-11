import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.job_store import JobStatus, JobStore, JobType
from src.web_api import ScriberWebController


@pytest.mark.asyncio
async def test_resume_pending_youtube_job_restarts_and_completes(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    store.enqueue(
        transcript_id="tx-resume-youtube",
        job_type=JobType.YOUTUBE,
        payload={
            "url": "https://youtube.com/watch?v=resume123",
            "title": "Resume Video",
            "channel": "Channel",
            "duration": "10:00",
            "language": "en",
        },
    )

    ctl = ScriberWebController(loop, job_store=store)

    async def _fake_run(rec):
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        resumed = await ctl.resume_pending_jobs(limit=10)
        assert resumed == 1
        task = ctl._running_tasks["tx-resume-youtube"]
        await asyncio.gather(task, return_exceptions=True)

    rec = ctl._get_history_record("tx-resume-youtube")
    assert rec is not None
    assert rec.status == "completed"
    job = store.get_by_transcript_id("tx-resume-youtube")
    assert job is not None
    assert job.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_file_job_without_source_marks_failed(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    store.enqueue(
        transcript_id="tx-resume-file-missing",
        job_type=JobType.FILE,
        payload={
            "path": str(tmp_path / "deleted.wav"),
            "title": "Missing file",
            "language": "de",
        },
    )

    ctl = ScriberWebController(loop, job_store=store)

    with (
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db", new=MagicMock()),
    ):
        resumed = await ctl.resume_pending_jobs(limit=10)
        assert resumed == 0

    rec = ctl._get_history_record("tx-resume-file-missing")
    assert rec is not None
    assert rec.status == "failed"
    assert "no longer available" in rec.content.lower()
    job = store.get_by_transcript_id("tx-resume-file-missing")
    assert job is not None
    assert job.status == JobStatus.FAILED


import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.data.job_store import JobStatus, JobStore, JobType
from src.web_api import ScriberWebController


@pytest.mark.asyncio
async def test_concurrent_retry_scans_are_serialized(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_list_pending(*, limit):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return []

    monkeypatch.setattr(store, "list_pending", slow_list_pending)
    await asyncio.gather(
        ctl.resume_pending_jobs(limit=10),
        ctl.resume_pending_jobs(limit=10),
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_runtime_retry_scan_does_not_reset_active_running_jobs(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-active",
        job_type=JobType.FILE,
        payload={"path": str(tmp_path / "active.wav")},
    )
    assert store.mark_running(job.id)
    ctl = ScriberWebController(loop, job_store=store)

    resumed = await ctl.resume_pending_jobs(limit=10, recover_running=False)

    persisted = store.get(job.id)
    assert resumed == 0
    assert persisted is not None
    assert persisted.status == JobStatus.RUNNING


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

    async def _fake_run(rec, *, provider):
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
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
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

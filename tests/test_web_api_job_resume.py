import asyncio
import threading
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.data.job_store import JobStatus, JobStore, JobType
from src.web_api import ScriberWebController, TranscriptRecord


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
async def test_due_job_backlog_refills_without_exceeding_concurrency(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_JOB_CONCURRENCY", "2")
    store = JobStore(db_path=tmp_path / "jobs.db")
    release_events: dict[str, asyncio.Event] = {}
    run_suffix = f"{tmp_path.parent.name}-{time.time_ns()}"
    for index in range(3):
        file_path = tmp_path / f"queued-{index}.wav"
        file_path.write_bytes(b"RIFF....WAVEfmt ")
        transcript_id = f"tx-backlog-{run_suffix}-{index}"
        release_events[transcript_id] = asyncio.Event()
        store.enqueue(
            transcript_id=transcript_id,
            job_type=JobType.FILE,
            payload={"path": str(file_path), "title": f"Queued {index}"},
        )

    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    started: list[str] = []

    async def _fake_run(rec, _file_path, *, provider):
        started.append(rec.id)
        await release_events[rec.id].wait()
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        try:
            assert await ctl.resume_pending_jobs(limit=25) == 2
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(started) == 2
            assert len([task for task in ctl._running_tasks.values() if not task.done()]) == 2

            release_events[started[0]].set()
            for _ in range(100):
                if len(started) == 3:
                    break
                await asyncio.sleep(0.01)

            assert len(started) == 3
            assert len([task for task in ctl._running_tasks.values() if not task.done()]) <= 2
        finally:
            ctl.begin_shutdown()
            for event in release_events.values():
                event.set()
            await asyncio.gather(*tuple(ctl._running_tasks.values()), return_exceptions=True)


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
    resume_started_at = datetime.now()
    release_run = asyncio.Event()

    async def _fake_run(rec, *, provider):
        await release_run.wait()
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        resumed = await ctl.resume_pending_jobs(limit=10)
        assert resumed == 1
        task = ctl._running_tasks["tx-resume-youtube"]
        release_run.set()
        await asyncio.gather(task, return_exceptions=True)

    rec = ctl._get_history_record("tx-resume-youtube")
    assert rec is not None
    assert rec.status == "completed"
    assert datetime.fromisoformat(rec.processing_started_at) >= resume_started_at
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
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
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
    broadcast_mock.assert_awaited_once_with(record=rec, reason="job_failed")


@pytest.mark.asyncio
async def test_resume_missing_owned_file_cleans_stale_upload_directory(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "stale-upload"
    upload_dir.mkdir(parents=True)
    missing_path = upload_dir / "missing.wav"
    (upload_dir / "leftover.tmp").write_bytes(b"stale")
    store.enqueue(
        transcript_id="tx-resume-owned-file-missing",
        job_type=JobType.FILE,
        payload={"path": str(missing_path), "title": "Missing owned file"},
    )

    with (
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
    ):
        resumed = await ctl.resume_pending_jobs(limit=10)

    assert resumed == 0
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_resume_reconciles_terminal_file_job_and_cleans_owned_upload(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "completed-upload"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    job = store.enqueue(
        transcript_id="tx-resume-terminal-file",
        job_type=JobType.FILE,
        payload={"path": str(file_path), "title": "Completed file"},
    )
    rec = TranscriptRecord(
        id=job.transcript_id,
        title="Completed file",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        source_url=str(file_path),
        content="Done",
    )
    ctl._add_to_history(rec)

    resumed = await ctl.resume_pending_jobs(limit=10)

    assert resumed == 0
    assert not upload_dir.exists()
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.COMPLETED

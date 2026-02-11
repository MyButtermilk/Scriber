from datetime import datetime, timedelta

from src.data.job_store import JobStatus, JobStore, JobType


def test_job_store_persists_and_transitions(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    queued = store.enqueue(
        transcript_id="tx-1",
        job_type=JobType.YOUTUBE,
        payload={"url": "https://example.com/watch?v=abc"},
    )
    assert queued.status == JobStatus.QUEUED

    assert store.mark_running(queued.id) is True
    running = store.get(queued.id)
    assert running is not None
    assert running.status == JobStatus.RUNNING
    assert running.attempts == 1

    assert store.mark_completed(queued.id) is True
    completed = store.get(queued.id)
    assert completed is not None
    assert completed.status == JobStatus.COMPLETED


def test_job_store_pending_and_retry_windows(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    due = store.enqueue(
        transcript_id="tx-due",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/file.wav"},
    )
    future = store.enqueue(
        transcript_id="tx-future",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/future.wav"},
    )

    retry_at = (datetime.now() + timedelta(minutes=5)).isoformat()
    assert store.set_retry(future.id, retry_at=retry_at, last_error="temporary failure")

    pending_ids = {job.id for job in store.list_pending()}
    assert due.id in pending_ids
    assert future.id not in pending_ids

    assert store.mark_running(due.id)
    assert store.mark_running(future.id)
    reset_count = store.reset_running_to_queued()
    assert reset_count == 2


def test_job_store_reports_seconds_until_next_retry(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-delay",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/retry.wav"},
    )
    retry_at = (datetime.now() + timedelta(seconds=2)).isoformat()
    assert store.set_retry(job.id, retry_at=retry_at, last_error="temporary")
    delay = store.seconds_until_next_retry()
    assert delay is not None
    assert 0.0 <= delay <= 3.0

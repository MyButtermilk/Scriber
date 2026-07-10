from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from src.data.job_store import JobStatus, JobStore, JobType


def test_job_store_default_uses_runtime_database_path(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SCRIBER_DATABASE_PATH", raising=False)

    store = JobStore()

    assert store._db_path == data_dir / "transcripts.db"
    assert (data_dir / "transcripts.db").is_file()


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


def test_job_store_reuses_thread_local_connection(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    first = store._connect()
    second = store._connect()

    assert first is second


def test_job_store_indexes_retry_scheduler_lookup(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    indexes = {row[1] for row in store._connect().execute("PRAGMA index_list(jobs)").fetchall()}

    assert "idx_jobs_status_next_retry_at" in indexes


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
    assert due.id not in {job.id for job in store.list_pending()}
    assert future.id not in {job.id for job in store.list_pending()}
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


def test_job_store_normalizes_offset_aware_retry_timestamps(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-aware-delay",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/retry.wav"},
    )
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    assert store.set_retry(job.id, retry_at=retry_at, last_error="temporary")

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.next_retry_at
    assert datetime.fromisoformat(persisted.next_retry_at).tzinfo is None
    assert job.id not in {pending.id for pending in store.list_pending()}
    delay = store.seconds_until_next_retry()
    assert delay is not None
    assert 295.0 <= delay <= 305.0


def test_job_store_reopens_worker_connection_after_close(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    def _connection_identity() -> int:
        conn = store._connect()
        conn.execute("SELECT 1").fetchone()
        return id(conn)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(_connection_identity).result(timeout=2.0)
        store.close()
        second = executor.submit(_connection_identity).result(timeout=2.0)

    assert second != first
    store.close()


def test_job_store_deletes_all_rows_for_transcript(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    first = store.enqueue(
        transcript_id="tx-delete",
        job_type=JobType.FILE,
        payload={"path": "C:/private/source.wav"},
    )
    second = store.enqueue(
        transcript_id="tx-delete",
        job_type=JobType.YOUTUBE,
        payload={"url": "https://youtube.com/watch?v=abcdefghijk"},
    )

    assert store.delete_by_transcript_id("tx-delete") == 2
    assert store.get(first.id) is None
    assert store.get(second.id) is None
    assert store.delete_by_transcript_id("tx-delete") == 0

import gc
import sqlite3
import weakref
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.data.job_store import (
    PROVIDER_REQUEST_MAY_BE_COMMITTED,
    PROVIDER_REQUEST_NOT_STARTED,
    PROVIDER_REQUEST_RESULT_DURABLE,
    JobStatus,
    JobStore,
    JobType,
)


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


def test_job_store_claim_is_atomic_across_workers(tmp_path):
    db_path = tmp_path / "jobs.db"
    first = JobStore(db_path=db_path)
    second = JobStore(db_path=db_path)
    job = first.enqueue(
        transcript_id="tx-claim", job_type=JobType.FILE, payload={"path": "meeting.wav"}
    )
    barrier = Barrier(2)

    def claim(store):
        barrier.wait(timeout=3)
        return store.mark_running(job.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first, second)))

    persisted = first.get(job.id)
    assert sorted(results) == [False, True]
    assert persisted is not None
    assert persisted.status == JobStatus.RUNNING
    assert persisted.attempts == 1
    first.close()
    second.close()


def test_job_store_freezes_and_finalizes_execution_route_once(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-route",
        job_type=JobType.FILE,
        payload={"path": "meeting.wav"},
    )
    initial = {
        "provider": "azure_mai",
        "providerRoute": "audio_transcriptions",
        "model": "mai-transcribe-1.5",
        "transport": "direct_upload",
        "language": "de",
        "audioInputFormat": None,
        "providerAudioCapabilityId": (
            "azure_mai:audio_transcriptions:mai-transcribe-1.5"
        ),
        "providerAudioCapabilityRevision": "provider-audio-formats-v1",
        "audioInputFormatVerified": None,
    }
    assert store.freeze_execution_route(job.id, initial)
    assert store.freeze_execution_route(job.id, initial)

    finalized = {
        **initial,
        "audioInputFormat": "mp3",
        "audioSelectionMode": "original_passthrough",
        "audioPreparationImplementation": "original_passthrough",
        "audioInputFormatVerified": True,
    }
    assert store.freeze_execution_route(job.id, finalized)
    assert store.get(job.id).payload["executionRoute"] == finalized

    changed = {**finalized, "audioInputFormat": "wav_pcm16"}
    assert store.freeze_execution_route(job.id, changed) is False
    assert store.get(job.id).payload["executionRoute"] == finalized


def test_job_store_execution_route_rejects_provider_switch_and_secrets(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-route-safe",
        job_type=JobType.YOUTUBE,
        payload={"url": "https://example.com"},
    )
    route = {
        "provider": "soniox",
        "providerRoute": "async_transcription",
        "model": "stt-async-v5",
    }
    assert store.freeze_execution_route(job.id, route)
    assert store.freeze_execution_route(job.id, {**route, "provider": "openai_async"}) is False
    with pytest.raises(ValueError, match="unsupported fields"):
        store.freeze_execution_route(job.id, {**route, "apiKey": "secret"})


def test_job_store_preserves_planned_fallback_and_records_actual_caption_route(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    fallback = {
        "provider": "soniox",
        "providerRoute": "async_transcription",
        "model": "stt-async-v5",
        "transport": "direct_upload",
        "language": "de",
    }
    captions = {
        "provider": "youtube_captions_auto",
        "providerRoute": "",
        "model": "youtube-json3-vtt",
        "transport": "caption_track",
        "language": "de",
    }
    job = store.enqueue(
        transcript_id="tx-caption-route",
        job_type=JobType.YOUTUBE,
        payload={"plannedFallbackRoute": fallback},
    )
    assert store.mark_running(job.id)

    assert store.freeze_execution_route(job.id, captions)
    assert store.record_executed_route(job.id, captions)
    assert store.record_executed_route(job.id, captions)

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.payload["plannedFallbackRoute"] == fallback
    assert persisted.payload["executionRoute"] == captions
    assert persisted.payload["executedRoute"] == captions
    assert store.freeze_execution_route(job.id, fallback) is False
    assert store.record_executed_route(job.id, fallback) is False


def test_job_store_cannot_replace_selected_provider_fallback_with_captions(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    fallback = {
        "provider": "soniox",
        "providerRoute": "async_transcription",
        "model": "stt-async-v5",
        "transport": "direct_upload",
        "language": "de",
    }
    captions = {
        "provider": "youtube_captions",
        "providerRoute": "",
        "model": "youtube-json3-vtt",
        "transport": "caption_track",
        "language": "de",
    }
    job = store.enqueue(
        transcript_id="tx-provider-selected",
        job_type=JobType.YOUTUBE,
        payload={"plannedFallbackRoute": fallback},
    )
    assert store.mark_running(job.id)

    # This selection is persisted before the provider upload boundary.
    assert store.freeze_execution_route(job.id, fallback)
    assert store.freeze_execution_route(job.id, captions) is False
    assert store.record_executed_route(job.id, captions) is False
    assert store.get(job.id).payload["executionRoute"] == fallback


def test_job_store_route_payloads_are_allowlisted_at_enqueue(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    with pytest.raises(ValueError, match="unsupported fields"):
        store.enqueue(
            transcript_id="tx-secret-route",
            job_type=JobType.YOUTUBE,
            payload={
                "plannedFallbackRoute": {
                    "provider": "soniox",
                    "model": "stt-async-v5",
                    "endpoint": "https://token@example.test",
                }
            },
        )


def test_job_store_terminal_transitions_are_idempotent_but_not_overwritable(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-cancel", job_type=JobType.YOUTUBE, payload={"url": "https://example.com"}
    )
    assert store.mark_running(job.id)
    assert store.mark_canceled(job.id, last_error="stopped")
    assert store.mark_canceled(job.id, last_error="stopped")
    assert store.mark_completed(job.id) is False
    assert store.mark_failed(job.id, last_error="late failure") is False
    assert store.set_retry(job.id, retry_at=datetime.now().isoformat()) is False
    assert store.mark_running(job.id) is False
    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.status == JobStatus.CANCELED
    assert persisted.attempts == 1

    direct = store.enqueue(
        transcript_id="tx-direct", job_type=JobType.FILE, payload={"path": "direct.wav"}
    )
    assert store.mark_completed(direct.id) is True
    assert store.mark_completed(direct.id) is True
    assert store.get(direct.id).status == JobStatus.COMPLETED


def test_job_store_reuses_thread_local_connection(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")

    first = store._connect()
    second = store._connect()

    assert first is second


def test_job_store_finalizer_closes_cached_connections(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    conn = store._connect()
    store_ref = weakref.ref(store)

    del store
    gc.collect()

    assert store_ref() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


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


def test_job_store_provider_request_fence_blocks_retry_and_restart_replay(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-provider-fence",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/provider.wav"},
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)

    fenced = store.get(job.id)
    assert fenced is not None
    assert fenced.provider_request_state == PROVIDER_REQUEST_MAY_BE_COMMITTED
    assert fenced.provider_request_attempt == fenced.attempts == 1
    assert store.set_retry(
        job.id,
        retry_at=datetime.now().isoformat(),
        last_error="connection lost",
    ) is False
    assert store.reset_running_to_queued() == 0
    assert [item.id for item in store.list_running_provider_outcomes()] == [job.id]
    assert store.get(job.id).status == JobStatus.RUNNING


def test_job_store_enqueue_cannot_replace_existing_provider_fence(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        job_id="fixed-job-id",
        transcript_id="tx-existing-fence",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/original.wav"},
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)

    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue(
            job_id=job.id,
            transcript_id="tx-replacement",
            job_type=JobType.FILE,
            payload={"path": "C:/tmp/replacement.wav"},
        )

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.transcript_id == "tx-existing-fence"
    assert persisted.status == JobStatus.RUNNING
    assert persisted.attempts == 1
    assert persisted.provider_request_state == PROVIDER_REQUEST_MAY_BE_COMMITTED


def test_job_store_migrates_legacy_running_job_to_unknown_outcome(tmp_path):
    db_path = tmp_path / "legacy-jobs.db"
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-running",
                "tx-legacy-running",
                JobType.FILE.value,
                JobStatus.RUNNING.value,
                '{"path":"C:/tmp/legacy.wav"}',
                1,
                "",
                "",
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-queued-retry",
                "tx-legacy-queued-retry",
                JobType.FILE.value,
                JobStatus.QUEUED.value,
                '{"path":"C:/tmp/legacy-retry.wav"}',
                2,
                datetime.now().isoformat(),
                "temporary provider timeout",
                now,
                now,
            ),
        )

    store = JobStore(db_path=db_path)
    migrated = store.get("legacy-running")
    assert migrated is not None
    assert migrated.provider_request_state == PROVIDER_REQUEST_MAY_BE_COMMITTED
    assert migrated.provider_request_attempt == migrated.attempts == 1
    assert store.reset_running_to_queued() == 0
    assert store.get("legacy-running").status == JobStatus.RUNNING
    legacy_retry = store.get("legacy-queued-retry")
    assert legacy_retry is not None
    assert legacy_retry.status == JobStatus.FAILED
    assert legacy_retry.provider_request_state == PROVIDER_REQUEST_MAY_BE_COMMITTED
    assert legacy_retry.provider_request_attempt == legacy_retry.attempts == 2
    assert "automatic replay was disabled" in legacy_retry.last_error


def test_job_store_proven_pre_body_failure_can_retry(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-provider-prebody",
        job_type=JobType.FILE,
        payload={"path": "C:/tmp/provider.wav"},
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)
    assert store.mark_provider_request_safe_to_retry(job.id)
    assert store.get(job.id).provider_request_state == PROVIDER_REQUEST_NOT_STARTED
    assert store.set_retry(
        job.id,
        retry_at=datetime.now().isoformat(),
        last_error="connect failed before body",
    )


def test_job_store_queues_only_verified_durable_result_recovery(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="tx-provider-result",
        job_type=JobType.YOUTUBE,
        payload={"url": "https://example.test/video"},
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)
    assert store.mark_provider_result_durable(job.id, attempt_id="attempt-paid-1")
    durable = store.get(job.id)
    assert durable.provider_request_state == PROVIDER_REQUEST_RESULT_DURABLE
    assert durable.provider_result_attempt_id == "attempt-paid-1"
    assert store.reset_running_to_queued() == 0
    assert store.queue_provider_result_recovery(job.id)

    queued = store.get(job.id)
    assert queued is not None
    assert queued.status == JobStatus.QUEUED
    assert queued.provider_request_state == PROVIDER_REQUEST_RESULT_DURABLE
    assert store.mark_running(job.id)
    resumed = store.get(job.id)
    assert resumed is not None
    assert resumed.provider_request_state == PROVIDER_REQUEST_RESULT_DURABLE
    assert resumed.provider_request_attempt == resumed.attempts == 2
    assert store.mark_provider_request_may_be_committed(job.id) is False


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

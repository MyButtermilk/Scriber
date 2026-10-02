from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import (
    TranscriptionCheckpointMismatch,
    TranscriptionPartOutcomeUnknown,
    source_sha256,
)

MANIFEST = {"parts": [{"index": 1, "start_ms": 0, "end_ms": 900}, {"index": 2, "start_ms": 800, "end_ms": 1700}]}
SHAPE = {"model": "mai-2", "language": "de", "word_timestamps": True}
ROUTE = {"provider": "openrouter_stt", "model": "mai-2", "region": "eu"}


@pytest.fixture
def checkpoint_job(tmp_path):
    path = tmp_path / "source.mp3"
    path.write_bytes(b"the exact original audio")
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.enqueue(transcript_id="transcript", job_type=JobType.FILE, payload={"path": str(path)})
    assert store.mark_running(job.id)

    def checkpoint(**overrides):
        args = {"source_digest": source_sha256(path), "source_path": path, "execution_route": ROUTE}
        args.update(overrides)
        return store.transcription_checkpoint(job.id, **args)

    yield store, job, checkpoint, path
    store.close()


@pytest.mark.asyncio
async def test_paid_parts_survive_restart_and_explicit_rejection_resume(checkpoint_job):
    store, job, make, _ = checkpoint_job
    first = make()
    await first.bind(manifest=MANIFEST, request_shape=SHAPE)
    await first.mark_started(1)
    await first.save_success(1, {"text": "already paid", "words": []})
    await first.mark_started(2)
    await first.mark_rejected(2, 413)
    assert store.mark_failed(job.id, last_error="known rejection")
    store.close()
    assert store.checkpoint_resume_available(job.id)
    assert store.queue_checkpoint_resume(job.id, source_digest=first.source_digest, expected_attempt=1)
    assert store.mark_running(job.id)
    resumed = make()
    assert await resumed.load_manifest(request_shape=SHAPE) == MANIFEST
    assert await resumed.lookup(1) == {"text": "already paid", "words": []}
    assert await resumed.lookup(2) is None
    await resumed.mark_started(2)
    await resumed.save_success(2, {"text": "new missing part"})
    with pytest.raises(RuntimeError, match="ownership"):
        await first.save_success(1, {"text": "stale worker"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 500, 502, 503])
async def test_unknown_or_gateway_request_never_resumes(checkpoint_job, status):
    store, job, make, _ = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest=MANIFEST, request_shape=SHAPE)
    await checkpoint.mark_started(1)
    if status:
        await checkpoint.mark_rejected(1, status)
    with pytest.raises(TranscriptionPartOutcomeUnknown):
        await checkpoint.lookup(1)
    assert store.mark_failed(job.id, last_error="unknown outcome")
    assert not store.checkpoint_resume_available(job.id)
    assert not store.queue_checkpoint_resume(job.id, source_digest=checkpoint.source_digest, expected_attempt=1)


@pytest.mark.asyncio
async def test_source_route_request_and_boundary_mismatch_fail_closed(checkpoint_job):
    _, _, make, path = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest=MANIFEST, request_shape=SHAPE)
    with pytest.raises(TranscriptionCheckpointMismatch):
        await checkpoint.load_manifest(request_shape={**SHAPE, "language": "en"})
    with pytest.raises(TranscriptionCheckpointMismatch):
        await make(execution_route={**ROUTE, "region": "us"}).load_manifest(request_shape=SHAPE)
    path.write_bytes(b"changed original")
    with pytest.raises(TranscriptionCheckpointMismatch):
        await make().load_manifest(request_shape=SHAPE)
    changed = {"parts": [MANIFEST["parts"][0], {"index": 2, "start_ms": 799, "end_ms": 1700}]}
    with pytest.raises(TranscriptionCheckpointMismatch):
        await checkpoint.bind(manifest=changed, request_shape=SHAPE)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_paid_result_commit(checkpoint_job, monkeypatch):
    _, _, make, _ = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest=MANIFEST, request_shape=SHAPE)
    await checkpoint.mark_started(1)
    entered = threading.Event()
    release = threading.Event()
    original = checkpoint._save_success

    def blocked_save(index, payload):
        entered.set()
        assert release.wait(timeout=5)
        original(index, payload)

    monkeypatch.setattr(checkpoint, "_save_success", blocked_save)
    task = asyncio.create_task(checkpoint.save_success(1, {"text": "durable despite cancellation"}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await make().lookup(1) == {"text": "durable despite cancellation"}


@pytest.mark.asyncio
async def test_remote_resource_ids_survive_restart_and_result_fences_cleanup(checkpoint_job):
    store, job, make, _ = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest={"parts": [{"index": 0}, {"index": 1}]}, request_shape=SHAPE)
    await checkpoint.mark_started(0)
    await checkpoint.save_remote_id(0, "provider-file-id")
    await checkpoint.mark_started(1)
    await checkpoint.save_remote_id(1, "provider-transcription-id")
    assert store.mark_canceled(job.id)
    store.close()
    assert store.queue_checkpoint_resume(job.id, source_digest=checkpoint.source_digest, expected_attempt=1)
    assert store.mark_running(job.id)
    resumed = make()
    assert await resumed.remote_id(0) == "provider-file-id"
    assert await resumed.remote_id(1) == "provider-transcription-id"
    assert await resumed.lookup(1, allow_remote=True) is None
    await resumed.save_success(1, {"text": "retrieved with GET"})
    assert await resumed.lookup(1) == {"text": "retrieved with GET"}


@pytest.mark.asyncio
async def test_job_deletion_removes_sensitive_results(checkpoint_job):
    store, job, make, _ = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest=MANIFEST, request_shape=SHAPE)
    await checkpoint.mark_started(1)
    await checkpoint.save_success(1, {"text": "sensitive text"})
    assert store.delete_exact(job.id, expected_transcript_id="transcript")
    with sqlite3.connect(store._db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcription_checkpoints").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transcription_parts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_canceled_start_commit_reopens_only_provably_unsent_part(checkpoint_job, monkeypatch):
    store, job, make, _ = checkpoint_job
    checkpoint = make()
    await checkpoint.bind(manifest=MANIFEST, request_shape=SHAPE)
    entered = threading.Event()
    release = threading.Event()
    original = checkpoint._mark_started

    def blocked_start(index):
        original(index)
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(checkpoint, "_mark_started", blocked_start)
    task = asyncio.create_task(checkpoint.mark_started(1))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await checkpoint.lookup(1) is None
    assert store.mark_canceled(job.id)
    assert store.checkpoint_resume_available(job.id)


@pytest.mark.asyncio
async def test_delete_hands_remote_ids_to_durable_private_cleanup_outbox(checkpoint_job):
    store, job, make, _ = checkpoint_job
    assert store.freeze_execution_route(
        job.id, {"provider": "soniox_async", "model": "stt-async-v5", "providerRegion": "eu"}
    )
    checkpoint = make()
    await checkpoint.bind(
        manifest={"kind": "soniox_remote_job", "parts": [{"index": 0}, {"index": 1}]}, request_shape=SHAPE
    )
    await checkpoint.mark_started(0)
    await checkpoint.save_remote_id(0, "file-to-delete")
    await checkpoint.mark_started(1)
    await checkpoint.save_remote_id(1, "transcription-to-delete")
    assert store.delete_by_transcript_id("transcript") == 1
    assert not store.has_transcription_checkpoint(job.id)
    store.close()
    rows = store.list_soniox_cleanup()
    assert rows == [
        {"job_id": job.id, "region": "eu", "resource_kind": "transcription", "resource_id": "transcription-to-delete"},
        {"job_id": job.id, "region": "eu", "resource_kind": "file", "resource_id": "file-to-delete"},
    ]
    assert not store.complete_soniox_cleanup(job_id=job.id, resource_kind="file", resource_id="foreign-id")
    assert store.list_soniox_cleanup() == rows
    assert store.complete_soniox_cleanup(
        job_id=job.id, resource_kind="transcription", resource_id="transcription-to-delete"
    )
    assert store.list_soniox_cleanup() == [rows[1]]


def test_cleanup_failure_rotation_preserves_dependencies_and_does_not_starve_jobs(checkpoint_job):
    store, _, _, _ = checkpoint_job
    with sqlite3.connect(store._db_path) as conn:
        conn.executemany(
            "INSERT INTO soniox_resource_cleanup(job_id,region,resource_kind,resource_id) VALUES(?,?,?,?)",
            [(job, "eu", kind, f"{job}-{kind}") for job in ("a", "b") for kind in ("file", "transcription")],
        )
    assert [row["resource_kind"] for row in store.list_soniox_cleanup(limit=2)] == ["transcription", "file"]
    store.note_soniox_cleanup_attempt(job_id="a")
    rows = store.list_soniox_cleanup(limit=2)
    assert [row["job_id"] for row in rows] == ["b", "b"]
    assert [row["resource_kind"] for row in rows] == ["transcription", "file"]

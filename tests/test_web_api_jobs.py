import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src import web_api
from src.config import Config
from src.data.job_store import JobStatus, JobStore
from src.web_api import ScriberWebController, TranscriptRecord
from src.youtube_download import YouTubeTranscript


@pytest.mark.asyncio
async def test_background_job_enqueue_runs_off_event_loop(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    event_loop_thread = threading.get_ident()
    enqueue_thread = None

    def enqueue(**_kwargs):
        nonlocal enqueue_thread
        enqueue_thread = threading.get_ident()
        return SimpleNamespace(id="job-off-loop")

    monkeypatch.setattr(ctl._job_store, "enqueue", enqueue)
    rec = TranscriptRecord(
        id="off-loop",
        title="Off loop",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )

    await ctl._enqueue_background_job_async(
        rec,
        job_type=web_api.JobType.FILE,
        payload={"path": "sample.wav"},
    )

    assert enqueue_thread is not None
    assert enqueue_thread != event_loop_thread
    assert ctl._job_ids_by_transcript[rec.id] == "job-off-loop"


def test_background_task_registry_consumes_and_logs_unexpected_failure():
    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        failure = RuntimeError("unexpected runner failure")
        task = SimpleNamespace(cancelled=lambda: False, exception=lambda: failure)
        ctl._running_tasks["failed-task"] = task

        with patch("src.web_api.logger") as logger_mock:
            ctl._unregister_task("failed-task", task)

        assert "failed-task" not in ctl._running_tasks
        logger_mock.opt.assert_called_once_with(exception=failure)
        logger_mock.opt.return_value.error.assert_called_once_with(
            "Background transcription task crashed: {}",
            "failed-task",
        )
    finally:
        loop.close()


def test_job_id_runtime_cache_is_bounded(monkeypatch):
    monkeypatch.setenv("SCRIBER_JOB_ID_CACHE_LIMIT", "25")
    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        for index in range(40):
            ctl._remember_job_id(f"transcript-{index}", f"job-{index}")

        assert len(ctl._job_ids_by_transcript) == 25
        assert ctl._job_ids_by_transcript["transcript-39"] == "job-39"
        assert "transcript-0" not in ctl._job_ids_by_transcript
    finally:
        loop.close()


def test_invalid_optional_runtime_numbers_fall_back_to_safe_defaults(monkeypatch):
    monkeypatch.setenv("SCRIBER_JOB_MAX_ATTEMPTS", "not-an-integer")
    monkeypatch.setenv("SCRIBER_JOB_RETRY_BASE_SEC", "not-a-number")
    monkeypatch.setenv("SCRIBER_JOB_RETRY_MAX_SEC", "not-a-number")
    monkeypatch.setenv("SCRIBER_BREAKER_FAILURE_THRESHOLD", "broken")
    monkeypatch.setenv("SCRIBER_BREAKER_COOLDOWN_SEC", "broken")
    monkeypatch.setenv("SCRIBER_HISTORY_CACHE_LIMIT", "broken")
    loop = asyncio.new_event_loop()
    try:
        ctl = ScriberWebController(loop)
        assert ctl._job_max_attempts == 3
        assert ctl._job_retry_base_seconds == 5.0
        assert ctl._job_retry_max_seconds == 120.0
        assert ctl._history_cache_limit == 250
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_start_youtube_transcription_persists_job_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "YOUTUBE_PREFER_CAPTIONS", True)
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
    assert job.payload["preferCaptions"] is True
    assert rec.processing_started_at
    assert rec.to_public(include_content=False)["processingStartedAt"] == rec.processing_started_at


@pytest.mark.asyncio
async def test_start_youtube_transcription_persists_caption_override(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)

    async def _fake_run(rec, *, provider):
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
        patch("src.web_api._validate_provider_ready", return_value=None),
    ):
        rec = await ctl.start_youtube_transcription(
            {
                "url": "https://youtube.com/watch?v=test123",
                "preferCaptions": False,
            }
        )
        await asyncio.gather(ctl._running_tasks[rec.id], return_exceptions=True)

    job = store.get(ctl._job_ids_by_transcript[rec.id])
    assert job is not None
    assert job.payload["preferCaptions"] is False
    assert rec._youtube_prefer_captions is False


@pytest.mark.asyncio
async def test_start_youtube_transcription_rejects_non_youtube_url(tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )

    with pytest.raises(ValueError, match="Unsupported YouTube URL"):
        await ctl.start_youtube_transcription(
            {"url": "http://127.0.0.1:8765/api/runtime/support-bundle"}
        )

    assert ctl._history == []
    assert ctl._running_tasks == {}


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
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
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
    assert rec.status == "stopped"
    assert rec.step == "Stopped by user"
    save_mock.assert_awaited_once_with(rec)
    assert any(call.kwargs.get("reason") == "canceled" for call in broadcast_mock.await_args_list)


@pytest.mark.asyncio
async def test_cancel_transcript_removes_owned_upload_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    upload_dir = ctl._downloads_dir / "files" / "cancel-owned-upload"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.sleep(0)
        assert await ctl.cancel_transcript(rec.id) is True
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "stopped"
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_cancel_transcript_preserves_external_source_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=JobStore(db_path=tmp_path / "jobs.db"))
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    sample_file = external_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.sleep(0)
        assert await ctl.cancel_transcript(rec.id) is True
        await asyncio.gather(ctl._running_tasks[rec.id], return_exceptions=True)

    assert external_dir.exists()
    assert sample_file.exists()


def test_live_record_start_exposes_wall_clock_attempt_start() -> None:
    rec = TranscriptRecord(
        id="live-attempt-start",
        title="Live",
        date="Today",
        duration="00:00",
        status="recording",
        type="mic",
        language="de",
    )

    rec.start()

    assert rec.processing_started_at
    assert datetime.fromisoformat(rec.processing_started_at)
    assert rec.to_public(include_content=False)["processingStartedAt"] == rec.processing_started_at


@pytest.mark.asyncio
async def test_history_update_throttle_preserves_multiple_transcript_changes() -> None:
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._history_broadcast_interval = 10.0
    ctl._history_broadcast_last = time.monotonic()
    first = TranscriptRecord(
        id="first-update",
        title="First",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="de",
    )
    second = TranscriptRecord(
        id="second-update",
        title="Second",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="de",
    )

    with patch.object(ctl, "broadcast", new=AsyncMock()) as broadcast_mock:
        await ctl._broadcast_history_updated(record=first, reason="completed")
        await ctl._broadcast_history_updated(record=second, reason="completed")

        assert ctl._history_broadcast_pending_payload == {
            "reason": "coalesced_multiple_transcripts"
        }
        await ctl._broadcast_history_updated(force=True)

    payload = broadcast_mock.await_args.args[0]
    assert payload["type"] == "history_updated"
    assert payload["reason"] == "coalesced_multiple_transcripts"
    assert "transcriptId" not in payload
    assert "transcriptType" not in payload


@pytest.mark.asyncio
async def test_shutdown_cancellation_keeps_background_job_resumable(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    run_started = asyncio.Event()

    async def _slow_run(_rec, _path, *, provider):
        run_started.set()
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.wait_for(run_started.wait(), timeout=1.0)
        task = ctl._running_tasks[rec.id]
        ctl._shutting_down = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    job = store.get_by_transcript_id(rec.id)
    assert job is not None
    assert job.status == JobStatus.RUNNING
    assert rec.status == "processing"
    assert sample_file.exists()
    save_mock.assert_not_awaited()
    assert not any(call.kwargs.get("reason") == "canceled" for call in broadcast_mock.await_args_list)


@pytest.mark.asyncio
async def test_delete_running_transcript_waits_for_cancellation_before_storage_delete(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    run_started = asyncio.Event()
    events: list[str] = []

    async def _slow_run(_rec, _path, *, provider):
        run_started.set()
        await asyncio.sleep(10)

    async def _save(record):
        events.append(f"save:{record.status}")

    def _delete(transcript_id):
        events.append(f"delete:{transcript_id}")
        return True

    monkeypatch.setattr(web_api.db, "delete_transcript", _delete)
    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock(side_effect=_save)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.wait_for(run_started.wait(), timeout=1.0)
        events.clear()

        status, deleted = await ctl.delete_transcript_record(rec.id)

    assert status == "deleted"
    assert deleted is rec
    assert rec.status == "stopped"
    assert ctl._get_history_record(rec.id) is None
    assert rec.id not in ctl._job_ids_by_transcript
    assert events == [f"save:stopped", f"delete:{rec.id}"]
    assert broadcast_mock.await_args_list[-1].kwargs["reason"] == "deleted"
    job = store.get_by_transcript_id(rec.id)
    assert job is None


@pytest.mark.asyncio
async def test_delete_transcript_keeps_history_when_storage_delete_fails(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=JobStore(db_path=tmp_path / "jobs.db"))
    rec = TranscriptRecord(
        id="delete-failure",
        title="Keep me",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
    )
    ctl._add_to_history(rec)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _transcript_id: False)

    with patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock:
        status, deleted = await ctl.delete_transcript_record(rec.id)

    assert status == "persistence_error"
    assert deleted is rec
    assert ctl._get_history_record(rec.id) is rec
    broadcast_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_waits_for_inflight_save_and_blocks_later_resurrection(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=JobStore(db_path=tmp_path / "jobs.db"))
    rec = TranscriptRecord(
        id="summary-delete-race",
        title="Delete during summary",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        content="Original transcript",
    )
    ctl._add_to_history(rec)
    save_started = threading.Event()
    release_save = threading.Event()
    events: list[str] = []

    def _save(_snapshot):
        events.append("save-started")
        save_started.set()
        assert release_save.wait(timeout=2.0)
        events.append("save-finished")

    def _delete(_transcript_id):
        events.append("deleted")
        return True

    monkeypatch.setattr(web_api.db, "save_transcript", _save)
    monkeypatch.setattr(web_api.db, "delete_transcript", _delete)
    with patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()):
        save_task = asyncio.create_task(ctl._save_transcript_to_db_async(rec))
        assert await asyncio.to_thread(save_started.wait, 1.0)
        delete_task = asyncio.create_task(ctl.delete_transcript_record(rec.id))
        await asyncio.sleep(0)
        release_save.set()
        await save_task
        status, deleted = await delete_task

        rec.summary = "Late summary"
        await ctl._save_transcript_to_db_async(rec)

    assert status == "deleted"
    assert deleted is rec
    assert events == ["save-started", "save-finished", "deleted"]
    assert rec.id in ctl._deleted_transcript_ids


@pytest.mark.asyncio
async def test_critical_transcript_save_retries_and_reports_permanent_failure(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = TranscriptRecord(
        id="critical-save",
        title="Critical save",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        content="Persist me",
    )
    attempts = 0

    def _eventually_save(_snapshot):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("database temporarily locked")

    monkeypatch.setattr(web_api.db, "save_transcript", _eventually_save)
    assert await ctl._save_transcript_to_db_async(rec, require_success=True) is True
    assert attempts == 3
    assert rec._persistence_failed is False

    attempts = 0

    def _never_save(_snapshot):
        nonlocal attempts
        attempts += 1
        raise OSError("disk full")

    monkeypatch.setattr(web_api.db, "save_transcript", _never_save)
    with pytest.raises(web_api.TranscriptPersistenceError, match="disk full"):
        await ctl._save_transcript_to_db_async(rec, require_success=True)
    assert attempts == 3
    assert rec._persistence_failed is True


@pytest.mark.asyncio
async def test_transcript_search_does_not_scan_completed_history(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    active = TranscriptRecord(
        id="active-search-record",
        title="Needle in active job",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )

    class _NoHistoryScan(list):
        def __iter__(self):
            raise AssertionError("search scanned the complete history")

    ctl._history = _NoHistoryScan()
    ctl._history_by_id = {active.id: active}
    ctl._running_tasks = {active.id: object()}
    monkeypatch.setattr(web_api.db, "existing_transcript_ids", lambda _ids: set())
    monkeypatch.setattr(
        web_api.db,
        "search_transcript_metadata",
        lambda *_args, **_kwargs: {"items": [], "total": 0},
    )

    result = await ctl.list_transcripts(query="needle")

    assert result["total"] == 1
    assert result["items"][0]["id"] == active.id


@pytest.mark.asyncio
async def test_summary_task_registry_rejects_duplicate_provider_work():
    ctl = ScriberWebController(asyncio.get_running_loop())
    first = asyncio.create_task(asyncio.sleep(10))
    second = asyncio.create_task(asyncio.sleep(10))
    try:
        assert ctl._register_summary_task("same-transcript", first) is True
        assert ctl._register_summary_task("same-transcript", first) is True
        assert ctl._register_summary_task("same-transcript", second) is False

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)

        assert "same-transcript" not in ctl._summary_tasks
        assert ctl._register_summary_task("same-transcript", second) is True
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_old_task_does_not_unregister_replacement():
    ctl = ScriberWebController(asyncio.get_running_loop())
    first = asyncio.create_task(asyncio.sleep(10))
    replacement = asyncio.create_task(asyncio.sleep(10))
    try:
        ctl._register_task("same-transcript", first)
        ctl._register_task("same-transcript", replacement)

        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)

        assert ctl._running_tasks["same-transcript"] is replacement
    finally:
        first.cancel()
        replacement.cancel()
        await asyncio.gather(first, replacement, return_exceptions=True)


@pytest.mark.asyncio
async def test_delete_transcript_cancels_active_summary(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    rec = TranscriptRecord(
        id="delete-active-summary",
        title="Delete me",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        content="Transcript",
    )
    ctl._add_to_history(rec)
    summary_task = asyncio.create_task(asyncio.sleep(10))
    assert ctl._register_summary_task(rec.id, summary_task)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _transcript_id: True)

    with patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()):
        status, _ = await ctl.delete_transcript_record(rec.id)

    await asyncio.sleep(0)
    assert status == "deleted"
    assert summary_task.cancelled()
    assert rec.id not in ctl._summary_tasks


@pytest.mark.asyncio
async def test_shutdown_drain_keeps_background_job_resumable(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="shutdown-resume",
        title="Resume after restart",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        source_url=str(audio_path),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(audio_path)},
    )
    ctl._job_ids_by_transcript[rec.id] = job.id
    started = asyncio.Event()

    async def slow_transcription(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(ctl, "_select_available_provider", lambda: "soniox")
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_run_file_transcription", slow_transcription)

    ctl._schedule_file_job(rec, audio_path)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    pending = await ctl.drain_background_tasks_for_shutdown(timeout_seconds=1.0)

    persisted = store.get(job.id)
    assert pending == 0
    assert rec.status == "processing"
    assert persisted is not None
    assert persisted.status == JobStatus.RUNNING
    assert rec.id not in ctl._running_tasks


@pytest.mark.asyncio
async def test_shutdown_drain_waits_for_scheduled_transcript_write(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = TranscriptRecord(
        id="shutdown-persist",
        title="Persist before exit",
        date="Today",
        duration="00:01",
        status="completed",
        type="mic",
        language="de",
        content="Last transcript",
    )
    write_started = threading.Event()
    release_write = threading.Event()
    saved_ids: list[str] = []

    def _save(snapshot):
        write_started.set()
        assert release_write.wait(timeout=2.0)
        saved_ids.append(snapshot["id"])

    monkeypatch.setattr(web_api.db, "save_transcript", _save)
    ctl._schedule_transcript_save(rec)
    assert await asyncio.to_thread(write_started.wait, 1.0)

    drain_task = asyncio.create_task(
        ctl.drain_background_tasks_for_shutdown(timeout_seconds=1.0)
    )
    await asyncio.sleep(0)
    assert drain_task.done() is False
    release_write.set()

    assert await drain_task == 0
    assert saved_ids == [rec.id]
    assert not ctl._transcript_persist_tasks


def test_deleted_transcript_tombstones_are_bounded():
    ctl = ScriberWebController(asyncio.new_event_loop())
    try:
        for index in range(web_api._MAX_DELETED_TRANSCRIPT_TOMBSTONES + 5):
            ctl._mark_transcript_deleted(f"transcript-{index}")

        assert len(ctl._deleted_transcript_ids) == web_api._MAX_DELETED_TRANSCRIPT_TOMBSTONES
        assert "transcript-0" not in ctl._deleted_transcript_ids
        assert f"transcript-{web_api._MAX_DELETED_TRANSCRIPT_TOMBSTONES + 4}" in ctl._deleted_transcript_ids
    finally:
        ctl._loop.close()


@pytest.mark.asyncio
async def test_startup_history_does_not_materialize_all_database_metadata(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    monkeypatch.setattr(
        web_api.db,
        "load_transcript_metadata",
        lambda: (_ for _ in ()).throw(AssertionError("full history load used")),
    )

    ctl._load_transcripts_from_db()

    assert ctl._history == []
    assert ctl._history_by_id == {}


@pytest.mark.asyncio
async def test_unfiltered_history_merges_active_jobs_with_database_page(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    active = TranscriptRecord(
        id="active-page-record",
        title="Active",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        created_at="2026-01-03T00:00:00",
    )
    ctl._add_to_history(active)
    ctl._running_tasks = {active.id: object()}
    calls: list[dict] = []

    def _page(**kwargs):
        calls.append(kwargs)
        return {
            "items": [{"id": "persisted", "status": "completed", "type": "file"}],
            "total": 1,
        }

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", _page)

    result = await ctl.list_transcripts(transcript_type="file", limit=2)

    assert [item["id"] for item in result["items"]] == [active.id, "persisted"]
    assert result["total"] == 2
    assert calls == [{
        "transcript_type": "file",
        "offset": 0,
        "limit": 1,
        "include_incomplete": True,
        "exclude_ids": (active.id,),
    }]


@pytest.mark.asyncio
async def test_retry_waiting_job_remains_visible_without_running_task(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    waiting = TranscriptRecord(
        id="retry-waiting",
        title="Waiting for retry",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Retrying in 30s (1/3)",
    )
    ctl._add_to_history(waiting)
    calls: list[dict] = []

    def _page(**kwargs):
        calls.append(kwargs)
        return {"items": [{"id": "persisted", "status": "completed", "type": "file"}], "total": 1}

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", _page)

    result = await ctl.list_transcripts(transcript_type="file", limit=2)

    assert waiting.id not in ctl._running_tasks
    assert [item["id"] for item in result["items"]] == [waiting.id, "persisted"]
    assert result["total"] == 2
    assert calls[0]["include_incomplete"] is True
    assert calls[0]["exclude_ids"] == (waiting.id,)


@pytest.mark.asyncio
async def test_retry_discards_partial_output_from_failed_attempt(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="retry-partial-output",
        title="Retry partial output",
        date="Today",
        duration="--:--",
        status="processing",
        type="file",
        language="auto",
    )
    rec.append_final_text("partial first segment")
    rec.append_final_text("partial second segment")
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(tmp_path / "sample.wav")},
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)

    scheduled = await ctl._schedule_retry_if_allowed(rec, TimeoutError("provider timeout"))

    assert scheduled is True
    assert rec.content_text() == ""
    assert rec.to_public(include_content=False)["preview"] == rec.title
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_transcript_persistence_failure_is_retryable_without_penalizing_provider(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="retry-storage-failure",
        title="Retry storage failure",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        content="Already transcribed",
        _persistence_failed=True,
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(tmp_path / "sample.wav")},
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)

    scheduled = await ctl._schedule_retry_if_allowed(
        rec,
        web_api.TranscriptPersistenceError("Failed to save transcript to database"),
    )

    assert scheduled is True
    assert rec.status == "processing"
    assert rec._persistence_failed is True
    assert rec.content_text() == ""
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_resumed_retry_discards_partial_output_from_older_runtime(monkeypatch, tmp_path):
    source = tmp_path / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="resume-partial-output",
        job_type=web_api.JobType.FILE,
        payload={"path": str(source), "title": "Resume partial"},
    )
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id=job.transcript_id,
        title="Resume partial",
        date="Today",
        duration="--:--",
        status="processing",
        type="file",
        language="auto",
        source_url=str(source),
        content="stale partial output",
    )
    ctl._add_to_history(rec)
    scheduled: list[TranscriptRecord] = []
    monkeypatch.setattr(ctl, "_schedule_file_job", lambda record, *_args, **_kwargs: scheduled.append(record))

    resumed = await ctl.resume_pending_jobs()

    assert resumed == 1
    assert scheduled == [rec]
    assert rec.content_text() == ""


@pytest.mark.asyncio
async def test_active_jobs_filling_page_still_count_persisted_history(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    active = TranscriptRecord(
        id="active-full-page",
        title="Active",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )
    ctl._add_to_history(active)
    ctl._running_tasks = {active.id: object()}
    monkeypatch.setattr(
        web_api.db,
        "load_transcript_metadata_page",
        lambda **kwargs: {"items": [], "total": 3},
    )

    result = await ctl.list_transcripts(transcript_type="file", limit=1)

    assert [item["id"] for item in result["items"]] == [active.id]
    assert result["total"] == 4
    assert result["hasMore"] is True


@pytest.mark.asyncio
async def test_transcript_list_rejects_oversized_search_and_invalid_type():
    ctl = ScriberWebController(asyncio.get_running_loop())

    with pytest.raises(ValueError, match="search exceeds"):
        await ctl.list_transcripts(query="x" * (web_api._TRANSCRIPT_SEARCH_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="Invalid transcript type"):
        await ctl.list_transcripts(transcript_type="arbitrary")

    ctl.shutdown()


@pytest.mark.asyncio
async def test_transcript_list_clamps_extreme_offset(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    calls: list[dict] = []

    def _page(**kwargs):
        calls.append(kwargs)
        return {"items": [], "total": 0}

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", _page)

    result = await ctl.list_transcripts(offset=10**100)

    assert result["offset"] == web_api._TRANSCRIPT_OFFSET_MAX
    assert calls[0]["offset"] == web_api._TRANSCRIPT_OFFSET_MAX
    ctl.shutdown()


@pytest.mark.asyncio
async def test_delete_transcript_supports_database_only_history(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    persisted = {
        "id": "database-only",
        "title": "Persisted",
        "date": "Today",
        "duration": "00:01",
        "status": "completed",
        "type": "mic",
        "language": "de",
        "content": "text",
        "createdAt": "2026-01-01T00:00:00",
        "updatedAt": "2026-01-01T00:00:00",
    }
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _id: persisted)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _id: True)
    with patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()):
        status, deleted = await ctl.delete_transcript_record("database-only")

    assert status == "deleted"
    assert deleted is not None
    assert deleted.id == "database-only"


@pytest.mark.asyncio
async def test_resume_scan_does_not_rerun_terminal_database_transcript(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job = store.enqueue(
        transcript_id="already-complete",
        job_type="file",
        payload={"path": str(tmp_path / "missing.wav"), "title": "Done"},
    )
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    monkeypatch.setattr(
        web_api.db,
        "get_transcript",
        lambda _id: {
            "id": "already-complete",
            "title": "Done",
            "date": "Today",
            "duration": "00:01",
            "status": "completed",
            "type": "file",
            "language": "de",
            "content": "finished",
        },
    )

    resumed = await ctl.resume_pending_jobs()

    assert resumed == 0
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.COMPLETED
    assert "already-complete" not in ctl._running_tasks


@pytest.mark.asyncio
async def test_runtime_history_cache_is_bounded_and_idempotent():
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._history_cache_limit = 25

    records = [
        TranscriptRecord(
            id=f"cached-{index}",
            title=f"Cached {index}",
            date="Today",
            duration="00:01",
            status="completed",
            type="mic",
            language="auto",
        )
        for index in range(30)
    ]
    for record in records:
        ctl._add_to_history(record)

    newest = records[-1]
    ctl._add_to_history(newest)

    assert len(ctl._history) == 25
    assert sum(item.id == newest.id for item in ctl._history) == 1
    assert ctl._history_by_id[newest.id] is newest
    assert records[0].id not in ctl._history_by_id


@pytest.mark.asyncio
async def test_history_database_page_does_not_block_event_loop(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    query_started = threading.Event()
    release_query = threading.Event()

    def _slow_page(**_kwargs):
        query_started.set()
        assert release_query.wait(timeout=2.0)
        return {"items": [], "total": 0}

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", _slow_page)
    request_task = asyncio.create_task(ctl.list_transcripts())
    assert await asyncio.to_thread(query_started.wait, 1.0)

    await asyncio.sleep(0)
    assert request_task.done() is False

    release_query.set()
    result = await request_task
    assert result["items"] == []


@pytest.mark.asyncio
async def test_summary_state_update_avoids_full_transcript_rewrite(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = TranscriptRecord(
        id="partial-summary-save",
        title="Long transcript",
        date="Today",
        duration="10:00",
        status="completed",
        type="file",
        language="de",
        content="large content" * 1000,
    )
    rec.mark_summary_completed("short summary")
    calls: list[dict] = []

    def _update(_transcript_id, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(web_api.db, "update_transcript_summary_state", _update)
    monkeypatch.setattr(
        web_api.db,
        "save_transcript",
        lambda _record: (_ for _ in ()).throw(AssertionError("full transcript rewrite")),
    )

    await ctl._save_transcript_summary_state_async(rec, include_summary=True)

    assert calls == [
        {
            "status": "completed",
            "error": "",
            "summary": "short summary",
            "step": "Completed",
        }
    ]


@pytest.mark.asyncio
async def test_critical_summary_state_save_retries_and_reports_failure(monkeypatch):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = TranscriptRecord(
        id="critical-summary-save",
        title="Summary persistence",
        date="Today",
        duration="00:10",
        status="completed",
        type="file",
        language="de",
        content="Transcript",
        summary="Summary",
        summary_status="completed",
    )
    attempts = 0

    def _fail_update(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("summary database locked")

    monkeypatch.setattr(web_api.db, "update_transcript_summary_state", _fail_update)

    with pytest.raises(web_api.TranscriptPersistenceError, match="summary database locked"):
        await ctl._save_transcript_summary_state_async(
            rec,
            include_summary=True,
            require_success=True,
        )

    assert attempts == 3


class _SyntheticPipeline:
    def __init__(self, *, on_transcription):
        self._on_transcription = on_transcription

    async def transcribe_file_direct(self, _path):
        self._on_transcription("Synthetic transcript text for summary failure.", True)


class _EmptyPipeline:
    async def transcribe_file_direct(self, _path):
        return None

    async def transcribe_file(self, _path):
        return None


def _completed_record(*, transcript_type: str, tmp_path) -> TranscriptRecord:
    now = datetime.now()
    return TranscriptRecord(
        id="summary-failure-record",
        title="Summary Failure",
        date="Today",
        duration="00:10",
        status="processing",
        type=transcript_type,
        language="auto",
        step="Queued",
        source_url="https://youtube.com/watch?v=summaryfailure" if transcript_type == "youtube" else "",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        _youtube_prefer_captions=False if transcript_type == "youtube" else None,
    )


@pytest.mark.asyncio
async def test_youtube_captions_skip_audio_download_and_stt_provider(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    rec._youtube_prefer_captions = True
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch(
            "src.web_api.download_youtube_transcript",
            new=AsyncMock(
                return_value=YouTubeTranscript(
                    text="Caption text without an audio upload.",
                    language="en-orig",
                    is_automatic=True,
                )
            ),
        ) as caption_mock,
        patch("src.web_api.download_youtube_audio", new=AsyncMock()) as audio_mock,
        patch("src.web_api._create_scriber_pipeline") as pipeline_mock,
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider=None)

    assert rec.status == "completed"
    assert rec.content == "Caption text without an audio upload."
    assert rec.language == "en-orig"
    assert rec._youtube_stt_provider_used == ""
    caption_mock.assert_awaited_once()
    audio_mock.assert_not_awaited()
    pipeline_mock.assert_not_called()
    save_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_youtube_auto_summary_failure_is_exposed_as_summary_state(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)

    async def _download_youtube_audio(*_args, **_kwargs):
        return audio_path

    summary_owner_observed = False

    async def _fail_summary(*_args, **_kwargs):
        nonlocal summary_owner_observed
        summary_owner_observed = ctl._summary_tasks.get(rec.id) is asyncio.current_task()
        raise RuntimeError("summary provider failed")

    def _create_pipeline(*_args, **kwargs):
        return _SyntheticPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "synthetic-summary-model")

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch("src.summarization.summarize_text", new=AsyncMock(side_effect=_fail_summary)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_save_transcript_summary_state_async", new=AsyncMock()) as summary_save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    assert rec.status == "completed"
    assert rec.summary == ""
    assert rec.summary_status == "failed"
    assert "summary provider failed" in rec.summary_error
    assert rec.to_public(include_content=True)["summaryStatus"] == "failed"
    assert save_mock.await_count == 1
    assert summary_save_mock.await_count == 2
    assert summary_owner_observed is True
    assert rec.id not in ctl._summary_tasks
    broadcast_reasons = [call.kwargs.get("reason") for call in broadcast_mock.await_args_list]
    assert "summary_pending" in broadcast_reasons
    assert "summary_failed" in broadcast_reasons


@pytest.mark.asyncio
async def test_file_transcription_empty_provider_result_fails_job(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        await ctl._run_file_transcription(rec, file_path, provider="gladia")

    assert rec.status == "failed"
    assert rec.step == "Failed"
    assert "provider returned no transcript text" in rec.content
    assert save_mock.await_count >= 1
    assert broadcast_mock.await_count >= 1
    assert file_path.exists(), "source files outside Scriber's upload workspace must never be deleted"


@pytest.mark.asyncio
async def test_file_persistence_failure_retries_job_and_preserves_owned_upload(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    file_path = ctl._downloads_dir / "files" / "upload-id" / "upload.wav"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    rec.id = "storage-retry-file"
    rec.source_url = str(file_path)
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(file_path)},
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(web_api.db, "save_transcript", lambda _snapshot: (_ for _ in ()).throw(OSError("disk full")))

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch(
            "src.web_api._create_scriber_pipeline",
            side_effect=lambda *_args, **kwargs: _SyntheticPipeline(
                on_transcription=kwargs["on_transcription"]
            ),
        ),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_record_provider_failure") as provider_failure,
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.QUEUED
    assert rec.status == "processing"
    assert rec.content_text() == ""
    assert rec._persistence_failed is True
    assert file_path.exists()
    provider_failure.assert_not_called()


@pytest.mark.asyncio
async def test_file_job_cleans_only_its_owned_upload_directory(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "owned-upload"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="gladia")

    assert not upload_dir.exists()
    assert (ctl._downloads_dir / "files").exists()


@pytest.mark.asyncio
async def test_youtube_transcription_empty_provider_result_fails_job(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)

    async def _download_youtube_audio(*_args, **_kwargs):
        return audio_path

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        await ctl._run_youtube_transcription(rec, provider="gladia")

    assert rec.status == "failed"
    assert rec.step == "Failed"
    assert "provider returned no transcript text" in rec.content
    assert save_mock.await_count >= 1
    assert broadcast_mock.await_count >= 1


@pytest.mark.asyncio
async def test_update_settings_rejects_unavailable_local_stt_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "soniox")
    monkeypatch.setattr(
        web_api,
        "_provider_readiness_error",
        lambda provider: "Local ONNX transcription is unavailable in this Scriber build."
        if provider == "onnx_local"
        else None,
    )
    ctl = ScriberWebController(asyncio.get_running_loop())

    with pytest.raises(RuntimeError, match="Local ONNX transcription is unavailable"):
        await ctl.update_settings({"defaultSttService": "onnx_local"})

    assert Config.DEFAULT_STT_SERVICE == "soniox"
    ctl.shutdown()


@pytest.mark.asyncio
async def test_late_youtube_download_progress_cannot_overwrite_transcription_step(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    now = datetime.now()
    rec = TranscriptRecord(
        id="late-download-progress",
        title="Late Download Progress",
        date="Today",
        duration="00:10",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://youtube.com/watch?v=lateprogress",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        _youtube_prefer_captions=False,
    )
    late_download_progress = {}

    async def _download_youtube_audio(*_args, **kwargs):
        callback = kwargs["on_progress"]
        late_download_progress["callback"] = callback
        callback(SimpleNamespace(status="finished", speed=None, eta=None, percent=100.0))
        return audio_path

    class _LateProgressPipeline:
        def __init__(self, *, on_transcription):
            self._on_transcription = on_transcription

        async def transcribe_file_direct(self, _path):
            late_download_progress["callback"](
                SimpleNamespace(status="finished", speed=None, eta=None, percent=100.0)
            )
            assert rec.step == "Transcribing..."
            self._on_transcription("Synthetic transcript after late progress.", True)

    def _create_pipeline(*_args, **kwargs):
        return _LateProgressPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    assert rec.status == "completed"
    assert rec.step == "Completed"
    assert "Synthetic transcript after late progress." in rec.content


@pytest.mark.asyncio
async def test_file_auto_summary_failure_is_exposed_as_summary_state(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    summary_owner_observed = False

    async def _fail_summary(*_args, **_kwargs):
        nonlocal summary_owner_observed
        summary_owner_observed = ctl._summary_tasks.get(rec.id) is asyncio.current_task()
        raise RuntimeError("summary provider failed")

    def _create_pipeline(*_args, **kwargs):
        return _SyntheticPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "synthetic-summary-model")

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch("src.summarization.summarize_text", new=AsyncMock(side_effect=_fail_summary)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_save_transcript_summary_state_async", new=AsyncMock()) as summary_save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert rec.status == "completed"
    assert rec.summary == ""
    assert rec.summary_status == "failed"
    assert "summary provider failed" in rec.summary_error
    assert rec.to_public(include_content=True)["summaryStatus"] == "failed"
    assert save_mock.await_count == 1
    assert summary_save_mock.await_count == 2
    assert summary_owner_observed is True
    assert rec.id not in ctl._summary_tasks


@pytest.mark.asyncio
async def test_file_auto_summary_cancellation_preserves_completed_transcript(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    async def _cancel_summary(*_args, **_kwargs):
        raise asyncio.CancelledError

    def _create_pipeline(*_args, **kwargs):
        return _SyntheticPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "synthetic-summary-model")

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch("src.summarization.summarize_text", new=AsyncMock(side_effect=_cancel_summary)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_save_transcript_summary_state_async", new=AsyncMock()) as summary_save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert rec.status == "completed"
    assert rec.step == "Completed"
    assert rec.summary_status == "failed"
    assert rec.summary_error == "Summary canceled"
    assert save_mock.await_count == 1
    assert summary_save_mock.await_count == 2

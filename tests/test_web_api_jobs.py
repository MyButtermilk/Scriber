import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from src import web_api
from src.config import Config
from src.data.job_store import JobStatus, JobStore
from src.pipeline import direct_file_workflow_timeout_seconds
from src.web_api import ScriberWebController, TranscriptRecord
from src.youtube_download import YouTubeCaptionCue, YouTubeDownloadError, YouTubeTranscript


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


@pytest.mark.asyncio
async def test_background_job_enqueue_failure_is_not_silently_ignored(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    rec = TranscriptRecord(
        id="queue-failure",
        title="Queue failure",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )
    monkeypatch.setattr(
        ctl._job_store,
        "enqueue",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(web_api.TranscriptPersistenceError, match="Failed to queue"):
        await ctl._enqueue_background_job_async(
            rec,
            job_type=web_api.JobType.FILE,
            payload={"path": "sample.wav"},
        )

    assert rec.id not in ctl._job_ids_by_transcript


@pytest.mark.asyncio
async def test_file_start_does_not_publish_or_schedule_an_unpersisted_job(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    monkeypatch.setattr(
        ctl._job_store,
        "enqueue",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with (
        patch("src.web_api._probe_media_duration_seconds", return_value=1.0),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
        patch.object(ctl, "_schedule_file_job") as schedule_mock,
        pytest.raises(web_api.TranscriptPersistenceError, match="Failed to queue"),
    ):
        await ctl.start_file_transcription(sample_file, "sample.wav")

    assert ctl._history == []
    broadcast_mock.assert_not_awaited()
    schedule_mock.assert_not_called()


@pytest.mark.asyncio
async def test_youtube_start_does_not_publish_or_schedule_an_unpersisted_job(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    monkeypatch.setattr(
        ctl._job_store,
        "enqueue",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with (
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
        patch.object(ctl, "_schedule_youtube_job") as schedule_mock,
        pytest.raises(web_api.TranscriptPersistenceError, match="Failed to queue"),
    ):
        await ctl.start_youtube_transcription(
            {"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"}
        )

    assert ctl._history == []
    broadcast_mock.assert_not_awaited()
    schedule_mock.assert_not_called()


@pytest.mark.asyncio
async def test_job_start_fails_when_persisted_lifecycle_row_disappears(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="missing-lifecycle-row",
        title="Missing lifecycle row",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )
    ctl._remember_job_id(rec.id, "missing-job-id")

    with pytest.raises(web_api.TranscriptPersistenceError, match="no longer exists"):
        await ctl._set_job_running_async(rec.id)


@pytest.mark.asyncio
async def test_file_runner_terminal_lifecycle_failure_releases_owned_upload(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "missing-job"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="missing-runner-lifecycle-row",
        title="Missing lifecycle row",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        source_url=str(file_path),
    )
    ctl._remember_job_id(rec.id, "missing-job-id")

    with (
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        ctl._schedule_file_job(rec, file_path)
        task = ctl._running_tasks[rec.id]
        await task

    assert rec.status == "failed"
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_retry_lookup_failure_degrades_to_terminal_failure(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="retry-read-failure",
        title="Retry read failure",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
    )
    ctl._remember_job_id(rec.id, "job-id")
    monkeypatch.setattr(
        store,
        "get",
        lambda _job_id: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    assert await ctl._schedule_retry_if_allowed(rec, TimeoutError("provider timeout")) is False


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
async def test_history_update_merges_pending_change_into_immediate_send() -> None:
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._history_broadcast_interval = 10.0
    ctl._history_broadcast_last = time.monotonic()
    first = TranscriptRecord(
        id="pending-update",
        title="Pending",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="de",
    )
    second = TranscriptRecord(
        id="immediate-update",
        title="Immediate",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="de",
    )

    with patch.object(ctl, "broadcast", new=AsyncMock()) as broadcast_mock:
        await ctl._broadcast_history_updated(record=first, reason="progress")
        ctl._history_broadcast_last = time.monotonic() - 20.0
        await ctl._broadcast_history_updated(record=second, reason="completed")

    payload = broadcast_mock.await_args.args[0]
    assert payload["reason"] == "coalesced_multiple_transcripts"
    assert "transcriptId" not in payload


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
async def test_retry_cas_loss_reconciles_terminal_job_without_scheduling(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="retry-canceled-race",
        title="Retry canceled race",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        content="partial provider output",
        step="Transcribing...",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(tmp_path / "sample.wav")},
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)
    original_set_retry = store.set_retry

    def cancel_before_retry(job_id, *, retry_at, last_error=""):
        assert store.mark_canceled(job_id, last_error="user canceled")
        return original_set_retry(
            job_id, retry_at=retry_at, last_error=last_error
        )

    monkeypatch.setattr(store, "set_retry", cancel_before_retry)
    with patch.object(ctl, "_schedule_retry_scan") as schedule_scan:
        scheduled = await ctl._schedule_retry_if_allowed(
            rec, TimeoutError("provider timeout")
        )

    assert scheduled is True
    assert rec.status == "stopped"
    assert rec.step == "user canceled"
    assert rec.content_text() == "partial provider output"
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.CANCELED
    schedule_scan.assert_not_called()


@pytest.mark.asyncio
async def test_file_runner_retry_cas_loss_keeps_canceled_state(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    source = tmp_path / "external.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="file-runner-retry-canceled-race",
        title="File retry canceled race",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        source_url=str(source),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    ctl._remember_job_id(rec.id, job.id)
    original_set_retry = store.set_retry

    def cancel_before_retry(job_id, *, retry_at, last_error=""):
        assert store.mark_canceled(job_id, last_error="user canceled")
        return original_set_retry(
            job_id, retry_at=retry_at, last_error=last_error
        )

    monkeypatch.setattr(store, "set_retry", cancel_before_retry)
    with (
        patch.object(ctl, "_select_available_provider", return_value="soniox"),
        patch("src.web_api._validate_provider_ready", return_value=None),
        patch.object(
            ctl,
            "_transcribe_file_to_canonical_artifact",
            new=AsyncMock(side_effect=TimeoutError("provider timeout")),
        ),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_schedule_retry_scan") as schedule_scan,
    ):
        ctl._schedule_file_job(rec, source)
        task = ctl._running_tasks[rec.id]
        await task

    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.CANCELED
    assert rec.status == "stopped"
    assert rec.step == "user canceled"
    assert "Timeout" not in rec.content_text()
    assert source.exists()
    # Task-registry cleanup performs its normal immediate pending-job scan, but
    # the lost retry CAS must not schedule the provider backoff delay.
    assert schedule_scan.call_args_list == [call(0.0)]


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
            "summary_format": "html",
            "step": "Completed",
        }
    ]


def test_new_transcription_attempt_resets_summary_format_to_markdown():
    rec = TranscriptRecord(
        id="summary-format-reset",
        title="Retry",
        date="Today",
        duration="00:10",
        status="completed",
        type="file",
        language="en",
        content="old content",
    )
    rec.mark_summary_completed("<section><h2>Old</h2></section>")
    assert rec.summary_format == "html"

    rec.reset_transcription_attempt()

    assert rec.summary == ""
    assert rec.summary_format == "markdown"
    assert rec.summary_status == "idle"


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
                    cues=(
                        YouTubeCaptionCue(
                            start_ms=0,
                            end_ms=1_500,
                            text="Caption text without an audio upload.",
                        ),
                    ),
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
    assert rec.content == "[0:00] Caption text without an audio upload."
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
async def test_final_file_persistence_failure_releases_owned_upload(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._job_max_attempts = 1
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "upload-id"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    rec.id = "storage-final-failure-file"
    rec.source_url = str(file_path)
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(file_path)},
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(
        web_api.db,
        "save_transcript",
        lambda _snapshot: (_ for _ in ()).throw(OSError("disk full")),
    )

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

    assert rec.status == "failed"
    assert rec._persistence_failed is True
    assert not upload_dir.exists()
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
async def test_youtube_attempt_lease_covers_download_and_long_local_diarization(
    monkeypatch, tmp_path
):
    """Regression for a paid result expiring during the local speaker pass.

    The production incident completed Azure MAI in seconds, persisted
    ``provider_result_ready``, then spent five minutes in local diarization. The
    old provider-only heartbeat had already stopped, so the final commit lost
    its 90-second lease. This compact clock proves that one guard now spans both
    the pre-provider download and the post-provider speaker phase, including the
    state-version change between them.
    """
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "downloaded.webm"
    audio_path.write_bytes(b"synthetic audio")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    rec.id = "youtube-long-postprocess-lease"
    owner = "youtube-lease-owner"
    attempt = SimpleNamespace(
        id="youtube-attempt",
        state=web_api.AttemptState.TRANSCRIBING,
        state_version=5,
        lease_owner=owner,
    )

    class ExpiringAttemptStore:
        def __init__(self):
            self._lock = threading.Lock()
            self.state = web_api.AttemptState.TRANSCRIBING
            self.version = 5
            self.owner = owner
            self.expires_at = 0.0
            self.renewed_versions: list[int] = []

        def require_attempt(self, _attempt_id):
            with self._lock:
                return SimpleNamespace(
                    state=self.state,
                    state_version=self.version,
                    lease_owner=self.owner,
                )

        def renew_attempt_lease(
            self, _attempt_id, *, owner, expected_version, ttl_seconds
        ):
            with self._lock:
                if time.monotonic() >= self.expires_at:
                    raise web_api.ArtifactConflict("Attempt lease has expired")
                if owner != self.owner or expected_version != self.version:
                    raise web_api.ArtifactConflict("Attempt lease renewal CAS lost")
                self.expires_at = time.monotonic() + ttl_seconds
                self.renewed_versions.append(expected_version)

        def enter_provider_result_ready(self):
            with self._lock:
                self.state = web_api.AttemptState.PROVIDER_RESULT_READY
                self.version = 6

        def assert_live_and_complete(self):
            with self._lock:
                assert time.monotonic() < self.expires_at
                self.state = web_api.AttemptState.COMPLETED
                self.version = 7
                self.owner = ""

    store = ExpiringAttemptStore()
    ctl._transcript_artifacts = store

    async def begin_attempt(*_args, **_kwargs):
        store.expires_at = (
            time.monotonic() + web_api._TRANSCRIPT_ARTIFACT_LEASE_TTL_SECONDS
        )
        return attempt, owner, None

    async def download_audio(*_args, **_kwargs):
        # Longer than the complete synthetic lease: the guard must already be
        # running before source preparation starts.
        await asyncio.sleep(0.07)
        return audio_path

    class Pipeline:
        last_structured_transcript_payload = None

        def __init__(self, *, on_transcription):
            self._on_transcription = on_transcription

        async def transcribe_file_direct(self, _path):
            self._on_transcription("A durable provider transcript.", True)

    def create_pipeline(*_args, **kwargs):
        return Pipeline(on_transcription=kwargs["on_transcription"])

    async def persist_provider_stage(*_args, **_kwargs):
        store.enter_provider_result_ready()
        return SimpleNamespace(
            id=attempt.id,
            state=web_api.AttemptState.PROVIDER_RESULT_READY,
            state_version=6,
            lease_owner=owner,
        )

    async def slow_local_diarization(*_args, **_kwargs):
        # This models the five-minute Sherpa timeout that exposed the incident.
        await asyncio.sleep(0.09)
        return []

    async def commit_artifact(*_args, **_kwargs):
        store.assert_live_and_complete()
        return "A durable provider transcript."

    async def finalize_content(record, **_kwargs):
        record.status = "completed"
        record.step = "Completed"

    monkeypatch.setattr(web_api, "_TRANSCRIPT_ARTIFACT_LEASE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(web_api, "_TRANSCRIPT_ARTIFACT_LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        web_api, "_TRANSCRIPT_ARTIFACT_LEASE_RETRY_DELAYS_SECONDS", (0.0, 0.001)
    )
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch.object(ctl, "_ensure_artifact_transcript_row", new=AsyncMock()),
        patch.object(
            ctl,
            "_begin_transcript_artifact_async",
            new=AsyncMock(side_effect=begin_attempt),
        ),
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=download_audio)),
        patch("src.web_api._probe_media_duration_seconds", return_value=958.0),
        patch.object(
            ctl,
            "_register_transcript_source_asset",
            new=AsyncMock(return_value=""),
        ),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=create_pipeline),
        patch.object(
            ctl,
            "_persist_provider_stage_before_local_diarization_async",
            new=AsyncMock(side_effect=persist_provider_stage),
        ),
        patch.object(
            ctl,
            "_apply_speaker_diarization_fallback",
            new=AsyncMock(side_effect=slow_local_diarization),
        ),
        patch.object(
            ctl,
            "_commit_transcript_artifact_async",
            new=AsyncMock(side_effect=commit_artifact),
        ),
        patch.object(
            ctl,
            "_finalize_youtube_content",
            new=AsyncMock(side_effect=finalize_content),
        ),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_mark_source_assets_purge_pending"),
        patch.object(ctl, "_mark_source_assets_purged"),
    ):
        await ctl._run_youtube_transcription(rec, provider="azure_mai")

    assert rec.status == "completed"
    assert 5 in store.renewed_versions
    assert 6 in store.renewed_versions


@pytest.mark.asyncio
async def test_youtube_failure_stops_lease_guard_before_attempt_cleanup(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    attempt = SimpleNamespace(id="youtube-failed-attempt", state_version=5)
    guard_active = False
    order: list[str] = []

    def start_guard(**_kwargs):
        nonlocal guard_active
        guard_active = True
        return asyncio.Event(), asyncio.create_task(asyncio.sleep(0))

    async def stop_guard(_stop, task):
        nonlocal guard_active
        order.append("stop")
        guard_active = False
        await asyncio.gather(task, return_exceptions=True)

    async def terminate(_attempt, *, owner, canceled):
        assert guard_active is False
        assert owner == "youtube-owner"
        assert canceled is False
        order.append("terminate")

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch.object(ctl, "_ensure_artifact_transcript_row", new=AsyncMock()),
        patch("src.web_api._validate_provider_ready"),
        patch.object(
            ctl,
            "_begin_transcript_artifact_async",
            new=AsyncMock(return_value=(attempt, "youtube-owner", None)),
        ),
        patch.object(ctl, "_start_transcript_artifact_lease_guard", start_guard),
        patch.object(ctl, "_stop_transcript_artifact_lease_guard", stop_guard),
        patch.object(
            ctl,
            "_terminate_artifact_attempt_before_result_async",
            new=AsyncMock(side_effect=terminate),
        ),
        patch(
            "src.web_api.download_youtube_audio",
            new=AsyncMock(side_effect=YouTubeDownloadError("download failed")),
        ),
        patch.object(ctl, "_schedule_retry_if_allowed", new=AsyncMock(return_value=False)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_mark_source_assets_purge_pending"),
        patch.object(ctl, "_mark_source_assets_purged"),
    ):
        await ctl._run_youtube_transcription(rec, provider="gladia")

    assert order[:2] == ["stop", "terminate"]
    assert guard_active is False


@pytest.mark.asyncio
async def test_file_postprocessing_failure_releases_provider_result_lease_immediately(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    file_path = tmp_path / "audio.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    owner = "file-owner"
    initial_attempt = SimpleNamespace(
        id="file-attempt",
        state=web_api.AttemptState.TRANSCRIBING,
        state_version=5,
    )
    provider_attempt = SimpleNamespace(
        id="file-attempt",
        state=web_api.AttemptState.PROVIDER_RESULT_READY,
        state_version=6,
    )
    guard_active = False
    terminated_attempts: list[tuple[object, bool]] = []

    def start_guard(**_kwargs):
        nonlocal guard_active
        guard_active = True
        return asyncio.Event(), asyncio.create_task(asyncio.sleep(0))

    async def stop_guard(_stop, task):
        nonlocal guard_active
        guard_active = False
        await asyncio.gather(task, return_exceptions=True)

    async def terminate(attempt, *, owner: str, canceled: bool):
        assert guard_active is False
        assert owner == "file-owner"
        terminated_attempts.append((attempt, canceled))

    class Pipeline:
        last_structured_transcript_payload = None

        def __init__(self, *, on_transcription):
            self._on_transcription = on_transcription

        async def transcribe_file_direct(self, _path):
            self._on_transcription("Provider transcript.", True)

    def create_pipeline(*_args, **kwargs):
        return Pipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch.object(ctl, "_ensure_artifact_transcript_row", new=AsyncMock()),
        patch("src.web_api._probe_media_duration_seconds", return_value=10.0),
        patch.object(
            ctl,
            "_register_transcript_source_asset",
            new=AsyncMock(return_value="source-asset"),
        ),
        patch.object(
            ctl,
            "_begin_transcript_artifact_async",
            new=AsyncMock(return_value=(initial_attempt, owner, None)),
        ),
        patch.object(ctl, "_start_transcript_artifact_lease_guard", start_guard),
        patch.object(ctl, "_stop_transcript_artifact_lease_guard", stop_guard),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=create_pipeline),
        patch.object(
            ctl,
            "_persist_provider_stage_before_local_diarization_async",
            new=AsyncMock(return_value=provider_attempt),
        ),
        patch.object(
            ctl,
            "_apply_speaker_diarization_fallback",
            new=AsyncMock(side_effect=RuntimeError("local diarization failed")),
        ),
        patch.object(
            ctl,
            "_terminate_artifact_attempt_before_result_async",
            new=AsyncMock(side_effect=terminate),
        ),
        pytest.raises(RuntimeError, match="local diarization failed"),
    ):
        await ctl._transcribe_file_to_canonical_artifact(
            rec,
            file_path,
            provider="gladia",
        )

    assert terminated_attempts == [(provider_attempt, False)]
    assert guard_active is False


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


@pytest.mark.asyncio
async def test_file_long_media_passes_duration_and_scaled_outer_timeout(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    file_path = tmp_path / "two-hours.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    rec.id = "file-long-duration-budget"
    captured_init: dict = {}
    captured_timeouts: list[float] = []

    class _DurationAwarePipeline:
        last_structured_transcript_payload = None

        def __init__(self, *, on_transcription, duration_seconds: float):
            self._on_transcription = on_transcription
            self._duration_seconds = duration_seconds

        def _direct_file_workflow_timeout_seconds(self, *, minimum_seconds: float):
            return direct_file_workflow_timeout_seconds(
                self._duration_seconds,
                minimum_seconds=minimum_seconds,
            )

        async def transcribe_file_direct(self, _path):
            self._on_transcription("A complete two-hour transcript.", True)

    def create_pipeline(*_args, **kwargs):
        captured_init.update(kwargs)
        return _DurationAwarePipeline(
            on_transcription=kwargs["on_transcription"],
            duration_seconds=kwargs["direct_file_expected_duration_seconds"],
        )

    async def capture_timeout(operation, *, timeout_seconds, timeout_label):
        if timeout_label == "File transcription":
            captured_timeouts.append(timeout_seconds)
        return await operation

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(ctl, "_await_with_timeout", capture_timeout)
    monkeypatch.setattr(
        ctl, "_apply_speaker_diarization_fallback", AsyncMock(return_value=[])
    )
    with (
        patch("src.web_api._probe_media_duration_seconds", return_value=7_200.0),
        patch("src.web_api._create_scriber_pipeline", side_effect=create_pipeline),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert rec.status == "completed"
    assert rec.duration == "2:00:00"
    assert captured_init["direct_file_expected_duration_seconds"] == 7_200.0
    assert captured_timeouts == [8_220.0]


@pytest.mark.asyncio
async def test_file_duration_limit_uses_concrete_frozen_route_model_before_pipeline(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    file_path = tmp_path / "too-long.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    rec.id = "file-concrete-route-duration-limit"
    observed_routes: list[tuple[str, str]] = []

    def duration_limit(provider: str, model: str | None = None):
        observed_routes.append((provider, str(model or "")))
        return 600

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(Config, "SONIOX_ASYNC_MODEL", "stt-async-concrete-test")
    with (
        patch("src.web_api._probe_media_duration_seconds", return_value=601.0),
        patch("src.web_api.meeting_max_duration_seconds", side_effect=duration_limit),
        patch("src.web_api._create_scriber_pipeline") as pipeline_mock,
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert observed_routes == [("soniox", "stt-async-concrete-test")]
    pipeline_mock.assert_not_called()
    assert rec.status == "failed"
    assert "up to 10 minutes" in rec.content


@pytest.mark.asyncio
async def test_youtube_duration_limit_is_checked_after_real_audio_download(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "downloaded-audio.webm"
    audio_path.write_bytes(b"downloaded-audio")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    rec.id = "youtube-post-download-duration-limit"
    download_finished = False
    observed_routes: list[tuple[str, str]] = []

    async def download_audio(*_args, **_kwargs):
        nonlocal download_finished
        download_finished = True
        return audio_path

    def duration_limit(provider: str, model: str | None = None):
        assert download_finished is True
        observed_routes.append((provider, str(model or "")))
        return 600

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(Config, "SONIOX_ASYNC_MODEL", "stt-async-youtube-test")
    register_asset = AsyncMock()
    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=download_audio)) as download_mock,
        patch("src.web_api._probe_media_duration_seconds", return_value=601.0),
        patch("src.web_api.meeting_max_duration_seconds", side_effect=duration_limit),
        patch("src.web_api._create_scriber_pipeline") as pipeline_mock,
        patch.object(ctl, "_register_transcript_source_asset", new=register_asset),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    download_mock.assert_awaited_once()
    assert observed_routes == [("soniox", "stt-async-youtube-test")]
    register_asset.assert_not_awaited()
    pipeline_mock.assert_not_called()
    assert rec.status == "failed"
    assert "up to 10 minutes" in rec.content


@pytest.mark.asyncio
async def test_transcript_artifact_phases_run_off_event_loop_and_commit_is_observed(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    loop_thread = threading.get_ident()
    phase_threads: dict[str, int] = {}
    attempt = SimpleNamespace(id="attempt-off-loop")

    def begin(_rec, _route):
        phase_threads["begin"] = threading.get_ident()
        return attempt, "owner", None

    def stage(**_kwargs):
        phase_threads["stage"] = threading.get_ident()
        return attempt

    def commit(_rec, **_kwargs):
        phase_threads["commit"] = threading.get_ident()
        # Approximate a large canonical projection while the loop must remain live.
        payload = [{"text": f"segment-{index}", "startMs": index * 10} for index in range(5_000)]
        assert len(web_api.json.dumps(payload)) > 100_000
        time.sleep(0.05)
        return "[0:00] Durable transcript"

    monkeypatch.setattr(ctl, "_begin_transcript_artifact", begin)
    monkeypatch.setattr(ctl, "_persist_provider_stage_before_local_diarization", stage)
    monkeypatch.setattr(ctl, "_commit_transcript_artifact", commit)

    await ctl._begin_transcript_artifact_async(rec, SimpleNamespace())
    await ctl._persist_provider_stage_before_local_diarization_async()
    heartbeat_ticks = 0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal heartbeat_ticks
        while not stop.is_set():
            heartbeat_ticks += 1
            await asyncio.sleep(0.002)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        rendered = await ctl._commit_transcript_artifact_async(rec)
    finally:
        stop.set()
        await heartbeat_task

    assert rendered == "[0:00] Durable transcript"
    assert rec.content == rendered
    assert set(phase_threads) == {"begin", "stage", "commit"}
    assert all(thread_id != loop_thread for thread_id in phase_threads.values())
    assert heartbeat_ticks >= 5


@pytest.mark.asyncio
async def test_transcript_artifact_commit_cancellation_waits_for_durable_worker(
    monkeypatch, tmp_path
):
    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def commit(_rec, **_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        finished.set()
        return "[0:00] Committed before cancellation completed"

    monkeypatch.setattr(ctl, "_commit_transcript_artifact", commit)
    task = asyncio.create_task(ctl._commit_transcript_artifact_async(rec))
    assert await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    assert await task == "[0:00] Committed before cancellation completed"
    assert finished.is_set()
    assert rec.content == "[0:00] Committed before cancellation completed"


@pytest.mark.asyncio
async def test_thread_cancellation_barrier_survives_repeated_cancel_requests():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def durable_mutation():
        started.set()
        assert release.wait(timeout=2.0)
        finished.set()

    task = asyncio.create_task(
        web_api._to_thread_cancellation_barrier(durable_mutation)
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_youtube_download_failures_do_not_open_stt_provider_breaker(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch(
            "src.web_api.download_youtube_audio",
            new=AsyncMock(side_effect=YouTubeDownloadError("connection timed out")),
        ),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        for index in range(3):
            rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
            rec.id = f"youtube-download-failure-{index}"
            await ctl._run_youtube_transcription(rec, provider="soniox")

    snapshot = ctl._provider_breaker.snapshot("soniox")
    assert snapshot.consecutive_failures == 0

    with (
        patch(
            "src.web_api.download_youtube_audio",
            new=AsyncMock(side_effect=TimeoutError("YouTube download timed out")),
        ),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        timed_out = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
        timed_out.id = "youtube-download-timeout"
        await ctl._run_youtube_transcription(timed_out, provider="soniox")

    assert ctl._provider_breaker.snapshot("soniox").consecutive_failures == 0


@pytest.mark.asyncio
async def test_youtube_provider_503_still_records_stt_provider_failure(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop())
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    rec.id = "youtube-provider-503"

    class FailingProviderPipeline:
        last_structured_transcript_payload = None

        async def transcribe_file_direct(self, _path):
            raise RuntimeError("503 provider service unavailable")

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(return_value=audio_path)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=FailingProviderPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    snapshot = ctl._provider_breaker.snapshot("soniox")
    assert snapshot.consecutive_failures == 1


@pytest.mark.asyncio
async def test_youtube_scheduler_does_not_reclassify_handled_download_failure(
    monkeypatch, tmp_path
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="youtube-scheduler-download-failure",
        title="Download failure",
        date="Today",
        duration="00:00",
        status="processing",
        type="youtube",
        language="auto",
        source_url="https://youtube.com/watch?v=test123",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._remember_job_id(rec.id, job.id)

    async def handled_download_failure(record, *, provider):
        record._youtube_stt_provider_used = provider
        record.status = "failed"
        record.step = "YouTube download failed"

    with (
        patch.object(ctl, "_select_available_provider", return_value="soniox"),
        patch("src.web_api._validate_provider_ready", return_value=None),
        patch.object(
            ctl,
            "_run_youtube_transcription",
            new=AsyncMock(side_effect=handled_download_failure),
        ),
        patch.object(ctl, "_record_provider_failure") as provider_failure,
    ):
        ctl._schedule_youtube_job(rec)
        task = ctl._running_tasks[rec.id]
        await task

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.status == JobStatus.FAILED
    provider_failure.assert_not_called()


@pytest.mark.asyncio
async def test_file_scheduler_does_not_double_count_handled_provider_failure(
    tmp_path,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="file-scheduler-provider-failure",
        title="Provider failure",
        date="Today",
        duration="00:00",
        status="processing",
        type="file",
        language="auto",
        source_url=str(file_path),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(file_path)},
    )
    ctl._remember_job_id(rec.id, job.id)

    async def handled_provider_failure(record, _file_path, *, provider):
        ctl._record_provider_failure(provider, "provider failed")
        record.status = "failed"
        record.step = "Provider failed"

    with (
        patch.object(ctl, "_select_available_provider", return_value="soniox"),
        patch("src.web_api._validate_provider_ready", return_value=None),
        patch.object(
            ctl,
            "_run_file_transcription",
            new=AsyncMock(side_effect=handled_provider_failure),
        ),
        patch.object(
            ctl, "_record_provider_failure", wraps=ctl._record_provider_failure
        ) as provider_failure,
    ):
        ctl._schedule_file_job(rec, file_path)
        task = ctl._running_tasks[rec.id]
        await task

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.status == JobStatus.FAILED
    provider_failure.assert_called_once_with("soniox", "provider failed")

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from src import web_api
from src.config import Config
from src.data.job_store import JobStatus, JobStore
from src.pipeline import direct_file_workflow_timeout_seconds
from src.runtime.cancellation import to_thread_cancellation_barrier
from src.web_api import ScriberWebController, TranscriptRecord
from src.youtube_download import YouTubeCaptionCue, YouTubeDownloadError, YouTubeTranscript


class _DurableParentHarness:
    """Small conditional-parent store for lifecycle tests with mocked persistence."""

    def __init__(self, *records: TranscriptRecord):
        self.parents = {record.id: record.to_public(include_content=True) for record in records}

    def get(self, transcript_id: str):
        parent = self.parents.get(transcript_id)
        return dict(parent) if parent is not None else None

    def save_sync(self, record) -> bool:
        candidate = dict(record) if isinstance(record, dict) else record.to_public(include_content=True)
        transcript_id = str(candidate["id"])
        current = self.parents.get(transcript_id)
        if current is not None:
            current_status = str(current.get("status", ""))
            candidate_status = str(candidate.get("status", ""))
            if current_status in {"completed", "failed", "stopped"} and current_status != candidate_status:
                return False
        self.parents[transcript_id] = candidate
        return True

    async def save(self, record: TranscriptRecord, **_kwargs) -> bool:
        return self.save_sync(record)

    async def transition(self, record: TranscriptRecord) -> bool:
        current = self.parents.get(record.id)
        if current is None or str(current.get("status", "")) != "processing":
            return False
        if record.status not in {"failed", "stopped"}:
            return False
        current["status"] = record.status
        current["step"] = record.step
        current["updatedAt"] = record.updated_at
        return True


def _install_durable_parent_harness(
    monkeypatch,
    ctl: ScriberWebController,
    *records: TranscriptRecord,
) -> _DurableParentHarness:
    harness = _DurableParentHarness(*records)
    monkeypatch.setattr(web_api.db, "get_transcript", harness.get)
    monkeypatch.setattr(web_api.db, "save_transcript", harness.save_sync)
    monkeypatch.setattr(ctl, "_transition_terminal_parent_to_db_async", harness.transition)
    return harness


def _file_upload_plan(
    ctl: ScriberWebController,
    *,
    provider: str = "soniox",
) -> web_api.FileUploadPlan:
    route = ctl._freeze_background_provider_route(
        workload="file",
        provider=provider,
        language="auto",
    )
    return web_api.FileUploadPlan(
        route=route,
        source_is_video=False,
        ingest_max_bytes=web_api._get_audio_ingest_max_bytes(provider),
        ingest_limit_label=web_api._get_audio_ingest_limit_label(provider),
        final_audio_max_bytes=web_api._get_audio_upload_max_bytes(provider),
        final_audio_limit_label=web_api._get_audio_upload_limit_label(provider),
    )


@pytest.mark.asyncio
async def test_background_job_enqueue_runs_off_event_loop(monkeypatch, tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    event_loop_thread = threading.get_ident()
    enqueue_thread = None

    def enqueue(**kwargs):
        nonlocal enqueue_thread
        enqueue_thread = threading.get_ident()
        return SimpleNamespace(id=kwargs["job_id"])

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
    assert ctl._job_ids_by_transcript[rec.id]


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
        await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=_file_upload_plan(ctl),
        )

    assert ctl._history == []
    broadcast_mock.assert_not_awaited()
    schedule_mock.assert_not_called()


@pytest.mark.asyncio
async def test_file_start_adopts_exact_job_when_enqueue_raises_after_commit(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "commit-then-raise"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    real_enqueue = store.enqueue

    def commit_then_raise(**kwargs):
        real_enqueue(**kwargs)
        raise OSError("connection dropped after commit")

    scheduled: list[tuple[TranscriptRecord, Path]] = []
    monkeypatch.setattr(store, "enqueue", commit_then_raise)
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        ctl,
        "_schedule_file_job",
        lambda rec, path: scheduled.append((rec, path)),
    )
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    rec = await ctl.start_file_transcription(
        sample_file,
        "sample.wav",
        plan=_file_upload_plan(ctl),
    )

    pending_jobs = store.list_pending()
    assert len(pending_jobs) == 1
    job = pending_jobs[0]
    assert job.transcript_id == rec.id
    assert ctl._job_ids_by_transcript[rec.id] == job.id
    assert sample_file.exists()
    assert [record.id for record in ctl._history] == [rec.id]
    assert [(record.id, path) for record, path in scheduled] == [(rec.id, sample_file)]


@pytest.mark.asyncio
async def test_file_start_retains_source_when_enqueue_commit_cannot_be_read(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "ambiguous-commit"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    real_enqueue = store.enqueue

    def commit_then_raise(**kwargs):
        real_enqueue(**kwargs)
        raise OSError("connection dropped after commit")

    monkeypatch.setattr(store, "enqueue", commit_then_raise)
    monkeypatch.setattr(store, "get", lambda _job_id: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)

    with (
        patch.object(ctl, "_schedule_file_job") as schedule_mock,
        patch.object(ctl, "_schedule_retry_scan") as retry_scan,
    ):
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=_file_upload_plan(ctl),
        )

    pending_jobs = store.list_pending()
    assert len(pending_jobs) == 1
    job = pending_jobs[0]
    assert rec.id == job.transcript_id
    assert rec.status == "processing"
    assert sample_file.exists()
    assert ctl._job_ids_by_transcript[job.transcript_id] == job.id
    assert [record.id for record in ctl._history] == [job.transcript_id]
    assert ctl._running_tasks == {}
    schedule_mock.assert_not_called()
    retry_scan.assert_called_once_with(0.0)


@pytest.mark.asyncio
async def test_file_start_cancellation_before_enqueue_releases_owned_upload(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "cancel-before-enqueue"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    probe_started = threading.Event()
    allow_probe = threading.Event()

    def delayed_probe(_path):
        probe_started.set()
        assert allow_probe.wait(timeout=2.0)
        return 1.0

    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", delayed_probe)
    task = asyncio.create_task(
        ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=_file_upload_plan(ctl),
        )
    )
    assert await asyncio.to_thread(probe_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_probe = task.done()
    allow_probe.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_before_probe is False
    assert store.list_pending() == []
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_file_start_cancellation_after_enqueue_keeps_durable_source_owned(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "cancel-after-enqueue"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    enqueue_started = threading.Event()
    allow_enqueue = threading.Event()
    real_enqueue = store.enqueue

    def delayed_enqueue(**kwargs):
        enqueue_started.set()
        assert allow_enqueue.wait(timeout=2.0)
        return real_enqueue(**kwargs)

    scheduled: list[tuple[TranscriptRecord, Path]] = []
    monkeypatch.setattr(store, "enqueue", delayed_enqueue)
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        ctl,
        "_schedule_file_job",
        lambda rec, path: scheduled.append((rec, path)),
    )
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    task = asyncio.create_task(
        ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=_file_upload_plan(ctl),
        )
    )
    assert await asyncio.to_thread(enqueue_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_enqueue = task.done()
    allow_enqueue.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending_jobs = store.list_pending()
    assert completed_before_enqueue is False
    assert len(pending_jobs) == 1
    job = pending_jobs[0]
    assert sample_file.exists()
    assert ctl._job_ids_by_transcript[job.transcript_id] == job.id
    assert [record.id for record in ctl._history] == [job.transcript_id]
    assert [(record.id, path) for record, path in scheduled] == [(job.transcript_id, sample_file)]


@pytest.mark.asyncio
async def test_file_enqueue_adoption_excludes_resume_scan_and_runs_job_once(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "enqueue-resume-race"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    plan = _file_upload_plan(ctl)
    committed = threading.Event()
    allow_enqueue_return = threading.Event()
    scan_started = threading.Event()
    provider_started = asyncio.Event()
    allow_provider_finish = asyncio.Event()
    real_enqueue = store.enqueue
    real_list_pending = store.list_pending
    real_mark_running = store.mark_running
    real_register_task = ctl._register_task
    real_add_to_history = ctl._add_to_history
    mark_running_calls: list[str] = []
    registered_tasks: list[asyncio.Task] = []
    history_calls: list[str] = []
    provider_calls: list[str] = []

    def enqueue_then_pause(**kwargs):
        job = real_enqueue(**kwargs)
        committed.set()
        assert allow_enqueue_return.wait(timeout=2.0)
        return job

    def observed_list_pending(*, limit=100):
        scan_started.set()
        return real_list_pending(limit=limit)

    def observed_mark_running(job_id):
        mark_running_calls.append(job_id)
        return real_mark_running(job_id)

    def observed_register_task(transcript_id, task):
        registered_tasks.append(task)
        real_register_task(transcript_id, task)

    def observed_add_to_history(rec):
        history_calls.append(rec.id)
        real_add_to_history(rec)

    async def run_file_once(rec, _path, *, provider):
        assert provider == plan.route.provider
        provider_calls.append(rec.id)
        provider_started.set()
        await allow_provider_finish.wait()

    monkeypatch.setattr(store, "enqueue", enqueue_then_pause)
    monkeypatch.setattr(store, "list_pending", observed_list_pending)
    monkeypatch.setattr(store, "mark_running", observed_mark_running)
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", AsyncMock(return_value=plan.route))
    monkeypatch.setattr(ctl, "_run_file_transcription", run_file_once)
    monkeypatch.setattr(ctl, "_register_task", observed_register_task)
    monkeypatch.setattr(ctl, "_add_to_history", observed_add_to_history)
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    start_task = asyncio.create_task(ctl.start_file_transcription(sample_file, "sample.wav", plan=plan))
    assert await asyncio.to_thread(committed.wait, 1.0)
    resume_task = asyncio.create_task(ctl.resume_pending_jobs())
    await asyncio.to_thread(scan_started.wait, 0.1)
    allow_enqueue_return.set()
    rec = await start_task
    await resume_task
    await asyncio.wait_for(provider_started.wait(), timeout=1.0)
    allow_provider_finish.set()
    await asyncio.gather(*registered_tasks)

    assert store.get(rec.id) is not None
    assert len(store.list_pending()) == 0
    assert len(registered_tasks) == 1
    assert mark_running_calls == [rec.id]
    assert provider_calls == [rec.id]
    assert history_calls == [rec.id]


@pytest.mark.asyncio
async def test_restarted_file_job_uses_its_admitted_audio_limit(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    first_controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    route = first_controller._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="auto",
    )
    admitted_limit = 12_345
    plan = web_api.FileUploadPlan(
        route=route,
        source_is_video=False,
        ingest_max_bytes=54_321,
        ingest_limit_label="admitted-ingest",
        final_audio_max_bytes=admitted_limit,
        final_audio_limit_label="admitted-final",
    )
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="restart-file-upload-plan",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        source_url=str(file_path),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={
            "path": str(file_path),
            "executionRoute": first_controller._job_execution_route(route),
            "fileUploadPlan": plan.durable_evidence(),
        },
    )

    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    restarted._remember_job_id(rec.id, job.id)
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_BYTES", "987654")
    captured_limits: list[int] = []
    prepared = SimpleNamespace(
        path=file_path,
        selected_format=SimpleNamespace(value="wav_pcm16"),
        selection_mode=SimpleNamespace(value="pass_through"),
        implementation="test-preparation",
    )

    class PreparedContext:
        async def __aenter__(self):
            return prepared

        async def __aexit__(self, *_args):
            return False

    def prepare(_path, *, provider, model, max_bytes):
        assert provider == "soniox"
        assert model == route.model
        captured_limits.append(max_bytes)
        return PreparedContext()

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api.prepare_provider_audio_file", side_effect=prepare),
        patch.object(restarted, "_recover_bound_provider_result", new=AsyncMock(return_value=None)),
        patch.object(restarted, "_freeze_background_provider_route", return_value=route),
        patch.object(restarted, "_finalize_job_execution_route", new=AsyncMock()),
        patch.object(
            restarted,
            "_transcribe_file_route_to_canonical_artifact",
            new=AsyncMock(return_value="done"),
        ),
    ):
        result = await restarted._transcribe_file_to_canonical_artifact(
            rec,
            file_path,
            provider="soniox",
            frozen_route=route,
        )

    assert result == "done"
    assert captured_limits == [admitted_limit]


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
        await ctl.start_youtube_transcription({"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"})

    assert ctl._history == []
    broadcast_mock.assert_not_awaited()
    schedule_mock.assert_not_called()


@pytest.mark.asyncio
async def test_youtube_start_returns_processing_when_exact_commit_read_is_unavailable(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    real_enqueue = store.enqueue

    def commit_then_raise(**kwargs):
        real_enqueue(**kwargs)
        raise OSError("write result unavailable")

    monkeypatch.setattr(store, "enqueue", commit_then_raise)
    monkeypatch.setattr(store, "get", lambda _job_id: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())

    with (
        patch.object(ctl, "_schedule_youtube_job") as schedule_mock,
        patch.object(ctl, "_schedule_retry_scan") as retry_scan,
    ):
        rec = await ctl.start_youtube_transcription({"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"})

    jobs = store.list_pending()
    assert rec.status == "processing"
    assert len(jobs) == 1
    assert jobs[0].id == rec.id
    assert ctl._job_ids_by_transcript[rec.id] == rec.id
    assert ctl._uncertain_job_commits[rec.id] == rec.id
    assert [record.id for record in ctl._history] == [rec.id]
    assert ctl._running_tasks == {}
    schedule_mock.assert_not_called()
    retry_scan.assert_called_once_with(0.0)


@pytest.mark.asyncio
async def test_uncertain_youtube_enqueue_confirmed_absent_removes_admission(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    real_get = store.get
    deleted_parents: list[str] = []

    monkeypatch.setattr(store, "enqueue", lambda **_kwargs: (_ for _ in ()).throw(OSError("write unavailable")))
    monkeypatch.setattr(store, "get", lambda _job_id: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(ctl, "_schedule_retry_scan", lambda _delay: None)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: deleted_parents.append(value) or True)

    rec = await ctl.start_youtube_transcription({"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"})
    assert rec.status == "processing"
    assert [record.id for record in ctl._history] == [rec.id]

    monkeypatch.setattr(store, "get", real_get)
    await ctl.resume_pending_jobs()

    assert deleted_parents == [rec.id]
    assert ctl._history == []
    assert rec.id not in ctl._job_ids_by_transcript
    assert rec.id not in ctl._uncertain_job_commits
    assert ctl._running_tasks == {}


@pytest.mark.asyncio
async def test_youtube_start_cancellation_after_enqueue_adopts_exact_job(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    enqueue_started = threading.Event()
    allow_enqueue = threading.Event()
    real_enqueue = store.enqueue

    def delayed_enqueue(**kwargs):
        enqueue_started.set()
        assert allow_enqueue.wait(timeout=2.0)
        return real_enqueue(**kwargs)

    scheduled: list[TranscriptRecord] = []
    ensure_parent = AsyncMock()
    broadcast = AsyncMock()
    monkeypatch.setattr(store, "enqueue", delayed_enqueue)
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", ensure_parent)
    monkeypatch.setattr(ctl, "_broadcast_history_updated", broadcast)
    monkeypatch.setattr(ctl, "_schedule_youtube_job", lambda rec: scheduled.append(rec))

    task = asyncio.create_task(ctl.start_youtube_transcription({"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"}))
    assert await asyncio.to_thread(enqueue_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_enqueue = task.done()
    allow_enqueue.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending_jobs = store.list_pending()
    assert completed_before_enqueue is False
    assert len(pending_jobs) == 1
    job = pending_jobs[0]
    assert ctl._job_ids_by_transcript[job.transcript_id] == job.id
    assert [record.id for record in ctl._history] == [job.transcript_id]
    ensure_parent.assert_awaited_once_with(ctl._history[0])
    assert [record.id for record in scheduled] == [job.transcript_id]
    assert broadcast.await_args.kwargs == {
        "record": ctl._history[0],
        "reason": "job_created",
    }


@pytest.mark.asyncio
async def test_youtube_enqueue_adoption_excludes_resume_scan_and_runs_job_once(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    committed = threading.Event()
    allow_enqueue_return = threading.Event()
    scan_started = threading.Event()
    provider_started = asyncio.Event()
    allow_provider_finish = asyncio.Event()
    real_enqueue = store.enqueue
    real_list_pending = store.list_pending
    real_mark_running = store.mark_running
    real_register_task = ctl._register_task
    real_add_to_history = ctl._add_to_history
    mark_running_calls: list[str] = []
    registered_tasks: list[asyncio.Task] = []
    history_calls: list[str] = []
    provider_calls: list[str] = []

    def enqueue_then_pause(**kwargs):
        job = real_enqueue(**kwargs)
        committed.set()
        assert allow_enqueue_return.wait(timeout=2.0)
        return job

    def observed_list_pending(*, limit=100):
        scan_started.set()
        return real_list_pending(limit=limit)

    def observed_mark_running(job_id):
        mark_running_calls.append(job_id)
        return real_mark_running(job_id)

    def observed_register_task(transcript_id, task):
        registered_tasks.append(task)
        real_register_task(transcript_id, task)

    def observed_add_to_history(rec):
        history_calls.append(rec.id)
        real_add_to_history(rec)

    async def run_youtube_once(rec, *, provider):
        provider_calls.append(rec.id)
        provider_started.set()
        await allow_provider_finish.wait()

    monkeypatch.setattr(store, "enqueue", enqueue_then_pause)
    monkeypatch.setattr(store, "list_pending", observed_list_pending)
    monkeypatch.setattr(store, "mark_running", observed_mark_running)
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(
        ctl,
        "_load_or_freeze_background_route",
        AsyncMock(
            return_value=ctl._freeze_background_provider_route(
                workload="youtube",
                provider="soniox",
                language="auto",
            )
        ),
    )
    monkeypatch.setattr(ctl, "_run_youtube_transcription", run_youtube_once)
    monkeypatch.setattr(ctl, "_register_task", observed_register_task)
    monkeypatch.setattr(ctl, "_add_to_history", observed_add_to_history)
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    start_task = asyncio.create_task(
        ctl.start_youtube_transcription({"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"})
    )
    assert await asyncio.to_thread(committed.wait, 1.0)
    resume_task = asyncio.create_task(ctl.resume_pending_jobs())
    await asyncio.to_thread(scan_started.wait, 0.1)
    allow_enqueue_return.set()
    rec = await start_task
    await resume_task
    await asyncio.wait_for(provider_started.wait(), timeout=1.0)
    allow_provider_finish.set()
    await asyncio.gather(*registered_tasks)

    assert store.get(rec.id) is not None
    assert len(store.list_pending()) == 0
    assert len(registered_tasks) == 1
    assert mark_running_calls == [rec.id]
    assert provider_calls == [rec.id]
    assert history_calls == [rec.id]


@pytest.mark.asyncio
async def test_uncertain_file_enqueue_confirmed_absent_cleans_all_ownership(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "uncertain-then-absent"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    deleted_parents: list[str] = []
    real_get = store.get

    monkeypatch.setattr(store, "enqueue", lambda **_kwargs: (_ for _ in ()).throw(OSError("write unavailable")))
    monkeypatch.setattr(store, "get", lambda _job_id: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(
        web_api.db, "delete_transcript", lambda transcript_id: deleted_parents.append(transcript_id) or True
    )

    rec = await ctl.start_file_transcription(sample_file, "sample.wav", plan=_file_upload_plan(ctl))

    transcript_id = rec.id
    assert rec.status == "processing"
    assert sample_file.exists()
    monkeypatch.setattr(store, "get", real_get)
    await ctl.resume_pending_jobs()

    assert not upload_dir.exists()
    assert ctl._history == []
    assert transcript_id not in ctl._job_ids_by_transcript
    assert transcript_id not in ctl._uncertain_job_commits
    assert deleted_parents == [transcript_id]


@pytest.mark.asyncio
async def test_uncertain_file_enqueue_committed_row_is_adopted_by_exact_id_once(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "uncertain-then-committed"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    real_enqueue = store.enqueue
    real_get = store.get
    scheduled: list[str] = []

    def commit_then_raise(**kwargs):
        real_enqueue(**kwargs)
        raise OSError("write result unavailable")

    monkeypatch.setattr(store, "enqueue", commit_then_raise)
    monkeypatch.setattr(store, "get", lambda _job_id: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(ctl, "_schedule_file_job", lambda rec, _path, **_kwargs: scheduled.append(rec.id))

    rec = await ctl.start_file_transcription(sample_file, "sample.wav", plan=_file_upload_plan(ctl))

    transcript_id = rec.id
    assert rec.status == "processing"
    monkeypatch.setattr(store, "get", real_get)
    await ctl.resume_pending_jobs()
    job = real_get(transcript_id)

    assert job is not None
    assert job.id == transcript_id
    assert job.transcript_id == transcript_id
    assert scheduled == [transcript_id]
    assert transcript_id not in ctl._uncertain_job_commits
    assert sample_file.exists()


@pytest.mark.asyncio
async def test_uncertain_committed_read_observes_repeated_cancellation_before_adoption(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="uncertain-committed-cancel",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(file_path),
    )
    store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(file_path)},
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    read_started = threading.Event()
    allow_read = threading.Event()
    real_get = store.get
    scheduled: list[str] = []

    def delayed_get(job_id):
        read_started.set()
        assert allow_read.wait(timeout=2.0)
        return real_get(job_id)

    monkeypatch.setattr(store, "get", delayed_get)
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(ctl, "_schedule_file_job", lambda record, _path, **_kwargs: scheduled.append(record.id))

    task = asyncio.create_task(ctl.resume_pending_jobs())
    assert await asyncio.to_thread(read_started.wait, 1.0)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_read = task.done()
    allow_read.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_before_read is False
    assert scheduled == [rec.id]
    assert ctl._job_ids_by_transcript[rec.id] == rec.id
    assert rec.id not in ctl._uncertain_job_commits
    assert [record.id for record in ctl._history] == [rec.id]


@pytest.mark.asyncio
async def test_no_runner_cancel_observes_repeated_cancellation_before_exact_adoption(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="adoption-cancel-serialized",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    ensure_started = asyncio.Event()
    release_ensure = asyncio.Event()
    registered_tasks: list[asyncio.Task] = []
    real_register_task = ctl._register_task

    async def blocked_ensure(_record):
        ensure_started.set()
        await release_ensure.wait()

    def observed_register(transcript_id, task):
        registered_tasks.append(task)
        real_register_task(transcript_id, task)

    provider_work = AsyncMock()
    route = ctl._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language="auto",
    )
    monkeypatch.setattr(ctl, "_ensure_artifact_transcript_row", blocked_ensure)
    monkeypatch.setattr(ctl, "_register_task", observed_register)
    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", AsyncMock(return_value=route))
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    resume_task = asyncio.create_task(ctl.resume_pending_jobs())
    await asyncio.wait_for(ensure_started.wait(), timeout=5)
    cancel_task = asyncio.create_task(ctl.cancel_transcript(rec.id))
    await asyncio.sleep(0.05)
    assert not cancel_task.done()
    cancel_task.cancel()
    cancel_task.cancel()
    await asyncio.sleep(0.05)
    assert not cancel_task.done()
    release_ensure.set()

    await resume_task
    with pytest.raises(asyncio.CancelledError):
        await cancel_task

    assert registered_tasks == []
    provider_work.assert_not_awaited()
    assert store.get(job.id).status == JobStatus.CANCELED
    assert rec.status == "stopped"
    assert rec.id not in ctl._uncertain_job_commits


@pytest.mark.asyncio
async def test_prestart_runner_cancel_terminalizes_exact_job_before_resume(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="prestart-runner-cancel",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    provider_work = AsyncMock()
    route = ctl._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language="auto",
    )
    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", AsyncMock(return_value=route))
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    assert ctl._schedule_youtube_job(rec)
    assert await ctl.cancel_transcript(rec.id) is True
    await ctl.resume_pending_jobs()

    assert store.get(job.id).status == JobStatus.CANCELED
    assert rec.status == "stopped"
    assert rec.id not in ctl._running_tasks
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_task_cancel_settles_runner_that_appears_before_resume_lock(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="cancel-task-appears-before-lock",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    provider_work = AsyncMock()
    route_wait = asyncio.Event()

    async def blocked_route(*_args, **_kwargs):
        await route_wait.wait()
        raise AssertionError("canceled runner must not reach a provider route")

    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", blocked_route)
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    await ctl._resume_jobs_lock.acquire()
    try:
        cancel_task = asyncio.create_task(ctl.cancel_transcript(rec.id))
        await asyncio.sleep(0.05)
        assert rec.id in ctl._background_job_cancel_requests
        assert ctl._schedule_youtube_job(rec)
        runner_task = ctl._running_tasks[rec.id]
    finally:
        ctl._resume_jobs_lock.release()

    assert await cancel_task is True
    await asyncio.gather(runner_task, return_exceptions=True)
    await ctl.resume_pending_jobs()

    assert store.get(job.id).status == JobStatus.CANCELED
    assert rec.status == "stopped"
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_absent_read_and_cleanup_observe_repeated_cancellation(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "uncertain-absent-cancel"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="uncertain-absent-cancel",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(file_path),
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    read_started = threading.Event()
    allow_read = threading.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    deleted_parents: list[str] = []
    real_cleanup = ctl._cleanup_owned_file_source

    def delayed_absent(_job_id):
        read_started.set()
        assert allow_read.wait(timeout=2.0)
        return None

    async def delayed_cleanup(source_path, *, reason, transcript_id=""):
        cleanup_started.set()
        await allow_cleanup.wait()
        return await real_cleanup(source_path, reason=reason, transcript_id=transcript_id)

    monkeypatch.setattr(store, "get", delayed_absent)
    monkeypatch.setattr(ctl, "_cleanup_owned_file_source", delayed_cleanup)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: deleted_parents.append(value) or True)

    task = asyncio.create_task(ctl.resume_pending_jobs())
    assert await asyncio.to_thread(read_started.wait, 1.0)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_read = task.done()
    allow_read.set()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)
    completed_before_cleanup = task.done()
    allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_before_read is False
    assert completed_before_cleanup is False
    assert not upload_dir.exists()
    assert deleted_parents == [rec.id]
    assert ctl._history == []
    assert rec.id not in ctl._job_ids_by_transcript
    assert rec.id not in ctl._uncertain_job_commits


@pytest.mark.asyncio
async def test_uncertain_file_cancel_store_outage_never_reschedules(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "cancel-store-outage"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    plan = _file_upload_plan(ctl)
    rec = TranscriptRecord(
        id="uncertain-file-cancel-outage",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(source),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={
            "path": str(source),
            "executionRoute": ctl._job_execution_route(plan.route),
            "fileUploadPlan": plan.durable_evidence(),
        },
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    real_mark_terminal = store.mark_terminal_projection_pending
    store_available = False

    def mark_terminal(job_id, *, status, last_error=""):
        if not store_available:
            raise OSError("job store unavailable")
        return real_mark_terminal(job_id, status=status, last_error=last_error)

    provider_work = AsyncMock()
    monkeypatch.setattr(store, "mark_terminal_projection_pending", mark_terminal)
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(ctl, "_run_file_transcription", provider_work)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))

    assert await ctl.cancel_transcript(rec.id) is True
    assert rec.status == "stopped"
    assert source.exists()
    assert store.get(job.id).status == JobStatus.QUEUED
    assert ctl._uncertain_job_commits[rec.id] == job.id

    store_available = True
    await ctl.resume_pending_jobs()
    task = ctl._running_tasks.get(rec.id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)

    assert store.get(job.id).status == JobStatus.CANCELED
    assert rec.status == "stopped"
    assert not source.exists()
    assert rec.id not in ctl._uncertain_job_commits
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_youtube_cancel_store_outage_never_reschedules(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="uncertain-youtube-cancel-outage",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    real_mark_terminal = store.mark_terminal_projection_pending
    store_available = False

    def mark_terminal(job_id, *, status, last_error=""):
        if not store_available:
            raise OSError("job store unavailable")
        return real_mark_terminal(job_id, status=status, last_error=last_error)

    provider_work = AsyncMock()
    monkeypatch.setattr(store, "mark_terminal_projection_pending", mark_terminal)
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))

    assert await ctl.cancel_transcript(rec.id) is True
    assert rec.status == "stopped"
    assert store.get(job.id).status == JobStatus.QUEUED
    assert ctl._uncertain_job_commits[rec.id] == job.id

    store_available = True
    await ctl.resume_pending_jobs()
    task = ctl._running_tasks.get(rec.id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)

    assert store.get(job.id).status == JobStatus.CANCELED
    assert rec.status == "stopped"
    assert rec.id not in ctl._uncertain_job_commits
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_job_commit_before_parent_save_recovers_after_restart(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "terminal-commit-before-parent-restart"
    source_url = "https://www.youtube.com/watch?v=J_RxOz_ddgs"
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": source_url},
    )
    assert store.mark_canceled(job.id, last_error="Stopped by user")
    parent = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url=source_url,
    ).to_public(include_content=True)

    def get_parent(value):
        return dict(parent) if value == transcript_id else None

    async def transition_parent(record):
        if parent["status"] != "processing":
            return False
        parent["status"] = record.status
        parent["step"] = record.step
        parent["updatedAt"] = record.updated_at
        return True

    monkeypatch.setattr(web_api.db, "get_transcript", get_parent)
    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    monkeypatch.setattr(
        restarted,
        "_transition_terminal_parent_to_db_async",
        transition_parent,
        raising=False,
    )
    provider_work = AsyncMock()
    monkeypatch.setattr(restarted, "_run_youtube_transcription", provider_work)

    await restarted.resume_pending_jobs(recover_running=True)

    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.CANCELED
    assert persisted_job.terminal_projection_pending is False
    assert parent["status"] == "stopped"
    assert parent["step"] == "Stopped by user"
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_status", "parent_status", "terminal_step"),
    [
        (JobStatus.CANCELED, "stopped", "Stopped by user"),
        (JobStatus.FAILED, "failed", "terminal failure"),
    ],
)
async def test_terminal_projection_preserves_full_parent_when_history_is_metadata_only(
    monkeypatch,
    tmp_path,
    job_status,
    parent_status,
    terminal_step,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = f"metadata-parent-{job_status.value}"
    durable_parent = TranscriptRecord(
        id=transcript_id,
        title="Durable parent",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Transcribing...",
        content="valuable partial transcript",
        summary="valuable summary",
        summary_status="completed",
    )
    metadata_only = TranscriptRecord(
        id=transcript_id,
        title="Metadata only",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        content="",
        summary="",
        _content_loaded=False,
        _summary_loaded=False,
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"},
    )
    if job_status == JobStatus.CANCELED:
        assert store.mark_canceled(job.id, last_error=terminal_step)
    else:
        assert store.mark_failed(job.id, last_error=terminal_step)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl, durable_parent)

    async def replace_processing_parent(record):
        return await parents.save(record)

    monkeypatch.setattr(
        ctl,
        "_transition_terminal_parent_to_db_async",
        replace_processing_parent,
    )
    ctl._add_to_history(metadata_only)
    ctl._job_ids_by_transcript[transcript_id] = job.id
    ctl._uncertain_job_commits[transcript_id] = job.id

    await ctl.resume_pending_jobs()

    persisted = parents.get(transcript_id)
    assert persisted is not None
    assert persisted["status"] == parent_status
    assert persisted["content"] == "valuable partial transcript"
    assert persisted["summary"] == "valuable summary"
    assert persisted["summaryStatus"] == "completed"


@pytest.mark.asyncio
async def test_cancel_mark_outage_persists_intent_and_never_replays_after_restart(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "cancel-mark-outage-restart"
    source_url = "https://www.youtube.com/watch?v=J_RxOz_ddgs"
    rec = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url=source_url,
    )
    parent = rec.to_public(include_content=True)
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": source_url},
    )
    first = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    first._add_to_history(rec)
    first._remember_job_id(transcript_id, job.id)

    def get_parent(value):
        return dict(parent) if value == transcript_id else None

    async def transition_parent(record):
        if parent["status"] != "processing":
            return False
        parent["status"] = record.status
        parent["step"] = record.step
        parent["updatedAt"] = record.updated_at
        return True

    monkeypatch.setattr(web_api.db, "get_transcript", get_parent)
    monkeypatch.setattr(
        first,
        "_transition_terminal_parent_to_db_async",
        transition_parent,
        raising=False,
    )
    real_mark_terminal = store.mark_terminal_projection_pending
    monkeypatch.setattr(
        store,
        "mark_terminal_projection_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("job store unavailable")),
    )

    assert await first.cancel_transcript(transcript_id) is True
    assert parent["status"] == "stopped"
    assert store.get(job.id).status == JobStatus.QUEUED

    monkeypatch.setattr(store, "mark_terminal_projection_pending", real_mark_terminal)
    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    monkeypatch.setattr(
        restarted,
        "_transition_terminal_parent_to_db_async",
        transition_parent,
        raising=False,
    )
    provider_work = AsyncMock()
    monkeypatch.setattr(restarted, "_run_youtube_transcription", provider_work)

    await restarted.resume_pending_jobs(recover_running=True)

    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.CANCELED
    assert persisted_job.terminal_projection_pending is False
    assert parent["status"] == "stopped"
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_projection_page_rearms_until_every_row_is_settled(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    records: list[TranscriptRecord] = []
    for index in range(101):
        transcript_id = f"terminal-page-{index:03d}"
        rec = TranscriptRecord(
            id=transcript_id,
            title="YouTube",
            date="Today",
            duration="00:01",
            status="stopped",
            type="youtube",
            language="auto",
            step="Stopped by user",
            source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
        )
        records.append(rec)
        job = store.enqueue(
            transcript_id=transcript_id,
            job_id=transcript_id,
            job_type=web_api.JobType.YOUTUBE,
            payload={"url": rec.source_url},
        )
        assert store.mark_canceled(job.id, last_error=rec.step)

    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    _install_durable_parent_harness(monkeypatch, restarted, *records)
    retry_delays: list[float] = []
    monkeypatch.setattr(restarted, "_schedule_retry_scan", retry_delays.append)
    provider_work = AsyncMock()
    monkeypatch.setattr(restarted, "_run_youtube_transcription", provider_work)

    await restarted.resume_pending_jobs(limit=1)

    assert len(store.list_terminal_projection_pending(limit=200)) == 1
    assert 0.0 in retry_delays

    await restarted.resume_pending_jobs(limit=1)

    assert store.list_terminal_projection_pending(limit=200) == []
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_parent_cas_outage_uses_durable_job_intent_and_recovers_after_restart(
    monkeypatch,
    tmp_path,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "cancel-parent-cas-outage"
    rec = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    first = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, first, rec)
    first._add_to_history(rec)
    first._remember_job_id(transcript_id, job.id)
    monkeypatch.setattr(
        first,
        "_transition_terminal_parent_to_db_async",
        AsyncMock(return_value=False),
    )

    assert await first.cancel_transcript(transcript_id) is True
    pending = store.get(job.id)
    assert pending is not None
    assert pending.status == JobStatus.CANCELED
    assert pending.terminal_projection_pending is True
    assert parents.get(transcript_id)["status"] == "processing"

    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    monkeypatch.setattr(
        restarted,
        "_transition_terminal_parent_to_db_async",
        parents.transition,
    )
    provider_work = AsyncMock()
    monkeypatch.setattr(restarted, "_run_youtube_transcription", provider_work)

    await restarted.resume_pending_jobs(recover_running=True)

    settled = store.get(job.id)
    assert settled is not None
    assert settled.status == JobStatus.CANCELED
    assert settled.terminal_projection_pending is False
    assert parents.get(transcript_id)["status"] == "stopped"
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_without_any_durable_proof_retries_without_provider_replay(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "cancel-no-durable-proof"
    rec = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._add_to_history(rec)
    ctl._remember_job_id(transcript_id, job.id)
    parent_available = False
    job_available = False

    async def transition_parent(record):
        if not parent_available:
            return False
        return await parents.transition(record)

    real_mark = store.mark_terminal_projection_pending

    def mark_terminal(*args, **kwargs):
        if not job_available:
            raise OSError("job store unavailable")
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(ctl, "_transition_terminal_parent_to_db_async", transition_parent)
    monkeypatch.setattr(store, "mark_terminal_projection_pending", mark_terminal)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)

    with pytest.raises(web_api.CancellationPersistenceUnavailable):
        await ctl.cancel_transcript(transcript_id)

    assert rec.status == "processing"
    assert store.get(job.id).status == JobStatus.QUEUED
    assert parents.get(transcript_id)["status"] == "processing"

    await ctl.resume_pending_jobs()

    assert rec.status == "processing"
    assert rec.step == "Cancellation pending"
    assert store.get(job.id).status == JobStatus.QUEUED
    assert rec.id in ctl._uncertain_job_commits
    provider_work.assert_not_awaited()

    parent_available = True
    job_available = True
    await ctl.resume_pending_jobs()

    assert store.get(job.id).status == JobStatus.CANCELED
    assert parents.get(transcript_id)["status"] == "stopped"
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_adopts_terminal_job_alignment_that_committed_before_raise(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "cancel-align-commit-before-raise"
    rec = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    assert store.mark_completed(job.id)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    durable_parent = TranscriptRecord(
        id=transcript_id,
        title="YouTube",
        date="Today",
        duration="00:01",
        status="stopped",
        type="youtube",
        language="auto",
        step="Stopped by user",
        source_url=rec.source_url,
    )
    _install_durable_parent_harness(monkeypatch, ctl, durable_parent)
    ctl._add_to_history(rec)
    ctl._remember_job_id(rec.id, job.id)
    real_mark = store.mark_terminal_projection_pending

    def commit_then_raise(*args, **kwargs):
        assert real_mark(*args, **kwargs)
        raise OSError("commit result unavailable")

    monkeypatch.setattr(store, "mark_terminal_projection_pending", commit_then_raise)

    assert await ctl.cancel_transcript(transcript_id) is True

    settled = store.get(job.id)
    assert settled is not None
    assert settled.status == JobStatus.CANCELED
    assert settled.terminal_projection_pending is False


@pytest.mark.asyncio
async def test_terminal_projection_keyset_advances_past_poison_page(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    poisoned_ids: list[str] = []
    for index in range(100):
        transcript_id = f"projection-poison-{index:03d}"
        poisoned_ids.append(transcript_id)
        job = store.enqueue(
            transcript_id=transcript_id,
            job_id=transcript_id,
            job_type=web_api.JobType.YOUTUBE,
            payload={"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"},
        )
        assert store.mark_canceled(job.id, last_error="Stopped by user")

    target = TranscriptRecord(
        id="projection-target-100",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="stopped",
        type="youtube",
        language="auto",
        step="Stopped by user",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    target_job = store.enqueue(
        transcript_id=target.id,
        job_id=target.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": target.source_url},
    )
    assert store.mark_canceled(target_job.id, last_error=target.step)
    target_parent = target.to_public(include_content=True)
    monkeypatch.setattr(
        web_api.db,
        "get_transcript",
        lambda value: dict(target_parent) if value == target.id else None,
    )
    monkeypatch.setattr(
        web_api.db,
        "transcript_exists_or_raise",
        lambda value: value in poisoned_ids,
    )
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    retry_delays: list[float] = []
    monkeypatch.setattr(ctl, "_schedule_retry_scan", retry_delays.append)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)

    await ctl.resume_pending_jobs(limit=1)

    assert store.get(target_job.id).terminal_projection_pending is True
    assert 0.0 not in retry_delays
    assert ctl._uncertain_job_commits == {}

    await ctl.resume_pending_jobs(limit=1)

    assert store.get(target_job.id).terminal_projection_pending is False
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED],
)
async def test_terminal_pending_file_with_confirmed_missing_parent_cleans_source_then_job(
    monkeypatch,
    tmp_path,
    terminal_status,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    transcript_id = f"missing-parent-{terminal_status.value}"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    if terminal_status == JobStatus.COMPLETED:
        assert store.mark_completed(job.id)
    elif terminal_status == JobStatus.FAILED:
        assert store.mark_failed(job.id, last_error="terminal failure")
    else:
        assert store.mark_canceled(job.id, last_error="Stopped by user")
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: None)
    monkeypatch.setattr(web_api.db, "transcript_exists_or_raise", lambda _value: False)
    order: list[str] = []
    real_cleanup = ctl._cleanup_owned_file_source
    real_delete = store.delete_exact

    async def cleanup(*args, **kwargs):
        order.append("source")
        return await real_cleanup(*args, **kwargs)

    def delete_exact(*args, **kwargs):
        assert not upload_dir.exists()
        order.append("job")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(ctl, "_cleanup_owned_file_source", cleanup)
    monkeypatch.setattr(store, "delete_exact", delete_exact)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_file_transcription", provider_work)

    await ctl.resume_pending_jobs(recover_running=True)

    assert order == ["source", "job"]
    assert not upload_dir.exists()
    assert store.get(job.id) is None
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_missing_parent_adopts_exact_job_delete_that_committed_before_raise(
    monkeypatch,
    tmp_path,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    transcript_id = "missing-parent-delete-commit-before-raise"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    assert store.mark_canceled(job.id, last_error="Stopped by user")
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: None)
    monkeypatch.setattr(web_api.db, "transcript_exists_or_raise", lambda _value: False)
    real_delete = store.delete_exact

    def commit_then_raise(*args, **kwargs):
        assert real_delete(*args, **kwargs)
        raise OSError("commit result unavailable")

    monkeypatch.setattr(store, "delete_exact", commit_then_raise)

    await ctl.resume_pending_jobs(recover_running=True)

    assert store.get(job.id) is None
    assert not upload_dir.exists()
    assert ctl._get_history_record(transcript_id) is None
    assert transcript_id not in ctl._job_ids_by_transcript
    assert transcript_id not in ctl._uncertain_job_commits


@pytest.mark.asyncio
async def test_delete_intent_save_failure_keeps_terminal_projection_for_restart_cleanup(
    monkeypatch,
    tmp_path,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    transcript_id = "delete-intent-save-failure"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id=transcript_id,
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        step="Completed",
        source_url=str(source),
        content="valuable transcript",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    parents = _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._add_to_history(rec)
    ctl._remember_job_id(rec.id, job.id)
    real_save = ctl._save_transcript_to_db_async

    async def fail_deleting(record, **kwargs):
        if record.step == "Deleting":
            return False
        return await real_save(record, **kwargs)

    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", fail_deleting)

    status, _ = await ctl.delete_transcript_record(transcript_id)

    pending = store.get(job.id)
    assert status == "persistence_error"
    assert pending is not None
    assert pending.status == JobStatus.COMPLETED
    assert pending.terminal_projection_pending is True
    assert upload_dir.exists()
    assert parents.get(transcript_id)["step"] == "Completed"

    restarted = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    restarted._downloads_dir = ctl._downloads_dir
    provider_work = AsyncMock()
    monkeypatch.setattr(restarted, "_run_file_transcription", provider_work)

    await restarted.resume_pending_jobs(recover_running=True)

    settled = store.get(job.id)
    assert settled is not None
    assert settled.terminal_projection_pending is False
    assert not upload_dir.exists()
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_completed_projection_with_processing_parent_fails_closed(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = "legacy-completed-processing-parent"
    parent = TranscriptRecord(
        id=transcript_id,
        title="Legacy",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Transcribing...",
        content="valuable partial transcript",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"},
    )
    assert store.mark_completed(job.id)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl, parent)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)

    await ctl.resume_pending_jobs(recover_running=True)

    persisted = parents.get(transcript_id)
    settled = store.get(job.id)
    assert persisted["status"] == "failed"
    assert persisted["content"] == "valuable partial transcript"
    assert "automatic replay was disabled" in persisted["step"]
    assert settled is not None
    assert settled.status == JobStatus.FAILED
    assert settled.terminal_projection_pending is False
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", [web_api.JobType.FILE, web_api.JobType.YOUTUBE])
@pytest.mark.parametrize(
    ("terminal_status", "expected_status", "expected_step"),
    [
        (JobStatus.COMPLETED, "completed", "Completed"),
        (JobStatus.FAILED, "failed", "terminal failure"),
        (JobStatus.CANCELED, "stopped", "terminal cancel"),
    ],
)
async def test_uncertain_terminal_job_projects_terminal_parent_without_provider_work(
    monkeypatch,
    tmp_path,
    job_type,
    terminal_status,
    expected_status,
    expected_step,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    is_file = job_type == web_api.JobType.FILE
    source = "https://www.youtube.com/watch?v=J_RxOz_ddgs"
    if is_file:
        upload_dir = ctl._downloads_dir / "files" / f"terminal-{terminal_status.value}"
        upload_dir.mkdir(parents=True)
        source_path = upload_dir / "sample.wav"
        source_path.write_bytes(b"RIFF....WAVEfmt ")
        source = str(source_path)
    rec = TranscriptRecord(
        id=f"terminal-{job_type.value}-{terminal_status.value}",
        title="Terminal",
        date="Today",
        duration="00:01",
        status="processing",
        type="file" if is_file else "youtube",
        language="auto",
        step="Queued",
        source_url=source,
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=job_type,
        payload={"path" if is_file else "url": source},
    )
    if terminal_status == JobStatus.COMPLETED:
        assert store.mark_completed(job.id)
    elif terminal_status == JobStatus.FAILED:
        assert store.mark_failed(job.id, last_error=expected_step)
    else:
        assert store.mark_canceled(job.id, last_error=expected_step)
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    file_provider = AsyncMock()
    youtube_provider = AsyncMock()
    save_parent = AsyncMock(return_value=True)
    persisted_parent = {
        **rec.to_public(include_content=True),
        "status": expected_status,
        "step": expected_step,
    }
    monkeypatch.setattr(
        web_api.db,
        "get_transcript",
        lambda value: persisted_parent if value == rec.id else None,
    )
    monkeypatch.setattr(ctl, "_run_file_transcription", file_provider)
    monkeypatch.setattr(ctl, "_run_youtube_transcription", youtube_provider)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", save_parent)

    await ctl.resume_pending_jobs()

    adopted = ctl._get_history_record(rec.id)
    assert adopted is not None
    assert adopted.status == expected_status
    assert adopted.step == expected_step
    assert rec.id not in ctl._uncertain_job_commits
    assert rec.id not in ctl._running_tasks
    if is_file:
        assert not Path(source).exists()
    save_parent.assert_not_awaited()
    file_provider.assert_not_awaited()
    youtube_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_completed_job_adopts_authoritative_parent_without_overwriting_content(
    monkeypatch,
    tmp_path,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = "terminal-completed-authoritative-parent"
    stale = TranscriptRecord(
        id=transcript_id,
        title="stale",
        date="Today",
        duration="--:--",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
        content="",
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": stale.source_url},
    )
    assert store.mark_completed(job.id)
    ctl._add_to_history(stale)
    ctl._job_ids_by_transcript[transcript_id] = job.id
    ctl._uncertain_job_commits[transcript_id] = job.id
    authoritative = {
        **stale.to_public(include_content=True),
        "title": "durable title",
        "duration": "01:23",
        "status": "completed",
        "step": "Completed",
        "content": "durable completed content",
        "summary": "durable summary",
        "summaryStatus": "completed",
    }
    save_parent = AsyncMock(return_value=True)
    provider_work = AsyncMock()
    monkeypatch.setattr(web_api.db, "get_transcript", lambda value: authoritative if value == transcript_id else None)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", save_parent)
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)

    await ctl.resume_pending_jobs()

    adopted = ctl._get_history_record(transcript_id)
    assert adopted is not None
    assert adopted.status == "completed"
    assert adopted.title == "durable title"
    assert adopted.content_text() == "durable completed content"
    assert adopted.summary == "durable summary"
    save_parent.assert_not_awaited()
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_terminal_parent_cancels_queued_job_before_cleanup(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "terminal-parent"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="terminal-parent-cancel",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="stopped",
        type="file",
        language="auto",
        step="Stopped by user",
        source_url=str(source),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    real_cleanup = ctl._cleanup_owned_file_source
    cleanup_observed_statuses: list[JobStatus] = []

    async def observe_cleanup(source_path, *, reason, transcript_id=""):
        cleanup_observed_statuses.append(store.get(job.id).status)
        return await real_cleanup(source_path, reason=reason, transcript_id=transcript_id)

    monkeypatch.setattr(ctl, "_cleanup_owned_file_source", observe_cleanup)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_file_transcription", provider_work)

    await ctl.resume_pending_jobs()

    assert cleanup_observed_statuses == [JobStatus.CANCELED]
    assert store.get(job.id).status == JobStatus.CANCELED
    assert not source.exists()
    assert rec.id not in ctl._uncertain_job_commits
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_uncertain_source_cleanup_failure_rearms_and_keeps_parent(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "cleanup-failure"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="absent-source-cleanup-failure",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(source),
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    parent_deletes: list[str] = []
    retry_delays: list[float] = []
    monkeypatch.setattr(ctl, "_cleanup_owned_file_source", AsyncMock(return_value=False))
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: parent_deletes.append(value) or True)
    monkeypatch.setattr(ctl, "_schedule_retry_scan", retry_delays.append)

    await ctl.resume_pending_jobs()

    assert source.exists()
    assert parent_deletes == []
    assert ctl._get_history_record(rec.id) is rec
    assert ctl._job_ids_by_transcript[rec.id] == rec.id
    assert ctl._uncertain_job_commits[rec.id] == rec.id
    assert retry_delays


@pytest.mark.asyncio
async def test_absent_uncertain_parent_delete_failure_rearms_and_keeps_projection(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="absent-parent-delete-failure",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    retry_delays: list[float] = []
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _value: False)
    monkeypatch.setattr(
        web_api.db,
        "transcript_exists_or_raise",
        lambda _value: (_ for _ in ()).throw(OSError("database unavailable")),
    )
    monkeypatch.setattr(ctl, "_schedule_retry_scan", retry_delays.append)

    await ctl.resume_pending_jobs()

    assert ctl._get_history_record(rec.id) is rec
    assert ctl._job_ids_by_transcript[rec.id] == rec.id
    assert ctl._uncertain_job_commits[rec.id] == rec.id
    assert retry_delays


@pytest.mark.asyncio
async def test_absent_uncertain_parent_delete_false_adopts_confirmed_absence(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="absent-parent-delete-committed",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _value: False)
    monkeypatch.setattr(web_api.db, "transcript_exists_or_raise", lambda _value: False)

    await ctl.resume_pending_jobs()

    assert ctl._get_history_record(rec.id) is None
    assert rec.id not in ctl._job_ids_by_transcript
    assert rec.id not in ctl._uncertain_job_commits


@pytest.mark.asyncio
async def test_delete_job_store_failure_returns_persistence_error_and_never_resumes(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "delete-job-store-failure"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    plan = _file_upload_plan(ctl)
    rec = TranscriptRecord(
        id="delete-job-store-failure",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        step="Completed",
        source_url=str(source),
        content="durable transcript",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={
            "path": str(source),
            "executionRoute": ctl._job_execution_route(plan.route),
            "fileUploadPlan": plan.durable_evidence(),
        },
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    parent_deletes: list[str] = []
    provider_work = AsyncMock()
    monkeypatch.setattr(store, "delete_by_transcript_id", lambda _value: (_ for _ in ()).throw(OSError("store down")))
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: parent_deletes.append(value) or True)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_run_file_transcription", provider_work)

    status, deleted = await ctl.delete_transcript_record(rec.id)

    assert status == "persistence_error"
    assert deleted is rec
    assert parent_deletes == []
    assert source.exists()
    assert ctl._get_history_record(rec.id) is rec
    assert store.get(job.id).status == JobStatus.COMPLETED

    await ctl.resume_pending_jobs()
    assert rec.id not in ctl._running_tasks
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after", ["intent", "job", "source", "parent"])
async def test_delete_orders_job_source_parent_and_survives_each_crash_window(
    monkeypatch,
    tmp_path,
    crash_after,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    transcript_id = f"delete-crash-{crash_after}"
    downloads_dir = tmp_path / "downloads"
    upload_dir = downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    assert store.mark_canceled(job.id, last_error="Deleting")
    if crash_after in {"job", "source", "parent"}:
        assert store.delete_by_transcript_id(transcript_id) == 1
    if crash_after in {"source", "parent"}:
        source.unlink()
        upload_dir.rmdir()
    parent_present = crash_after != "parent"
    ordinary_id = f"ordinary-completed-{crash_after}"
    events: list[str] = []
    parent_deletes: list[str] = []
    real_delete_jobs = store.delete_by_transcript_id

    def metadata_page(*, transcript_type, offset, limit, include_incomplete, exclude_ids=()):
        assert include_incomplete is True
        items = []
        if transcript_type == "file":
            if parent_present:
                items.append(
                    {
                        "id": transcript_id,
                        "title": "sample.wav",
                        "status": "stopped",
                        "type": "file",
                        "step": "Deleting",
                        "sourceUrl": str(source),
                    }
                )
            items.append(
                {
                    "id": ordinary_id,
                    "title": "keep.txt",
                    "status": "completed",
                    "type": "file",
                    "step": "Completed",
                    "sourceUrl": "",
                }
            )
        return {"items": items, "hasMore": False, "total": len(items)}

    def delete_jobs(value):
        events.append("job")
        return real_delete_jobs(value)

    def delete_parent(value):
        nonlocal parent_present
        events.append("parent")
        parent_deletes.append(value)
        if value != transcript_id or not parent_present:
            return False
        parent_present = False
        return True

    def get_parent(value):
        if value != transcript_id or not parent_present:
            return None
        return {
            "id": transcript_id,
            "title": "sample.wav",
            "status": "stopped",
            "type": "file",
            "step": "Deleting",
            "sourceUrl": str(source),
        }

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", metadata_page)
    monkeypatch.setattr(web_api.db, "get_transcript", get_parent)
    monkeypatch.setattr(web_api.db, "transcript_exists_or_raise", lambda _value: parent_present)
    monkeypatch.setattr(web_api.db, "delete_transcript", delete_parent)
    monkeypatch.setattr(store, "delete_by_transcript_id", delete_jobs)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = downloads_dir
    real_cleanup_phase = ctl._cleanup_terminal_file_source

    async def cleanup_phase(rec, *, reason):
        events.append("source")
        return await real_cleanup_phase(rec, reason=reason)

    monkeypatch.setattr(ctl, "_cleanup_terminal_file_source", cleanup_phase)
    file_provider = AsyncMock()
    monkeypatch.setattr(ctl, "_run_file_transcription", file_provider)

    await ctl.resume_pending_jobs(recover_running=True)

    if crash_after == "parent":
        assert events == []
    else:
        assert events == ["job", "source", "parent"]
    assert store.get_by_transcript_id(transcript_id) is None
    assert not source.exists()
    assert parent_present is False
    assert ordinary_id not in parent_deletes
    assert transcript_id not in ctl._startup_orphan_admissions
    assert transcript_id not in ctl._running_tasks
    file_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_serializes_exact_job_commit_with_resume_scan(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="delete-resume-serialized",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="auto",
        step="Completed",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
        content="done",
    )
    store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    exact_read_entered = threading.Event()
    release_exact_read = threading.Event()
    pending_scanned = threading.Event()
    real_get_by_transcript_id = store.get_by_transcript_id
    real_list_pending = store.list_pending

    def blocked_exact_read(value):
        exact_read_entered.set()
        assert release_exact_read.wait(timeout=5)
        return real_get_by_transcript_id(value)

    def observed_list_pending(*, limit):
        pending_scanned.set()
        return real_list_pending(limit=limit)

    monkeypatch.setattr(store, "get_by_transcript_id", blocked_exact_read)
    monkeypatch.setattr(store, "list_pending", observed_list_pending)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _value: True)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_youtube_transcription", provider_work)

    delete_task = asyncio.create_task(ctl.delete_transcript_record(rec.id))
    assert await asyncio.to_thread(exact_read_entered.wait, 5)
    resume_task = asyncio.create_task(ctl.resume_pending_jobs())
    await asyncio.sleep(0.05)

    assert not pending_scanned.is_set()
    release_exact_read.set()
    assert (await delete_task)[0] == "deleted"
    await resume_task
    provider_work.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_job_cas_observes_repeated_cancellation_before_source_cleanup(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "terminal-cas-cancel"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="terminal-cas-cancel",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(source),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    cas_started = threading.Event()
    release_cas = threading.Event()
    real_mark_terminal = store.mark_terminal_projection_pending

    def blocked_mark_terminal(job_id, *, status, last_error=""):
        cas_started.set()
        assert release_cas.wait(timeout=5)
        return real_mark_terminal(job_id, status=status, last_error=last_error)

    monkeypatch.setattr(store, "mark_terminal_projection_pending", blocked_mark_terminal)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    task = asyncio.create_task(ctl.cancel_transcript(rec.id))
    assert await asyncio.to_thread(cas_started.wait, 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert source.exists()
    release_cas.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get(job.id).status == JobStatus.CANCELED
    assert not source.exists()
    assert rec.id not in ctl._uncertain_job_commits


@pytest.mark.asyncio
async def test_delete_job_row_observes_repeated_cancellation_before_cleanup(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    upload_dir = ctl._downloads_dir / "files" / "delete-row-cancel"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="delete-row-cancel",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="completed",
        type="file",
        language="auto",
        step="Completed",
        source_url=str(source),
        content="done",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    delete_started = threading.Event()
    release_delete = threading.Event()
    real_delete = store.delete_by_transcript_id
    parent_deletes: list[str] = []

    def blocked_delete(value):
        delete_started.set()
        assert release_delete.wait(timeout=5)
        return real_delete(value)

    monkeypatch.setattr(store, "delete_by_transcript_id", blocked_delete)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: parent_deletes.append(value) or True)

    task = asyncio.create_task(ctl.delete_transcript_record(rec.id))
    assert await asyncio.to_thread(delete_started.wait, 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert source.exists()
    assert parent_deletes == []
    release_delete.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_by_transcript_id(rec.id) is None
    assert not source.exists()
    assert parent_deletes == [rec.id]
    assert ctl._get_history_record(rec.id) is None


@pytest.mark.asyncio
async def test_delete_intent_write_observes_repeated_cancellation_and_settles_ownership(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="delete-intent-cancel",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="auto",
        step="Completed",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
        content="done",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    intent_started = asyncio.Event()
    release_intent = asyncio.Event()
    save_calls = 0
    job_deletes: list[str] = []
    real_delete = store.delete_by_transcript_id

    async def blocked_intent_save(_record):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            intent_started.set()
            await release_intent.wait()
        return True

    def observed_delete(value):
        job_deletes.append(value)
        return real_delete(value)

    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", blocked_intent_save)
    monkeypatch.setattr(store, "delete_by_transcript_id", observed_delete)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _value: True)

    task = asyncio.create_task(ctl.delete_transcript_record(rec.id))
    await asyncio.wait_for(intent_started.wait(), timeout=5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert job_deletes == []
    release_intent.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert job_deletes == [rec.id]
    assert store.get_by_transcript_id(rec.id) is None
    assert ctl._get_history_record(rec.id) is None
    assert rec.id not in ctl._startup_orphan_admissions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "job_status"),
    [("completed", JobStatus.COMPLETED), ("failed", JobStatus.FAILED)],
)
async def test_file_terminal_mark_outage_recovers_same_runtime_without_provider_replay(
    monkeypatch,
    tmp_path,
    terminal_status,
    job_status,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    ctl._job_retry_base_seconds = 60.0
    transcript_id = f"file-terminal-mark-outage-{terminal_status}"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    source = upload_dir / "sample.wav"
    source.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id=transcript_id,
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(source),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(source)},
    )
    ctl._add_to_history(rec)
    parents = _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    route = ctl._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="auto",
    )
    provider_calls: list[str] = []

    async def run_once(record, _path, *, provider):
        provider_calls.append(provider)
        record.status = terminal_status
        record.step = "Completed" if terminal_status == "completed" else "terminal failure"
        if terminal_status == "completed":
            record.content = "durable content"
        assert await parents.save(record)

    store_available = False
    real_mark = store.mark_terminal_projection_pending

    def flaky_mark(job_id, *, status, last_error=""):
        if not store_available:
            raise OSError("job store unavailable")
        return real_mark(job_id, status=status, last_error=last_error)

    monkeypatch.setattr(store, "mark_terminal_projection_pending", flaky_mark)
    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", AsyncMock(return_value=route))
    monkeypatch.setattr(ctl, "_run_file_transcription", run_once)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    assert ctl._schedule_file_job(rec, source)
    await ctl._running_tasks[rec.id]

    assert store.get(job.id).status == JobStatus.RUNNING
    assert ctl._uncertain_job_commits[rec.id] == job.id
    assert source.exists()
    assert provider_calls == ["soniox"]

    store_available = True
    await ctl.resume_pending_jobs()

    assert store.get(job.id).status == job_status
    assert rec.id not in ctl._uncertain_job_commits
    assert not source.exists()
    assert provider_calls == ["soniox"]
    ctl._retry_scheduler.cancel(cancel_running=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "job_status"),
    [("completed", JobStatus.COMPLETED), ("failed", JobStatus.FAILED)],
)
async def test_youtube_terminal_mark_outage_recovers_without_provider_replay(
    monkeypatch,
    tmp_path,
    terminal_status,
    job_status,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._job_retry_base_seconds = 60.0
    rec = TranscriptRecord(
        id=f"youtube-terminal-mark-outage-{terminal_status}",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    parents = _install_durable_parent_harness(monkeypatch, ctl, rec)
    ctl._remember_job_id(rec.id, job.id)
    route = ctl._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language="auto",
    )
    provider_calls: list[str] = []

    async def run_once(record, *, provider):
        provider_calls.append(provider)
        record.status = terminal_status
        record.step = "Completed" if terminal_status == "completed" else "terminal failure"
        if terminal_status == "completed":
            record.content = "durable content"
        assert await parents.save(record)

    store_available = False
    real_mark = store.mark_terminal_projection_pending

    def flaky_mark(job_id, *, status, last_error=""):
        if not store_available:
            raise OSError("job store unavailable")
        return real_mark(job_id, status=status, last_error=last_error)

    monkeypatch.setattr(store, "mark_terminal_projection_pending", flaky_mark)
    monkeypatch.setattr(ctl, "_load_or_freeze_background_route", AsyncMock(return_value=route))
    monkeypatch.setattr(ctl, "_run_youtube_transcription", run_once)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(ctl, "_broadcast_history_updated", AsyncMock())

    assert ctl._schedule_youtube_job(rec)
    await ctl._running_tasks[rec.id]

    assert store.get(job.id).status == JobStatus.RUNNING
    assert ctl._uncertain_job_commits[rec.id] == job.id
    assert provider_calls == ["soniox"]

    store_available = True
    await ctl.resume_pending_jobs()

    assert store.get(job.id).status == job_status
    assert rec.id not in ctl._uncertain_job_commits
    assert provider_calls == ["soniox"]
    ctl._retry_scheduler.cancel(cancel_running=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_status", "parent_step", "job_status", "mark_name"),
    [
        ("completed", "Completed", JobStatus.COMPLETED, "mark_completed"),
        ("stopped", "Stopped by user", JobStatus.CANCELED, "mark_canceled"),
        ("failed", "terminal failure", JobStatus.FAILED, "mark_failed"),
    ],
)
async def test_startup_provider_outcome_terminal_cas_loss_adopts_exact_winner(
    monkeypatch,
    tmp_path,
    parent_status,
    parent_step,
    job_status,
    mark_name,
):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = f"provider-outcome-cas-loss-{parent_status}"
    job = store.enqueue(
        transcript_id=transcript_id,
        job_id=transcript_id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"},
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)
    parent = TranscriptRecord(
        id=transcript_id,
        title="durable parent",
        date="Today",
        duration="00:01",
        status=parent_status,
        type="youtube",
        language="auto",
        step=parent_step,
        content="authoritative provider outcome content",
    ).to_public(include_content=True)
    real_mark = getattr(store, mark_name)

    def concurrent_terminal_winner(job_id, **kwargs):
        assert real_mark(job_id, **kwargs)
        return False

    save_parent = AsyncMock(return_value=True)
    monkeypatch.setattr(store, mark_name, concurrent_terminal_winner)
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: parent)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", save_parent)

    reconciled = await ctl._reconcile_running_provider_outcomes(
        limit=10,
        eligible_job_ids=frozenset({job.id}),
    )

    assert reconciled == 1
    assert store.get(job.id).status == job_status
    projected = ctl._get_history_record(transcript_id)
    assert projected is not None
    assert projected.status == parent_status
    assert projected.content_text() == "authoritative provider outcome content"
    save_parent.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_resume_sweeps_file_parent_without_any_job(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    transcript_id = "startup-orphan"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    deleted_parents: list[str] = []

    def metadata_page(*, transcript_type, offset, limit, include_incomplete, exclude_ids=()):
        assert offset == 0
        assert limit > 0
        assert include_incomplete is True
        assert exclude_ids == ()
        items = (
            [
                {
                    "id": transcript_id,
                    "title": "sample.wav",
                    "status": "processing",
                    "type": "file",
                    "step": "Queued",
                    "sourceUrl": str(sample_file),
                }
            ]
            if transcript_type == "file"
            else []
        )
        return {"items": items, "hasMore": False, "total": len(items)}

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", metadata_page)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: deleted_parents.append(value) or True)

    await ctl.resume_pending_jobs(recover_running=True)

    assert deleted_parents == [transcript_id]
    assert not upload_dir.exists()


@pytest.mark.asyncio
async def test_uncertain_exact_read_retry_remains_armed_without_queued_jobs(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="uncertain-read-retry",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
        source_url=str(tmp_path / "sample.wav"),
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = rec.id
    ctl._uncertain_job_commits[rec.id] = rec.id
    reads = 0

    def unavailable(_job_id):
        nonlocal reads
        reads += 1
        raise OSError("read unavailable")

    monkeypatch.setattr(store, "get", unavailable)
    ctl._job_retry_base_seconds = 0.1

    await ctl.resume_pending_jobs()

    assert reads == 1
    assert ctl._retry_scheduler.due_monotonic is not None
    deadline = asyncio.get_running_loop().time() + 1.0
    while (reads < 2 or ctl._retry_scheduler.due_monotonic is None) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0)

    assert reads >= 2
    assert ctl._retry_scheduler.due_monotonic is not None
    assert ctl._uncertain_job_commits[rec.id] == rec.id
    ctl._retry_scheduler.cancel(cancel_running=True)


@pytest.mark.asyncio
async def test_uncertain_exact_read_outage_excludes_same_row_from_pending_scan(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="uncertain-read-pending-scan",
        title="YouTube",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://www.youtube.com/watch?v=J_RxOz_ddgs",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": rec.source_url},
    )
    ctl._add_to_history(rec)
    ctl._job_ids_by_transcript[rec.id] = job.id
    ctl._uncertain_job_commits[rec.id] = job.id
    scheduled: list[str] = []
    monkeypatch.setattr(store, "get", lambda _value: (_ for _ in ()).throw(OSError("read unavailable")))
    monkeypatch.setattr(
        ctl,
        "_schedule_youtube_job",
        lambda record, **_kwargs: scheduled.append(record.id) or True,
    )

    await ctl.resume_pending_jobs()

    assert scheduled == []
    assert ctl._uncertain_job_commits[rec.id] == job.id
    assert store.get_by_transcript_id(rec.id).status == JobStatus.QUEUED
    ctl._retry_scheduler.cancel(cancel_running=True)


@pytest.mark.asyncio
async def test_startup_orphan_read_failure_is_retried_until_absence_is_confirmed(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    transcript_id = "startup-orphan-read-outage"
    upload_dir = ctl._downloads_dir / "files" / transcript_id
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    deleted_parents: list[str] = []
    lookups = 0

    def metadata_page(*, transcript_type, offset, limit, include_incomplete, exclude_ids=()):
        items = (
            [
                {
                    "id": transcript_id,
                    "title": "sample.wav",
                    "status": "processing",
                    "type": "file",
                    "step": "Queued",
                    "sourceUrl": str(sample_file),
                }
            ]
            if transcript_type == "file"
            else []
        )
        return {"items": items, "hasMore": False, "total": len(items)}

    def transient_lookup(_transcript_id):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            raise OSError("read unavailable")
        return None

    monkeypatch.setattr(web_api.db, "load_transcript_metadata_page", metadata_page)
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda value: deleted_parents.append(value) or True)
    monkeypatch.setattr(store, "get_by_transcript_id", transient_lookup)

    await ctl.resume_pending_jobs(recover_running=True)
    assert sample_file.exists()
    assert transcript_id in ctl._startup_orphan_admissions
    assert ctl._retry_scheduler.due_monotonic is not None

    await ctl.resume_pending_jobs()

    assert lookups == 2
    assert deleted_parents == [transcript_id]
    assert not upload_dir.exists()
    assert transcript_id not in ctl._startup_orphan_admissions
    ctl._retry_scheduler.cancel(cancel_running=True)


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
async def test_job_runner_losing_running_compare_and_set_exits_without_provider_work(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = TranscriptRecord(
        id="running-cas-lost",
        title="sample.wav",
        date="Today",
        duration="00:01",
        status="processing",
        type="file",
        language="auto",
        source_url=str(file_path),
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.FILE,
        payload={"path": str(file_path)},
    )
    assert store.mark_running(job.id) is True
    ctl._remember_job_id(rec.id, job.id)
    provider_work = AsyncMock()
    monkeypatch.setattr(ctl, "_run_file_transcription", provider_work)

    ctl._schedule_file_job(rec, file_path)
    await ctl._running_tasks[rec.id]

    provider_work.assert_not_awaited()
    assert store.get(job.id).status == JobStatus.RUNNING
    assert rec.status == "processing"


@pytest.mark.asyncio
async def test_file_runner_terminal_lifecycle_failure_retains_owned_upload_for_reconciliation(tmp_path):
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
    assert upload_dir.exists()
    assert ctl._uncertain_job_commits[rec.id] == "missing-job-id"


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
    parents = _install_durable_parent_harness(monkeypatch, ctl)

    async def _fake_run(rec, *, provider):
        rec.status = "completed"
        rec.step = "Completed"
        assert await parents.save(rec)

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock(side_effect=parents.save)),
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
    assert "plannedFallbackRoute" in job.payload
    assert "executionRoute" not in job.payload
    assert rec.processing_started_at
    assert rec.to_public(include_content=False)["processingStartedAt"] == rec.processing_started_at


@pytest.mark.asyncio
async def test_start_youtube_transcription_persists_caption_override(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl)

    async def _fake_run(rec, *, provider):
        rec.status = "completed"
        rec.step = "Completed"
        assert await parents.save(rec)

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock(side_effect=parents.save)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
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
    assert "plannedFallbackRoute" not in job.payload
    assert job.payload["executionRoute"]["provider"]
    assert rec._youtube_prefer_captions is False


@pytest.mark.asyncio
async def test_start_youtube_transcription_rejects_non_youtube_url(tmp_path):
    ctl = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )

    with pytest.raises(ValueError, match="Unsupported YouTube URL"):
        await ctl.start_youtube_transcription({"url": "http://127.0.0.1:8765/api/runtime/support-bundle"})

    assert ctl._history == []
    assert ctl._running_tasks == {}


@pytest.mark.asyncio
async def test_cancel_transcript_marks_background_job_canceled(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl)
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(
            ctl,
            "_save_transcript_to_db_async",
            new=AsyncMock(side_effect=parents.save),
        ) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
        await asyncio.sleep(0)
        task = ctl._running_tasks[rec.id]
        assert await ctl.cancel_transcript(rec.id) is True
        await asyncio.gather(task, return_exceptions=True)

    job_id = ctl._job_ids_by_transcript[rec.id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CANCELED
    assert job.job_type.value == "file"
    assert rec.status == "stopped"
    assert rec.step == "Stopped by user"
    save_mock.assert_not_awaited()
    assert any(call.kwargs.get("reason") == "canceled" for call in broadcast_mock.await_args_list)


@pytest.mark.asyncio
async def test_cancel_transcript_removes_owned_upload_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    parents = _install_durable_parent_harness(monkeypatch, ctl)
    upload_dir = ctl._downloads_dir / "files" / "cancel-owned-upload"
    upload_dir.mkdir(parents=True)
    sample_file = upload_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(
            ctl,
            "_save_transcript_to_db_async",
            new=AsyncMock(side_effect=parents.save),
        ),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
        await asyncio.sleep(0)
        task = ctl._running_tasks[rec.id]
        assert await ctl.cancel_transcript(rec.id) is True
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
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
        await asyncio.sleep(0)
        task = ctl._running_tasks[rec.id]
        assert await ctl.cancel_transcript(rec.id) is True
        await asyncio.gather(task, return_exceptions=True)

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

        assert ctl._history_broadcast_pending_payload == {"reason": "coalesced_multiple_transcripts"}
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
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
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
    persisted_parent: dict[str, object] | None = None

    async def _slow_run(_rec, _path, *, provider):
        run_started.set()
        await asyncio.sleep(10)

    async def _save(record):
        nonlocal persisted_parent
        events.append(f"save:{record.status}")
        persisted_parent = record.to_public(include_content=True)

    async def _transition(record):
        nonlocal persisted_parent
        if persisted_parent is None or persisted_parent.get("status") != "processing":
            return False
        events.append(f"save:{record.status}")
        persisted_parent["status"] = record.status
        persisted_parent["step"] = record.step
        persisted_parent["updatedAt"] = record.updated_at
        return True

    def _save_parent_sync(record):
        nonlocal persisted_parent
        persisted_parent = dict(record) if isinstance(record, dict) else record.to_public(include_content=True)

    def _delete(transcript_id):
        events.append(f"delete:{transcript_id}")
        return True

    monkeypatch.setattr(web_api.db, "delete_transcript", _delete)
    monkeypatch.setattr(web_api.db, "save_transcript", _save_parent_sync)
    monkeypatch.setattr(
        web_api.db,
        "get_transcript",
        lambda _value: persisted_parent,
    )
    monkeypatch.setattr(ctl, "_transition_terminal_parent_to_db_async", _transition)
    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock(side_effect=_save)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
        await asyncio.wait_for(run_started.wait(), timeout=1.0)
        events.clear()

        status, deleted = await ctl.delete_transcript_record(rec.id)

    assert status == "deleted"
    assert deleted is rec
    assert rec.status == "stopped"
    assert ctl._get_history_record(rec.id) is None
    assert rec.id not in ctl._job_ids_by_transcript
    assert events == ["save:stopped", "save:stopped", f"delete:{rec.id}"]
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
async def test_delete_adopts_parent_delete_that_committed_before_false_return(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=JobStore(db_path=tmp_path / "jobs.db"))
    rec = TranscriptRecord(
        id="delete-parent-commit-unknown",
        title="Delete me",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="auto",
        step="Completed",
        content="done",
    )
    ctl._add_to_history(rec)
    parent_present = True

    def commit_then_false(_value):
        nonlocal parent_present
        parent_present = False
        return False

    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api.db, "delete_transcript", commit_then_false)
    monkeypatch.setattr(
        web_api.db,
        "transcript_exists_or_raise",
        lambda _value: parent_present,
    )

    status, deleted = await ctl.delete_transcript_record(rec.id)

    assert status == "deleted"
    assert deleted is rec
    assert ctl._get_history_record(rec.id) is None
    assert rec.id not in ctl._startup_orphan_admissions


@pytest.mark.asyncio
async def test_delete_false_with_strict_parent_read_outage_retains_durable_intent(monkeypatch, tmp_path):
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=JobStore(db_path=tmp_path / "jobs.db"))
    rec = TranscriptRecord(
        id="delete-parent-read-outage",
        title="Keep intent",
        date="Today",
        duration="00:01",
        status="completed",
        type="youtube",
        language="auto",
        step="Completed",
        content="done",
    )
    ctl._add_to_history(rec)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", AsyncMock(return_value=True))
    monkeypatch.setattr(web_api.db, "delete_transcript", lambda _value: False)
    monkeypatch.setattr(
        web_api.db,
        "transcript_exists_or_raise",
        lambda _value: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    status, retained = await ctl.delete_transcript_record(rec.id)

    assert status == "persistence_error"
    assert retained is rec
    assert rec.step == "Deleting"
    assert ctl._get_history_record(rec.id) is rec
    assert ctl._startup_orphan_admissions[rec.id] is rec


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
    assert events == [
        "save-started",
        "save-finished",
        "save-started",
        "save-finished",
        "deleted",
    ]
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

    drain_task = asyncio.create_task(ctl.drain_background_tasks_for_shutdown(timeout_seconds=1.0))
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
    assert calls == [
        {
            "transcript_type": "file",
            "offset": 0,
            "limit": 1,
            "include_incomplete": True,
            "exclude_ids": (active.id,),
        }
    ]


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
        return original_set_retry(job_id, retry_at=retry_at, last_error=last_error)

    monkeypatch.setattr(store, "set_retry", cancel_before_retry)
    authoritative_parent = {
        **rec.to_public(include_content=True),
        "status": "stopped",
        "step": "user canceled",
    }
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: authoritative_parent)
    with patch.object(ctl, "_schedule_retry_scan") as schedule_scan:
        scheduled = await ctl._schedule_retry_if_allowed(rec, TimeoutError("provider timeout"))

    assert scheduled is True
    assert rec.status == "stopped"
    assert rec.step == "user canceled"
    assert rec.content_text() == "partial provider output"
    persisted_job = store.get(job.id)
    assert persisted_job is not None
    assert persisted_job.status == JobStatus.CANCELED
    schedule_scan.assert_not_called()


@pytest.mark.asyncio
async def test_retry_cas_loss_adopts_authoritative_completed_content_without_overwrite(monkeypatch, tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="retry-completed-authoritative-race",
        title="stale title",
        date="Today",
        duration="00:01",
        status="processing",
        type="youtube",
        language="auto",
        step="Transcribing...",
        content="stale partial content",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={"url": "https://www.youtube.com/watch?v=J_RxOz_ddgs"},
    )
    assert store.mark_running(job.id)
    ctl._add_to_history(rec)
    ctl._remember_job_id(rec.id, job.id)
    real_set_retry = store.set_retry

    def complete_before_retry(job_id, *, retry_at, last_error=""):
        assert store.mark_completed(job_id)
        return real_set_retry(job_id, retry_at=retry_at, last_error=last_error)

    authoritative_parent = {
        **rec.to_public(include_content=True),
        "title": "durable title",
        "status": "completed",
        "step": "Completed",
        "content": "authoritative completed content",
        "summary": "authoritative summary",
        "summaryStatus": "completed",
    }
    save_parent = AsyncMock(return_value=True)
    monkeypatch.setattr(store, "set_retry", complete_before_retry)
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: authoritative_parent)
    monkeypatch.setattr(ctl, "_save_transcript_to_db_async", save_parent)

    handled = await ctl._schedule_retry_if_allowed(rec, TimeoutError("provider timeout"))

    assert handled is True
    assert store.get(job.id).status == JobStatus.COMPLETED
    assert rec.status == "completed"
    assert rec.title == "durable title"
    assert rec.content_text() == "authoritative completed content"
    assert rec.summary == "authoritative summary"
    save_parent.assert_not_awaited()


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
        return original_set_retry(job_id, retry_at=retry_at, last_error=last_error)

    monkeypatch.setattr(store, "set_retry", cancel_before_retry)
    authoritative_parent = {
        **rec.to_public(include_content=True),
        "status": "stopped",
        "step": "user canceled",
        "content": "",
    }
    monkeypatch.setattr(web_api.db, "get_transcript", lambda _value: authoritative_parent)
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
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    ctl._downloads_dir = tmp_path / "downloads"
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    rec._youtube_prefer_captions = True
    fallback_route = ctl._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language=rec.language,
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=web_api.JobType.YOUTUBE,
        payload={
            "plannedFallbackRoute": ctl._job_execution_route(fallback_route),
        },
    )
    assert store.mark_running(job.id)
    ctl._remember_job_id(rec.id, job.id)
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch(
            "src.web_api.download_youtube_transcript",
            new=AsyncMock(
                return_value=YouTubeTranscript(
                    text="Caption text without an audio upload.",
                    language="en-orig",
                    is_automatic=True,
                    duration_seconds=64.9,
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
    assert rec.duration == "01:04"
    assert rec._youtube_stt_provider_used == ""
    caption_mock.assert_awaited_once()
    audio_mock.assert_not_awaited()
    pipeline_mock.assert_not_called()
    save_mock.assert_awaited_once()
    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.payload["plannedFallbackRoute"]["provider"] == "soniox"
    assert persisted.payload["executionRoute"]["provider"] == "youtube_captions_auto"
    assert persisted.payload["executionRoute"]["transport"] == "caption_track"
    assert persisted.payload["executedRoute"] == persisted.payload["executionRoute"]


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
async def test_youtube_meta_contributor_404_exposes_actionable_access_error_without_fallback(
    monkeypatch,
    tmp_path,
):
    from src.core.provider_errors import provider_transport_error

    ctl = ScriberWebController(asyncio.get_running_loop())
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)
    openrouter_fallback = AsyncMock(
        return_value="<section><h2>Fallback</h2><p>Must not replace the selected Meta model.</p></section>"
    )

    async def _meta_contributor_not_available(*_args, **_kwargs):
        raise provider_transport_error(
            "meta",
            "summarization",
            status=404,
            response_body='{"error":{"message":"private provider detail"}}',
        )

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "muse-spark-1.2-contributor")
    monkeypatch.setattr(Config, "MODEL_API_KEY", "meta-test-key", raising=False)
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "openrouter-test-key", raising=False)
    monkeypatch.delenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", raising=False)

    with (
        patch(
            "src.summarization._post_meta_chat_completion",
            new=AsyncMock(side_effect=_meta_contributor_not_available),
        ),
        patch("src.summarization._summarize_openrouter", new=openrouter_fallback),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_summary_state_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._finalize_youtube_content(
            rec,
            content="A complete transcript that should remain available when summary access is missing.",
            provider="youtube_captions_auto",
            started_at=time.monotonic(),
            source="captions",
        )

    assert rec.status == "completed"
    assert rec.summary_status == "failed"
    assert rec.summary == ""
    assert rec.summary_error == (
        "Muse Spark 1.2 Contributor is not available for this Meta project. "
        "Choose Muse Spark 1.2 Standard in Settings or request Contributor access "
        "in the Meta dashboard, then try again."
    )
    assert "404" not in rec.summary_error
    assert "private provider detail" not in rec.summary_error
    openrouter_fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_transcription_empty_provider_result_fails_job(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)
    rec.source_url = str(file_path)

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
            side_effect=lambda *_args, **kwargs: _SyntheticPipeline(on_transcription=kwargs["on_transcription"]),
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
async def test_final_file_persistence_failure_retains_owned_upload_until_projection(monkeypatch, tmp_path):
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
            side_effect=lambda *_args, **kwargs: _SyntheticPipeline(on_transcription=kwargs["on_transcription"]),
        ),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_record_provider_failure") as provider_failure,
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert rec.status == "failed"
    assert rec._persistence_failed is True
    assert upload_dir.exists()
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
    rec.source_url = str(file_path)

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="gladia")
        await ctl._settle_terminal_background_job(rec, cleanup_reason="failed")

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
        lambda provider: (
            "Local ONNX transcription is unavailable in this Scriber build." if provider == "onnx_local" else None
        ),
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
            late_download_progress["callback"](SimpleNamespace(status="finished", speed=None, eta=None, percent=100.0))
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
async def test_youtube_attempt_lease_covers_download_and_long_local_diarization(monkeypatch, tmp_path):
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

        def renew_attempt_lease(self, _attempt_id, *, owner, expected_version, ttl_seconds):
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
        store.expires_at = time.monotonic() + web_api._TRANSCRIPT_ARTIFACT_LEASE_TTL_SECONDS
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
    monkeypatch.setattr(web_api, "_TRANSCRIPT_ARTIFACT_LEASE_RETRY_DELAYS_SECONDS", (0.0, 0.001))
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
async def test_youtube_failure_stops_lease_guard_before_attempt_cleanup(monkeypatch, tmp_path):
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
async def test_file_postprocessing_failure_releases_provider_result_lease_immediately(monkeypatch, tmp_path):
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
async def test_file_long_media_passes_duration_and_scaled_outer_timeout(monkeypatch, tmp_path):
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
    monkeypatch.setattr(ctl, "_apply_speaker_diarization_fallback", AsyncMock(return_value=[]))
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
async def test_file_duration_limit_uses_concrete_frozen_route_model_before_pipeline(monkeypatch, tmp_path):
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
async def test_youtube_duration_limit_is_checked_after_real_audio_download(monkeypatch, tmp_path):
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
async def test_transcript_artifact_phases_run_off_event_loop_and_commit_is_observed(monkeypatch, tmp_path):
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
async def test_transcript_artifact_commit_cancellation_waits_for_durable_worker(monkeypatch, tmp_path):
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

    task = asyncio.create_task(to_thread_cancellation_barrier(durable_mutation))
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
async def test_youtube_scheduler_does_not_reclassify_handled_download_failure(monkeypatch, tmp_path):
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
        patch.object(ctl, "_record_provider_failure", wraps=ctl._record_provider_failure) as provider_failure,
    ):
        ctl._schedule_file_job(rec, file_path)
        task = ctl._running_tasks[rec.id]
        await task

    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.status == JobStatus.FAILED
    provider_failure.assert_called_once_with("soniox", "provider failed")

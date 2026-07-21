import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import database
from src.config import Config
from src.data.job_store import (
    PROVIDER_REQUEST_MAY_BE_COMMITTED,
    PROVIDER_REQUEST_NOT_STARTED,
    JobStatus,
    JobStore,
    JobType,
)
from src.data.transcript_artifact_store import AttemptState, StageUnit
from src.transcript_artifacts import FrozenTranscriptionRoute
from src.web_api import ScriberWebController, TranscriptRecord


_DURABLE_TRANSCRIPT_TEXT = "Recovered from the durable provider result."


@pytest.fixture
def isolated_recovery_database(monkeypatch, tmp_path):
    """Keep controller startup and all recovery projections inside the test dir."""

    runtime_dir = tmp_path / "runtime"
    db_path = runtime_dir / "transcripts.db"
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("SCRIBER_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("SCRIBER_DOWNLOADS_DIR", str(runtime_dir / "downloads"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SKIP_LEGACY_DATA_MIGRATION", "1")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setattr(database, "_DB_PATH", db_path)
    database._close_all_connections()
    database.init_database()
    try:
        yield db_path
    finally:
        database._close_all_connections()


def _persist_file_projection(
    *,
    transcript_id: str,
    source_path: Path,
    status: str,
    content: str = "stale projection",
) -> TranscriptRecord:
    record = TranscriptRecord(
        id=transcript_id,
        title="Durable recovery",
        date="Today",
        duration="00:05",
        status=status,
        type="file",
        language="en",
        step="Failed" if status == "failed" else "Transcribing...",
        source_url=str(source_path),
        content=content,
    )
    database.save_transcript(record)
    return record


def _create_fenced_file_job(
    controller: ScriberWebController,
    store: JobStore,
    *,
    transcript_id: str,
    source_path: Path,
    route: FrozenTranscriptionRoute,
):
    job = store.enqueue(
        transcript_id=transcript_id,
        job_type=JobType.FILE,
        payload={
            "path": str(source_path),
            "title": "Durable recovery",
            "language": "en",
            "executionRoute": controller._job_execution_route(route),
        },
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)
    return store.get(job.id)


def _persist_provider_result(
    controller: ScriberWebController,
    *,
    transcript_id: str,
    route: FrozenTranscriptionRoute,
    attempt_id: str,
    workload: str = "file",
):
    artifacts = controller._transcript_artifacts
    attempt = artifacts.create_attempt(
        transcript_id=transcript_id,
        workload=workload,
        attempt_id=attempt_id,
    )
    artifacts.persist_route_snapshot(attempt.id, route.snapshot_draft())
    attempt = artifacts.transition_attempt(
        attempt.id,
        expected_state=AttemptState.QUEUED,
        expected_version=attempt.state_version,
        new_state=AttemptState.RESOLVING_SOURCE,
    )
    attempt = artifacts.transition_attempt(
        attempt.id,
        expected_state=AttemptState.RESOLVING_SOURCE,
        expected_version=attempt.state_version,
        new_state=AttemptState.SOURCE_READY,
    )
    attempt = artifacts.transition_attempt(
        attempt.id,
        expected_state=AttemptState.SOURCE_READY,
        expected_version=attempt.state_version,
        new_state=AttemptState.TRANSCRIBING,
    )
    _stage, attempt = artifacts.persist_stage_result(
        attempt.id,
        expected_version=attempt.state_version,
        transcript_text=_DURABLE_TRANSCRIPT_TEXT,
        units=(
            StageUnit(
                source_track="mix",
                start_ms=0,
                end_ms=5000,
                text=_DURABLE_TRANSCRIPT_TEXT,
            ),
        ),
        evidence={"fixtureKind": "durable_recovery"},
    )
    return attempt


def _commit_provider_result(controller: ScriberWebController, attempt):
    artifacts = controller._transcript_artifacts
    stage = artifacts.get_stage_result(attempt.id)
    assert stage is not None
    attempt = artifacts.transition_attempt(
        attempt.id,
        expected_state=AttemptState.PROVIDER_RESULT_READY,
        expected_version=attempt.state_version,
        new_state=AttemptState.CANONICALIZING,
    )
    attempt = artifacts.transition_attempt(
        attempt.id,
        expected_state=AttemptState.CANONICALIZING,
        expected_version=attempt.state_version,
        new_state=AttemptState.COMMITTING,
    )
    return artifacts.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=attempt.expected_head_generation,
        segments=stage.units,
    )


async def _close_test_controller(controller: ScriberWebController) -> None:
    await controller.drain_background_tasks_for_shutdown(timeout_seconds=0.5)
    controller.shutdown()
    controller.close_persistence_stores()


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


@pytest.mark.asyncio
async def test_startup_recovers_exact_durable_result_with_failed_projection_and_missing_source(
    isolated_recovery_database,
):
    store = JobStore(db_path=isolated_recovery_database)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = "tx-durable-missing-source"
    missing_source = isolated_recovery_database.parent / "deleted-source.wav"
    route = ctl._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="en",
    )
    _persist_file_projection(
        transcript_id=transcript_id,
        source_path=missing_source,
        status="failed",
    )
    job = _create_fenced_file_job(
        ctl,
        store,
        transcript_id=transcript_id,
        source_path=missing_source,
        route=route,
    )
    assert job is not None
    attempt = _persist_provider_result(
        ctl,
        transcript_id=transcript_id,
        route=route,
        attempt_id="paid-result-missing-source",
    )
    assert store.mark_provider_result_durable(job.id, attempt_id=attempt.id)
    ctl._startup_running_job_ids = frozenset({job.id})

    prepare_audio = MagicMock(
        side_effect=AssertionError("durable recovery must not inspect the missing source")
    )
    try:
        with (
            patch("src.web_api.prepare_provider_audio_file", new=prepare_audio),
            patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        ):
            resumed = await ctl.resume_pending_jobs(limit=10, recover_running=True)
            assert resumed == 1
            task = ctl._running_tasks.get(transcript_id)
            assert task is not None
            await asyncio.gather(task)

        persisted = database.get_transcript(transcript_id)
        recovered_job = store.get(job.id)
        assert persisted is not None
        assert persisted["status"] == "completed"
        assert _DURABLE_TRANSCRIPT_TEXT in persisted["content"]
        assert recovered_job is not None
        assert recovered_job.status == JobStatus.COMPLETED
        assert recovered_job.provider_result_attempt_id == attempt.id
        assert (
            ctl._transcript_artifacts.require_attempt(attempt.id).state
            == AttemptState.COMPLETED
        )
        prepare_audio.assert_not_called()
    finally:
        await _close_test_controller(ctl)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_status", ["processing", "failed"])
async def test_startup_reprojects_completed_artifact_instead_of_only_completing_job(
    isolated_recovery_database,
    stale_status,
):
    store = JobStore(db_path=isolated_recovery_database)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = f"tx-completed-artifact-{stale_status}"
    missing_source = isolated_recovery_database.parent / f"deleted-{stale_status}.wav"
    route = ctl._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="en",
    )
    _persist_file_projection(
        transcript_id=transcript_id,
        source_path=missing_source,
        status="processing",
    )
    job = _create_fenced_file_job(
        ctl,
        store,
        transcript_id=transcript_id,
        source_path=missing_source,
        route=route,
    )
    assert job is not None
    attempt = _persist_provider_result(
        ctl,
        transcript_id=transcript_id,
        route=route,
        attempt_id=f"paid-result-completed-{stale_status}",
    )
    committed = _commit_provider_result(ctl, attempt)
    assert committed.attempt.state == AttemptState.COMPLETED
    assert store.mark_provider_result_durable(job.id, attempt_id=attempt.id)
    ctl._startup_running_job_ids = frozenset({job.id})
    _persist_file_projection(
        transcript_id=transcript_id,
        source_path=missing_source,
        status=stale_status,
        content="stale projection written after canonical commit",
    )

    prepare_audio = MagicMock(
        side_effect=AssertionError("completed artifact recovery must remain source-free")
    )
    try:
        with (
            patch("src.web_api.prepare_provider_audio_file", new=prepare_audio),
            patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        ):
            resumed = await ctl.resume_pending_jobs(limit=10, recover_running=True)
            assert resumed == 1
            task = ctl._running_tasks.get(transcript_id)
            assert task is not None
            await asyncio.gather(task)

        persisted = database.get_transcript(transcript_id)
        recovered_job = store.get(job.id)
        assert persisted is not None
        assert persisted["status"] == "completed"
        assert _DURABLE_TRANSCRIPT_TEXT in persisted["content"]
        assert "stale projection" not in persisted["content"]
        assert recovered_job is not None
        assert recovered_job.status == JobStatus.COMPLETED
        assert (
            recovered_job.payload["executedRoute"]
            == recovered_job.payload["executionRoute"]
        )
        assert recovered_job.provider_result_attempt_id == attempt.id
        prepare_audio.assert_not_called()
    finally:
        await _close_test_controller(ctl)


@pytest.mark.asyncio
async def test_startup_provider_outcome_reconciliation_paginates_beyond_25(
    isolated_recovery_database,
):
    store = JobStore(db_path=isolated_recovery_database)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    jobs = []
    for index in range(31):
        job = store.enqueue(
            transcript_id=f"tx-unknown-provider-outcome-{index:02d}",
            job_type=JobType.FILE,
            payload={"path": str(isolated_recovery_database.parent / f"{index}.wav")},
        )
        assert store.mark_running(job.id)
        assert store.mark_provider_request_may_be_committed(job.id)
        jobs.append(job)

    async def fail_without_provider_replay(record, message):
        assert "automatic replay was disabled" in message
        job_id = ctl._job_ids_by_transcript[record.id]
        assert store.mark_failed(job_id, last_error=message)

    try:
        with patch.object(
            ctl,
            "_fail_resumed_job",
            new=AsyncMock(side_effect=fail_without_provider_replay),
        ) as fail_mock:
            reconciled = await ctl._reconcile_running_provider_outcomes(
                limit=25,
                eligible_job_ids=frozenset(job.id for job in jobs),
            )

        assert reconciled == 31
        assert fail_mock.await_count == 31
        assert store.list_running_provider_outcomes(limit=100) == []
        assert all(store.get(job.id).status == JobStatus.FAILED for job in jobs)
    finally:
        await _close_test_controller(ctl)


@pytest.mark.asyncio
async def test_startup_recovery_does_not_mutate_job_started_after_running_snapshot(
    isolated_recovery_database,
):
    store = JobStore(db_path=isolated_recovery_database)
    stale_job = store.enqueue(
        transcript_id="tx-stale-at-startup",
        job_type=JobType.FILE,
        payload={
            "path": str(isolated_recovery_database.parent / "stale.wav"),
            "duration": "00:05",
        },
    )
    assert store.mark_running(stale_job.id)

    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    assert ctl._startup_running_job_ids == frozenset({stale_job.id})

    new_job = store.enqueue(
        transcript_id="tx-started-after-snapshot",
        job_type=JobType.FILE,
        payload={
            "path": str(isolated_recovery_database.parent / "new.wav"),
            "duration": "00:05",
        },
    )
    assert store.mark_running(new_job.id)
    assert store.mark_provider_request_may_be_committed(new_job.id)

    try:
        with (
            patch.object(store, "list_pending", return_value=[]),
            patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        ):
            resumed = await ctl.resume_pending_jobs(limit=25, recover_running=True)

        recovered_stale = store.get(stale_job.id)
        untouched_new = store.get(new_job.id)
        assert resumed == 0
        assert recovered_stale is not None
        assert recovered_stale.status == JobStatus.QUEUED
        assert recovered_stale.provider_request_state == PROVIDER_REQUEST_NOT_STARTED
        assert untouched_new is not None
        assert untouched_new.status == JobStatus.RUNNING
        assert (
            untouched_new.provider_request_state
            == PROVIDER_REQUEST_MAY_BE_COMMITTED
        )
        assert ctl._startup_running_job_ids == frozenset()
    finally:
        await _close_test_controller(ctl)


@pytest.mark.asyncio
async def test_startup_recovers_youtube_durable_result_without_url_or_download(
    isolated_recovery_database,
):
    store = JobStore(db_path=isolated_recovery_database)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = "tx-youtube-durable-without-url"
    route = ctl._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language="en",
    )
    database.save_transcript(
        TranscriptRecord(
            id=transcript_id,
            title="YouTube durable recovery",
            date="Today",
            duration="00:05",
            status="failed",
            type="youtube",
            language="en",
            step="Failed",
            source_url="",
            content="stale YouTube projection",
        )
    )
    job = store.enqueue(
        transcript_id=transcript_id,
        job_type=JobType.YOUTUBE,
        payload={
            "title": "YouTube durable recovery",
            "duration": "00:05",
            "language": "en",
            "preferCaptions": True,
            "executionRoute": ctl._job_execution_route(route),
        },
    )
    assert store.mark_running(job.id)
    assert store.mark_provider_request_may_be_committed(job.id)
    attempt = _persist_provider_result(
        ctl,
        transcript_id=transcript_id,
        route=route,
        attempt_id="paid-youtube-result-without-url",
        workload="youtube",
    )
    assert store.mark_provider_result_durable(job.id, attempt_id=attempt.id)
    ctl._startup_running_job_ids = frozenset({job.id})

    forbidden_source_io = AssertionError(
        "durable YouTube recovery must not require captions, URL, or audio download"
    )
    download_audio = MagicMock(side_effect=forbidden_source_io)
    download_captions = MagicMock(side_effect=forbidden_source_io)
    prepare_audio = MagicMock(side_effect=forbidden_source_io)
    validate_provider = MagicMock(side_effect=forbidden_source_io)
    try:
        with (
            patch("src.web_api.download_youtube_audio", new=download_audio),
            patch("src.web_api.download_youtube_transcript", new=download_captions),
            patch("src.web_api.prepare_provider_audio_file", new=prepare_audio),
            patch("src.web_api._validate_provider_ready", new=validate_provider),
            patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        ):
            resumed = await ctl.resume_pending_jobs(limit=10, recover_running=True)
            assert resumed == 1
            task = ctl._running_tasks.get(transcript_id)
            assert task is not None
            await asyncio.gather(task)

        persisted = database.get_transcript(transcript_id)
        recovered_job = store.get(job.id)
        assert persisted is not None
        assert persisted["status"] == "completed"
        assert _DURABLE_TRANSCRIPT_TEXT in persisted["content"]
        assert recovered_job is not None
        assert recovered_job.status == JobStatus.COMPLETED
        assert recovered_job.provider_result_attempt_id == attempt.id
        assert (
            ctl._transcript_artifacts.require_attempt(attempt.id).state
            == AttemptState.COMPLETED
        )
        download_audio.assert_not_called()
        download_captions.assert_not_called()
        prepare_audio.assert_not_called()
        validate_provider.assert_not_called()
    finally:
        await _close_test_controller(ctl)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_kind", ["mismatched", "ambiguous"])
async def test_startup_unbound_provider_results_fail_closed(
    isolated_recovery_database,
    candidate_kind,
):
    store = JobStore(db_path=isolated_recovery_database)
    ctl = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    transcript_id = f"tx-unbound-{candidate_kind}"
    missing_source = isolated_recovery_database.parent / f"deleted-{candidate_kind}.wav"
    job_route = ctl._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="en",
    )
    _persist_file_projection(
        transcript_id=transcript_id,
        source_path=missing_source,
        status="processing",
    )
    job = _create_fenced_file_job(
        ctl,
        store,
        transcript_id=transcript_id,
        source_path=missing_source,
        route=job_route,
    )
    assert job is not None

    candidate_routes = (
        [
            ctl._freeze_background_provider_route(
                workload="file",
                provider="soniox",
                language="de",
            )
        ]
        if candidate_kind == "mismatched"
        else [job_route, job_route]
    )
    attempt_ids = []
    for index, candidate_route in enumerate(candidate_routes):
        attempt = _persist_provider_result(
            ctl,
            transcript_id=transcript_id,
            route=candidate_route,
            attempt_id=f"unbound-{candidate_kind}-{index}",
        )
        attempt_ids.append(attempt.id)
    ctl._startup_running_job_ids = frozenset({job.id})

    try:
        with (
            patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
            patch.object(ctl, "_schedule_file_job") as schedule_file,
        ):
            resumed = await ctl.resume_pending_jobs(limit=10, recover_running=True)

        persisted_job = store.get(job.id)
        assert resumed == 0
        assert persisted_job is not None
        assert persisted_job.status == JobStatus.FAILED
        assert persisted_job.provider_result_attempt_id == ""
        failed_projection = database.get_transcript(transcript_id)
        assert failed_projection is not None
        assert "automatic replay was disabled" in failed_projection["content"]
        assert all(
            ctl._transcript_artifacts.require_attempt(attempt_id).state
            == AttemptState.PROVIDER_RESULT_READY
            for attempt_id in attempt_ids
        )
        schedule_file.assert_not_called()
    finally:
        await _close_test_controller(ctl)

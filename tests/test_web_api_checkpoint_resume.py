from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src import database
from src.config import Config
from src.data.job_store import JobStatus, JobStore, JobType
from src.web_api import ScriberWebController, TranscriptRecord


@pytest.fixture
def isolated_database(monkeypatch, tmp_path):
    database._close_all_connections()
    path = tmp_path / "transcripts.db"
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DATABASE_PATH", str(path))
    monkeypatch.setenv("SCRIBER_DOWNLOADS_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("SCRIBER_SKIP_LEGACY_DATA_MIGRATION", "1")
    monkeypatch.setattr(database, "_DB_PATH", path)
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    database.init_database()
    yield path
    database._close_all_connections()


async def failed_checkpoint_job(path, *, unknown=False):
    store = JobStore(path)
    controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    source = controller._downloads_dir / "files" / "checkpoint-test" / "source.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"retained original audio")
    rec = TranscriptRecord(
        id="checkpoint-test",
        title="Resume test",
        date="Today",
        duration="00:10",
        status="failed",
        type="file",
        language="en",
        source_url=str(source),
        content="old failure message",
        step="Failed",
    )
    route = controller._freeze_background_provider_route(
        workload="file", provider="openrouter_stt", language="en", model="microsoft/mai-2-transcribe"
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=JobType.FILE,
        payload={"path": str(source), "executionRoute": controller._job_execution_route(route)},
    )
    controller._remember_job_id(rec.id, job.id)
    assert store.mark_running(job.id)
    checkpoint = await controller._file_transcription_checkpoint(rec, source, route)
    await checkpoint.bind(manifest={"parts": [{"index": 1}, {"index": 2}]}, request_shape={"model": route.model})
    await checkpoint.mark_started(1)
    if not unknown:
        await checkpoint.save_success(1, {"text": "paid part"})
    assert store.mark_failed(job.id, last_error="interrupted")
    database.save_transcript(rec)
    return controller, store, job, rec, source, route


@pytest.mark.asyncio
async def test_explicit_resume_reuses_identity_and_rejects_double_queue(isolated_database):
    controller, store, job, rec, source, route = await failed_checkpoint_job(isolated_database)
    with (
        patch.object(controller, "_schedule_retry_scan"),
        patch.object(controller, "_load_or_freeze_background_route", new=AsyncMock(return_value=route)),
        patch.object(controller, "resume_pending_jobs", new=AsyncMock(return_value=0)),
        patch.object(controller, "_broadcast_history_updated", new=AsyncMock()),
    ):
        detail = await controller.get_transcript(rec.id)
        assert detail["resumeAvailable"] is True
        assert await controller.resume_checkpointed_transcription(rec.id)
        assert not await controller.resume_checkpointed_transcription(rec.id)
    resumed_job = store.get(job.id)
    assert resumed_job.status == JobStatus.QUEUED
    assert resumed_job.payload["checkpointResumeRequested"] is True
    assert database.get_transcript(rec.id)["status"] == "processing"
    assert database.get_transcript(rec.id)["content"] == ""
    assert source.read_bytes() == b"retained original audio"
    store.close()


@pytest.mark.asyncio
async def test_unknown_request_cannot_resume_from_public_controller(isolated_database):
    controller, store, _, rec, _, _ = await failed_checkpoint_job(isolated_database, unknown=True)
    detail = await controller.get_transcript(rec.id)
    assert detail["resumeAvailable"] is False
    with pytest.raises(ValueError, match="unresolved"):
        await controller.resume_checkpointed_transcription(rec.id)
    store.close()


@pytest.mark.asyncio
async def test_restart_adopts_explicit_queue_even_if_parent_save_was_interrupted(isolated_database):
    controller, store, job, rec, source, route = await failed_checkpoint_job(isolated_database)
    from src.data.transcription_part_store import source_sha256

    assert store.queue_checkpoint_resume(job.id, source_digest=source_sha256(source), expected_attempt=1)
    assert database.get_transcript(rec.id)["status"] == "failed"
    schedule = Mock(return_value=True)
    with (
        patch.object(controller, "_schedule_file_job", schedule),
        patch.object(controller, "_schedule_retry_scan"),
        patch.object(controller, "_broadcast_history_updated", new=AsyncMock()),
    ):
        assert await controller.resume_pending_jobs() == 1
    resumed = schedule.call_args.args[0]
    assert resumed.id == rec.id
    assert resumed.status == "processing"
    assert resumed.content == ""
    assert store.has_transcription_checkpoint(job.id)
    store.close()


@pytest.mark.asyncio
async def test_terminal_settlement_retains_source_until_explicit_delete(isolated_database):
    controller, store, job, rec, source, _ = await failed_checkpoint_job(isolated_database)
    controller._add_to_history(rec)
    await controller._settle_terminal_background_job(rec, cleanup_reason="failed")
    assert source.exists()
    assert store.get(job.id).terminal_projection_pending is False
    status, _ = await controller.delete_transcript_record(rec.id)
    assert status == "deleted"
    assert not source.exists()
    assert not store.has_transcription_checkpoint(job.id)
    store.close()


@pytest.mark.asyncio
async def test_legacy_and_explicit_response_contracts_survive_model_upgrade(isolated_database):
    controller, store, _, rec, _, route = await failed_checkpoint_job(isolated_database)
    old = controller._restore_response_contract(route, {})
    assert old.response_shape == "final_text"
    assert old.timestamp_mode == "estimated"
    assert old.parser_version == "2"
    explicit = controller._job_execution_route(replace(route, parser_version="old-parser", timestamp_mode="estimated"))
    restored = controller._restore_response_contract(route, explicit)
    assert restored.parser_version == "old-parser"
    assert restored.timestamp_mode == "estimated"
    store.close()


@pytest.mark.asyncio
async def test_delete_completes_locally_while_cloud_cleanup_retries_durably(isolated_database, monkeypatch):
    controller, store, job, rec, source, _ = await failed_checkpoint_job(isolated_database)
    with sqlite3.connect(isolated_database) as conn:
        conn.executemany(
            "INSERT INTO soniox_resource_cleanup(job_id,region,resource_kind,resource_id) VALUES(?,?,?,?)",
            [(job.id, "eu", kind, f"known-{kind}") for kind in ("file", "transcription")],
        )
    state = {"status": 503}
    requests = []

    @asynccontextmanager
    async def delete(url, **kwargs):
        requests.append(url)
        yield SimpleNamespace(status=state["status"])

    controller._provider_http_transport = SimpleNamespace(
        session_view=AsyncMock(return_value=SimpleNamespace(delete=delete))
    )
    monkeypatch.setattr(Config, "get_api_key", lambda _provider: "test-key")
    controller._add_to_history(rec)
    with patch.object(controller, "_schedule_retry_scan") as retry:
        status, _ = await controller.delete_transcript_record(rec.id)
        await controller._wait_for_detached_tasks()
        assert status == "deleted"
        assert not source.exists()
        assert len(store.list_soniox_cleanup()) == 2
        retry.assert_called_with(60.0)
        assert len(requests) == 1
        assert requests[0].endswith("/transcriptions/known-transcription")
        state["status"] = 204
        assert await controller.resume_pending_jobs() == 0
        await controller._wait_for_detached_tasks()
    assert store.list_soniox_cleanup() == []
    assert requests[-1].endswith("/files/known-file")
    store.close()


@pytest.mark.asyncio
async def test_youtube_first_audio_refinement_preserves_frozen_response_contract(isolated_database, monkeypatch):
    store = JobStore(isolated_database)
    controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = TranscriptRecord(
        id="youtube-legacy",
        title="Legacy response",
        date="Today",
        duration="00:10",
        status="processing",
        type="youtube",
        language="en",
        source_url="https://youtube.com/watch?v=unused",
        _youtube_prefer_captions=False,
    )
    route = replace(
        controller._freeze_background_provider_route(
            workload="youtube", provider="openrouter_stt", language="en", model="microsoft/mai-transcribe-2"
        ),
        parser_version="2",
        response_shape="final_text",
        timestamp_mode="estimated",
    )
    monkeypatch.setattr(controller, "_ensure_artifact_transcript_row", AsyncMock())
    monkeypatch.setattr(controller, "_recover_bound_provider_result", AsyncMock(return_value=None))
    monkeypatch.setattr("src.web_api._validate_provider_ready", lambda _: None)
    stop = AsyncMock(side_effect=RuntimeError("stop-before-provider"))
    monkeypatch.setattr(controller, "_begin_transcript_artifact_async", stop)
    with pytest.raises(RuntimeError, match="stop-before-provider"):
        await controller._run_youtube_transcription(rec, provider="openrouter_stt", frozen_route=route)
    refined = stop.call_args.args[1]
    assert (refined.parser_version, refined.response_shape, refined.timestamp_mode) == (
        route.parser_version,
        route.response_shape,
        route.timestamp_mode,
    )
    store.close()

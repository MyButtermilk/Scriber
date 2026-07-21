from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.audio_prepare import PreparedProviderAudio
from src.config import Config
from src.core.provider_audio_formats import (
    AudioInputFormat,
    AudioSelectionMode,
    SPEECHMATICS_BATCH_DEFAULT_BASE_URL,
    SPEECHMATICS_REALTIME_DEFAULT_BASE_URL,
)
from src.data.job_store import JobStore, JobType
from src.runtime.provider_http import ProviderRequestAcceptanceUnknown
from src.transcript_artifacts import freeze_provider_route
from src.web_api import (
    ScriberWebController,
    TranscriptPersistenceError,
    TranscriptRecord,
)
from src.youtube_download import YouTubeDownloadError


def _record(*, transcript_id: str, transcript_type: str, source: Path | str) -> TranscriptRecord:
    return TranscriptRecord(
        id=transcript_id,
        title="Provider audio",
        date="Today",
        duration="00:01",
        status="processing",
        type=transcript_type,
        language="en",
        step="Queued",
        source_url=str(source),
        _youtube_prefer_captions=False if transcript_type == "youtube" else None,
    )


@pytest.mark.asyncio
async def test_file_job_persists_exact_prepared_route_before_artifact(tmp_path: Path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    source = tmp_path / "source.webm"
    source.write_bytes(b"opus-bytes")
    rec = _record(transcript_id="exact-file-route", transcript_type="file", source=source)
    route = controller._freeze_background_provider_route(
        workload="file",
        provider="soniox",
        language="en",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=JobType.FILE,
        payload={
            "path": str(source),
            "executionRoute": controller._job_execution_route(route),
        },
    )
    controller._remember_job_id(rec.id, job.id)
    prepared = PreparedProviderAudio(
        path=source,
        source_format=AudioInputFormat.WEBM_OPUS,
        selected_format=AudioInputFormat.WEBM_OPUS,
        selection_mode=AudioSelectionMode.ORIGINAL_PASSTHROUGH,
        implementation="original_passthrough",
        content_type="audio/webm; codecs=opus",
        capability_id=route.provider_audio_capability_id,
        capability_revision=route.provider_audio_capability_revision,
        byte_length=source.stat().st_size,
        generated=False,
    )

    @asynccontextmanager
    async def prepared_context(*_args, **_kwargs):
        yield prepared

    inner = AsyncMock(return_value="durable transcript")
    with (
        patch("src.web_api.prepare_provider_audio_file", new=prepared_context),
        patch.object(
            controller,
            "_transcribe_file_route_to_canonical_artifact",
            new=inner,
        ),
    ):
        result = await controller._transcribe_file_to_canonical_artifact(
            rec,
            source,
            provider="soniox",
            frozen_route=route,
        )

    assert result == "durable transcript"
    persisted = store.get(job.id)
    assert persisted is not None
    execution = persisted.payload["executionRoute"]
    assert execution["provider"] == "soniox"
    assert execution["model"] == route.model
    assert execution["audioInputFormat"] == "webm_opus"
    assert execution["audioInputFormatVerified"] is True
    assert execution["audioSelectionMode"] == "original_passthrough"
    assert execution["audioPreparationImplementation"] == "original_passthrough"
    called_route = inner.await_args.kwargs["route"]
    assert called_route.audio_input_format == AudioInputFormat.WEBM_OPUS
    assert inner.await_args.kwargs["prepared_audio"] is prepared


@pytest.mark.asyncio
async def test_custom_direct_provider_model_fails_closed_before_preparation(tmp_path: Path):
    controller = ScriberWebController(asyncio.get_running_loop())
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    rec = _record(transcript_id="custom-model", transcript_type="file", source=source)
    route = freeze_provider_route(
        workload="file",
        provider="soniox",
        model="unverified-custom-model",
        language="en",
    )

    with patch("src.web_api.prepare_provider_audio_file") as prepare:
        with pytest.raises(ValueError, match="no verified batch audio capability"):
            await controller._transcribe_file_to_canonical_artifact(
                rec,
                source,
                provider="soniox",
                frozen_route=route,
            )
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_custom_speechmatics_endpoint_fails_before_probe_or_http(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "SCRIBER_SPEECHMATICS_BATCH_BASE_URL",
        "https://private.invalid/speechmatics/v2",
    )
    controller = ScriberWebController(asyncio.get_running_loop())
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    rec = _record(
        transcript_id="custom-speechmatics-endpoint",
        transcript_type="file",
        source=source,
    )
    route = controller._freeze_background_provider_route(
        workload="file",
        provider="speechmatics_async",
        language="en",
    )

    assert not route.provider_audio_capability_id
    with patch("src.web_api.prepare_provider_audio_file") as prepare:
        with pytest.raises(ValueError, match="no verified batch audio capability"):
            await controller._transcribe_file_to_canonical_artifact(
                rec,
                source,
                provider="speechmatics_async",
                frozen_route=route,
            )
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_speechmatics_realtime_and_batch_freeze_distinct_endpoints(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SPEECHMATICS_RT_URL", raising=False)
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_BATCH_BASE_URL", raising=False)
    controller = ScriberWebController(asyncio.get_running_loop())

    realtime = controller._freeze_background_provider_route(
        workload="file",
        provider="speechmatics",
        language="en",
    )
    batch = controller._freeze_background_provider_route(
        workload="file",
        provider="speechmatics_async",
        language="en",
    )

    assert realtime.provider_endpoint_sha256 == hashlib.sha256(
        SPEECHMATICS_REALTIME_DEFAULT_BASE_URL.encode("utf-8")
    ).hexdigest()
    assert batch.provider_endpoint_sha256 == hashlib.sha256(
        SPEECHMATICS_BATCH_DEFAULT_BASE_URL.encode("utf-8")
    ).hexdigest()
    assert realtime.provider_endpoint_sha256 != batch.provider_endpoint_sha256
    assert realtime.provider_audio_capability_id == (
        "speechmatics:realtime_v2:enhanced"
    )
    assert batch.provider_audio_capability_id == (
        "speechmatics_async:batch_v2:enhanced"
    )


@pytest.mark.asyncio
async def test_request_acceptance_unknown_is_never_automatically_retried(tmp_path: Path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    rec = _record(
        transcript_id="unknown-acceptance",
        transcript_type="file",
        source=tmp_path / "source.wav",
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=JobType.FILE,
        payload={"path": rec.source_url},
    )
    assert store.mark_running(job.id)
    controller._remember_job_id(rec.id, job.id)

    assert not await controller._schedule_retry_if_allowed(
        rec,
        ProviderRequestAcceptanceUnknown("soniox"),
    )
    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.status.value == "running"
    assert persisted.next_retry_at == ""


@pytest.mark.asyncio
async def test_file_save_failure_after_provider_result_never_reuploads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = JobStore(db_path=tmp_path / "jobs.db")
    controller = ScriberWebController(asyncio.get_running_loop(), job_store=store)
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    rec = _record(
        transcript_id="post-provider-save-failure",
        transcript_type="file",
        source=source,
    )
    job = store.enqueue(
        transcript_id=rec.id,
        job_type=JobType.FILE,
        payload={"path": str(source)},
    )
    assert store.mark_running(job.id)
    controller._remember_job_id(rec.id, job.id)
    provider_call = AsyncMock(return_value="A durable provider transcript.")

    async def fail_required_save(record, *, require_success=False, **_kwargs):
        if require_success:
            record._persistence_failed = True
            raise TranscriptPersistenceError("synthetic disk failure")

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    with (
        patch.object(
            controller,
            "_transcribe_file_to_canonical_artifact",
            new=provider_call,
        ),
        patch.object(
            controller,
            "_save_transcript_to_db_async",
            new=AsyncMock(side_effect=fail_required_save),
        ),
        patch.object(controller, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(controller, "_cleanup_owned_file_source", new=AsyncMock()),
    ):
        await controller._run_file_transcription(
            rec,
            source,
            provider="soniox",
        )

    assert provider_call.await_count == 1
    persisted = store.get(job.id)
    assert persisted is not None
    assert persisted.next_retry_at == ""
    assert rec.status == "failed"


@pytest.mark.asyncio
async def test_youtube_freezes_exact_route_and_lease_before_download(tmp_path: Path):
    controller = ScriberWebController(asyncio.get_running_loop())
    controller._downloads_dir = tmp_path / "downloads"
    rec = _record(
        transcript_id="youtube-exact-route",
        transcript_type="youtube",
        source="https://youtube.com/watch?v=route-test",
    )
    route = controller._freeze_background_provider_route(
        workload="youtube",
        provider="soniox",
        language="en",
    )
    events: list[str] = []
    captured_route = None

    async def begin(_rec, exact_route):
        nonlocal captured_route
        events.append("attempt")
        captured_route = exact_route
        return SimpleNamespace(id="attempt"), "owner", None

    async def download(*_args, **_kwargs):
        events.append("download")
        raise YouTubeDownloadError("synthetic download failure")

    async def completed_task():
        return None

    lease_task = asyncio.create_task(completed_task())

    def start_lease(*_args, **_kwargs):
        events.append("lease")
        return asyncio.Event(), lease_task

    with (
        patch("src.web_api._validate_provider_ready"),
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=download)),
        patch.object(controller, "_ensure_artifact_transcript_row", new=AsyncMock()),
        patch.object(controller, "_begin_transcript_artifact_async", new=begin),
        patch.object(controller, "_start_transcript_artifact_lease_guard", side_effect=start_lease),
        patch.object(controller, "_stop_transcript_artifact_lease_guard", new=AsyncMock()),
        patch.object(
            controller,
            "_terminate_artifact_attempt_before_result_async",
            new=AsyncMock(),
        ),
        patch.object(controller, "_schedule_retry_if_allowed", new=AsyncMock(return_value=False)),
        patch.object(controller, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(controller, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await controller._run_youtube_transcription(
            rec,
            provider="soniox",
            frozen_route=route,
        )

    assert events[:3] == ["attempt", "lease", "download"]
    assert captured_route is not None
    assert captured_route.audio_input_format == AudioInputFormat.WEBM_OPUS
    assert captured_route.audio_input_format_verified is True

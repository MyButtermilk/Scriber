"""HTTP contract for the File transcription ingest domain."""

from __future__ import annotations

import asyncio
import errno
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

from src.api import file_transcription_routes
from src.api.file_transcription_routes import (
    FileTranscriptionControllerPort,
    FileUploadPlan,
    register_file_transcription_routes,
)
from src.api.upload_policy import FileUploadLimits, UploadLimit, file_upload_limits
from src.core import logging_setup
from src.data.job_store import JobStore
from src.transcript_artifacts import FrozenTranscriptionRoute


def _route() -> FrozenTranscriptionRoute:
    return FrozenTranscriptionRoute(
        workload="file",
        source_track="upload",
        provider="assemblyai",
        model="best",
        transport="batch",
        language="auto",
        response_shape="transcript",
        timestamp_mode="none",
        diarization_mode="disabled",
        parser_id="test",
        parser_version="1",
    )


def _plan(*, source_is_video: bool = False) -> FileUploadPlan:
    return FileUploadPlan(
        route=_route(),
        limits=FileUploadLimits(
            source_is_video=source_is_video,
            ingest=UploadLimit(max_bytes=1024 * 1024, label="1MB"),
            final_audio=UploadLimit(max_bytes=512 * 1024, label="512KB"),
        ),
    )


@dataclass
class _PublicRecord:
    id: str = "file-record"

    def to_public(self, *, include_content: bool) -> dict[str, object]:
        return {"id": self.id, "status": "processing", "includeContent": include_content}


class _Controller:
    def __init__(self, root: Path, *, plan: FileUploadPlan | None = None) -> None:
        self._root = root
        self._plan = plan or _plan()
        self.started: list[tuple[Path, str, FileUploadPlan]] = []
        self.started_ids: list[str | None] = []
        self.resume_checkpointed_transcription = AsyncMock(return_value=True)

    @property
    def file_upload_root(self) -> Path:
        return self._root

    def plan_file_upload(self, *, source_is_video: bool) -> FileUploadPlan:
        assert source_is_video == self._plan.source_is_video
        return self._plan

    async def start_file_transcription(
        self,
        file_path: Path,
        original_filename: str,
        *,
        plan: FileUploadPlan,
        transcript_id: str | None = None,
    ) -> _PublicRecord:
        self.started.append((file_path, original_filename, plan))
        self.started_ids.append(transcript_id)
        return _PublicRecord(id=transcript_id or "file-record")


async def _client(controller: _Controller) -> TestClient:
    app = web.Application()
    register_file_transcription_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def captured_file_logs(monkeypatch):
    monkeypatch.setattr(logging_setup, "_LOGGING_ENABLED", True)
    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(str(message)),
        serialize=True,
        filter=lambda record: (
            record["extra"].get("workflow") == "file" or record["name"] == file_transcription_routes.__name__
        ),
    )
    try:
        yield messages
    finally:
        logger.remove(sink)


def _assert_upload_events(messages: list[str], correlation_id: str) -> list[dict]:
    events = [json.loads(message)["record"]["extra"] for message in messages]
    assert events
    assert UUID(hex=correlation_id).hex == correlation_id
    assert {event["trace_id"] for event in events} == {correlation_id}
    assert {event["transcript_id"] for event in events} == {correlation_id}
    assert all(0 <= event["duration_ms"] <= 86_400_000 for event in events if "duration_ms" in event)
    return events


@pytest.mark.asyncio
async def test_file_upload_reaches_durable_admission_through_the_domain_route(
    tmp_path: Path, captured_file_logs
) -> None:
    controller = _Controller(tmp_path / "files")
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"RIFF-WAVE", filename="admitted.wav", content_type="audio/wav")
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    correlation_id = response.headers["X-Scriber-Correlation-Id"]
    assert payload == {"id": correlation_id, "status": "processing", "includeContent": True}
    assert controller.started_ids == [correlation_id]
    assert len(controller.started) == 1
    admitted_path, admitted_name, admitted_plan = controller.started[0]
    assert admitted_path.read_bytes() == b"RIFF-WAVE"
    assert admitted_name == "admitted.wav"
    assert admitted_plan is controller._plan
    assert admitted_path.parent.name == correlation_id
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert events[-1]["event"] == "file.upload.completed"
    assert events[-1]["outcome"] == "success"
    assert {event["stage"] for event in events} >= {"admission", "streaming", "preparation", "handoff"}
    assert "admitted.wav" not in "".join(captured_file_logs)
    assert "RIFF-WAVE" not in "".join(captured_file_logs)
    assert file_transcription_routes._UPLOAD_CORRELATION_ID.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openrouter_stt", "azure_mai", "soniox", "soniox_async"])
async def test_provider_mp3_bypasses_lossy_ingest_compression(monkeypatch, tmp_path, provider):
    route = replace(_route(), provider=provider, model="microsoft/mai-transcribe-2")
    plan = FileUploadPlan(route=route, limits=file_upload_limits(provider, source_is_video=False))
    controller = _Controller(tmp_path / "files", plan=plan)
    compressor = AsyncMock(side_effect=AssertionError("An MP3 must reach the chunking adapter unchanged"))
    monkeypatch.setattr(file_transcription_routes, "maybe_compress_audio_upload", compressor)
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"original-mp3", filename="recording.mp3", content_type="audio/mpeg")
        response = await client.post("/api/file/transcribe", data=form)
        assert response.status == 200
    finally:
        await client.close()
    compressor.assert_not_called()
    assert controller.started[0][0].read_bytes() == b"original-mp3"


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_bytes, expected_status", [(4, 200), (9, 413)])
async def test_large_video_is_admitted_by_its_extracted_audio(monkeypatch, tmp_path, audio_bytes, expected_status):
    monkeypatch.setenv("SCRIBER_UPLOAD_MAX_BYTES", "8")
    plan = FileUploadPlan(route=_route(), limits=file_upload_limits("assemblyai", source_is_video=True))
    controller = _Controller(tmp_path / "files", plan=plan)

    class VideoField:
        name = "file"
        filename = "large-video.mp4"

        def __init__(self):
            self.chunks = iter([b"bounded-video-chunk", b""])

        async def read_chunk(self, *, size):
            return next(self.chunks)

    async def fields():
        yield VideoField()

    # A large declared video must reach the bounded stream writer, not the old
    # Content-Length pre-rejection. No multi-GB allocation is needed by the test.
    request = SimpleNamespace(
        content_type="multipart/form-data",
        content_length=3 * 1024**3,
        multipart=AsyncMock(return_value=fields()),
        app={file_transcription_routes.APP_FILE_TRANSCRIPTION_SERVICE: SimpleNamespace(controller=controller)},
    )

    async def extract(path, root):
        assert path.read_bytes() == b"bounded-video-chunk"
        audio = root / "prepared.webm"
        audio.write_bytes(b"a" * audio_bytes)
        return audio

    monkeypatch.setattr(file_transcription_routes, "extract_audio_from_video", extract)
    monkeypatch.setattr(
        file_transcription_routes, "maybe_compress_audio_upload", AsyncMock(side_effect=lambda path, **_: path)
    )
    response = await file_transcription_routes.transcribe_file(request)
    assert response.status == expected_status
    if expected_status == 200:
        path, name, admitted_plan = controller.started[0]
        assert path.read_bytes() == b"a" * audio_bytes
        assert name == "large-video.mp4"
        assert not (path.parent / name).exists()
        assert admitted_plan.final_audio_max_bytes == 8
    else:
        assert controller.started == []
        assert list(controller.file_upload_root.iterdir()) == []


@pytest.mark.asyncio
async def test_parallel_video_preparations_keep_separate_sources_and_jobs(monkeypatch, tmp_path, captured_file_logs):
    controller = _Controller(tmp_path / "files", plan=_plan(source_is_video=True))
    entered: list[Path] = []
    both_entered = asyncio.Event()

    async def extract(path, root):
        entered.append(path)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        audio = root / "prepared.webm"
        audio.write_bytes(path.read_bytes())
        return audio

    monkeypatch.setattr(file_transcription_routes, "extract_audio_from_video", extract)
    client = await _client(controller)
    try:
        forms = []
        for name in ("first", "second"):
            form = FormData()
            form.add_field("file", name.encode(), filename=f"{name}.mp4", content_type="video/mp4")
            forms.append(form)
        responses = await asyncio.gather(*(client.post("/api/file/transcribe", data=form) for form in forms))
        assert [response.status for response in responses] == [200, 200]
    finally:
        await client.close()
    assert len({path.parent for path, _, _ in controller.started}) == 2
    assert {path.read_bytes() for path, _, _ in controller.started} == {b"first", b"second"}
    response_ids = {response.headers["X-Scriber-Correlation-Id"] for response in responses}
    assert response_ids == set(controller.started_ids)
    for correlation_id in response_ids:
        messages = [
            message
            for message in captured_file_logs
            if json.loads(message)["record"]["extra"].get("trace_id") == correlation_id
        ]
        events = _assert_upload_events(messages, correlation_id)
        assert events[-1]["outcome"] == "success"


def test_unbounded_video_evidence_roundtrips_and_legacy_bounds_are_preserved(monkeypatch):
    monkeypatch.delenv("SCRIBER_UPLOAD_MAX_BYTES", raising=False)
    plan = FileUploadPlan(route=_route(), limits=file_upload_limits("assemblyai", source_is_video=True))
    evidence = plan.durable_evidence()
    assert evidence["ingestMaxBytes"] is None
    assert FileUploadPlan.from_durable_evidence(route=_route(), evidence=evidence) == plan
    for version in (1, 2):
        legacy = {**evidence, "schemaVersion": version, "ingestMaxBytes": 2 * 1024**3, "ingestLimitLabel": "2GB"}
        assert FileUploadPlan.from_durable_evidence(route=_route(), evidence=legacy).ingest_max_bytes == 2 * 1024**3
    with pytest.raises(ValueError):
        FileUploadPlan.from_durable_evidence(route=_route(), evidence={**evidence, "sourceKind": "audio"})
    with pytest.raises(ValueError):
        FileUploadPlan.from_durable_evidence(route=_route(), evidence={**evidence, "finalAudioMaxBytes": None})


@pytest.mark.asyncio
async def test_full_disk_returns_actionable_error_and_cleans_partial_upload(monkeypatch, tmp_path):
    controller = _Controller(tmp_path / "files")

    async def disk_full(_field, path, **_kwargs):
        path.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_transcription_routes, "write_upload_stream_to_disk", disk_full)
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"audio", filename="audio.wav")
        response = await client.post("/api/file/transcribe", data=form)
        assert response.status == 507
        assert await response.json() == {
            "message": "Not enough disk space to prepare this file.",
            "correlationId": response.headers["X-Scriber-Correlation-Id"],
        }
    finally:
        await client.close()
    assert controller.started == []
    assert list(controller.file_upload_root.iterdir()) == []


@pytest.mark.asyncio
async def test_empty_upload_is_rejected_before_ownership_transfer(tmp_path: Path) -> None:
    controller = _Controller(tmp_path / "files")
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"", filename="empty.wav", content_type="audio/wav")
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 400
    assert payload == {
        "message": "Uploaded file is empty",
        "correlationId": response.headers["X-Scriber-Correlation-Id"],
    }
    assert controller.started == []
    assert list(controller.file_upload_root.iterdir()) == []


@pytest.mark.asyncio
async def test_unexpected_start_failure_is_redacted_after_ownership_handoff(
    monkeypatch,
    tmp_path: Path,
    captured_file_logs,
) -> None:
    controller = _Controller(tmp_path / "files")
    monkeypatch.setattr(
        controller,
        "start_file_transcription",
        AsyncMock(side_effect=OSError(r"C:\Users\Alice\private.wav token=top-secret")),
    )
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"RIFF-WAVE", filename="private.wav", content_type="audio/wav")
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 500
    correlation_id = response.headers["X-Scriber-Correlation-Id"]
    assert payload == {"message": "Failed to process file upload", "correlationId": correlation_id}
    assert controller.start_file_transcription.await_args.kwargs["transcript_id"] == correlation_id
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert events[-1]["outcome"] == "failure"
    assert events[-1]["meta"] == {"status": 500, "phase": "handoff"}
    assert "top-secret" not in "".join(captured_file_logs)
    assert "private.wav" not in "".join(captured_file_logs)
    handed_off_path = controller.start_file_transcription.await_args.args[0]
    assert handed_off_path.is_file()


def test_file_upload_plan_round_trip_preserves_reviewed_labels() -> None:
    plan = FileUploadPlan(
        route=_route(),
        limits=FileUploadLimits(
            source_is_video=False,
            ingest=UploadLimit(2_200_000_000, "2.2GB"),
            final_audio=UploadLimit(2_200_000_000, "2.2GB"),
        ),
    )

    restored = FileUploadPlan.from_durable_evidence(
        route=plan.route,
        evidence=plan.durable_evidence(),
    )

    assert restored == plan


@pytest.mark.asyncio
async def test_video_extraction_failure_is_redacted(monkeypatch, tmp_path: Path, captured_file_logs) -> None:
    controller = _Controller(tmp_path / "files", plan=_plan(source_is_video=True))
    monkeypatch.setattr(
        file_transcription_routes,
        "extract_audio_from_video",
        AsyncMock(side_effect=RuntimeError(r"C:\Users\Alice\private.mp4 token=top-secret")),
    )
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"video", filename="private.mp4", content_type="video/mp4")
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 500
    correlation_id = response.headers["X-Scriber-Correlation-Id"]
    assert payload == {"message": "Failed to extract audio from video.", "correlationId": correlation_id}
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert any(event["stage"] == "cleanup" and event["outcome"] == "success" for event in events)
    assert events[-1]["error_category"] == "extraction_failed"
    assert "top-secret" not in "".join(captured_file_logs)
    assert "private.mp4" not in "".join(captured_file_logs)


@pytest.mark.asyncio
async def test_compression_fallback_keeps_upload_correlation_without_logging_source_or_exception(
    monkeypatch, tmp_path: Path, captured_file_logs
) -> None:
    controller = _Controller(tmp_path / "files")
    monkeypatch.setattr(file_transcription_routes, "UPLOAD_COMPRESSION_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(
        file_transcription_routes,
        "_transcode_media_to_webm_audio",
        AsyncMock(side_effect=RuntimeError("private-source.wav token=compression-secret")),
    )
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"private-audio-data", filename="private-source.wav", content_type="audio/wav")
        response = await client.post("/api/file/transcribe", data=form)
        assert response.status == 200
    finally:
        await client.close()

    events = _assert_upload_events(captured_file_logs, response.headers["X-Scriber-Correlation-Id"])
    assert any(event.get("error_category") == "compression_failed" for event in events)
    assert events[-1]["outcome"] == "success"
    assert "compression-secret" not in "".join(captured_file_logs)
    assert "private-source.wav" not in "".join(captured_file_logs)
    assert "private-audio-data" not in "".join(captured_file_logs)


@pytest.mark.asyncio
async def test_upload_cancellation_is_correlated_through_workspace_cleanup(tmp_path: Path, captured_file_logs) -> None:
    controller = _Controller(tmp_path / "files")

    async def fields():
        yield SimpleNamespace(
            name="file",
            filename="private-source.wav",
            read_chunk=AsyncMock(side_effect=asyncio.CancelledError),
        )

    request = SimpleNamespace(
        content_type="multipart/form-data",
        content_length=None,
        multipart=AsyncMock(return_value=fields()),
        app={file_transcription_routes.APP_FILE_TRANSCRIPTION_SERVICE: SimpleNamespace(controller=controller)},
    )
    with pytest.raises(asyncio.CancelledError):
        await file_transcription_routes.transcribe_file(request)

    correlation_id = json.loads(captured_file_logs[0])["record"]["extra"]["trace_id"]
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert {event["stage"] for event in events} >= {"cancelled", "cleanup", "completed"}
    assert events[-1]["outcome"] == "cancelled"
    assert events[-1]["meta"]["status"] == 499
    assert controller.started == []
    assert list(controller.file_upload_root.iterdir()) == []
    assert file_transcription_routes._UPLOAD_CORRELATION_ID.get() is None
    assert "private-source.wav" not in "".join(captured_file_logs)


@pytest.mark.asyncio
async def test_upload_rejection_has_correlation_before_multipart_parsing(tmp_path: Path, captured_file_logs) -> None:
    controller = _Controller(tmp_path / "files")
    client = await _client(controller)
    try:
        response = await client.post("/api/file/transcribe", json={"filename": "private.wav"})
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 400
    correlation_id = response.headers["X-Scriber-Correlation-Id"]
    assert payload == {"message": "Expected multipart/form-data", "correlationId": correlation_id}
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert events[-1]["outcome"] == "rejected"
    assert not controller.file_upload_root.exists()
    assert "private.wav" not in "".join(captured_file_logs)


@pytest.mark.asyncio
async def test_oversized_upload_stays_413_when_first_cleanup_attempt_fails(
    monkeypatch, tmp_path: Path, captured_file_logs
) -> None:
    plan = FileUploadPlan(
        route=_route(),
        limits=FileUploadLimits(
            source_is_video=False,
            ingest=UploadLimit(4, "4 bytes"),
            final_audio=UploadLimit(4, "4 bytes"),
        ),
    )
    controller = _Controller(tmp_path / "files", plan=plan)
    real_remove = file_transcription_routes.remove_tree_if_exists
    cleanup_calls = 0

    async def fail_first_cleanup(path: Path) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise OSError("private-source.wav token=cleanup-secret")
        await real_remove(path)

    monkeypatch.setattr(file_transcription_routes, "remove_tree_if_exists", fail_first_cleanup)
    client = await _client(controller)
    try:
        form = FormData()
        form.add_field("file", b"12345", filename="oversized.wav", content_type="audio/wav")
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 413
    correlation_id = response.headers["X-Scriber-Correlation-Id"]
    assert payload == {"message": "File too large (max raw upload 4 bytes).", "correlationId": correlation_id}
    assert cleanup_calls == 2
    events = _assert_upload_events(captured_file_logs, correlation_id)
    assert [event["outcome"] for event in events if event["stage"] == "cleanup"] == ["failure", "success"]
    assert events[-1]["outcome"] == "rejected"
    assert "cleanup-secret" not in "".join(captured_file_logs)
    assert "private-source.wav" not in "".join(captured_file_logs)


@pytest.mark.asyncio
async def test_workspace_cleanup_retries_before_delivering_repeated_cancellation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "incomplete-upload"
    workspace.mkdir()
    (workspace / "source.part").write_bytes(b"partial")
    cleanup_started = threading.Event()
    allow_cleanup_failure = threading.Event()
    cleanup_calls = 0
    real_remove = file_transcription_routes.remove_tree_if_exists

    def blocking_failure() -> None:
        cleanup_started.set()
        if not allow_cleanup_failure.wait(timeout=3):
            raise TimeoutError("cleanup failure gate was not released")
        raise OSError("workspace is temporarily locked")

    async def fail_once_then_remove(path: Path) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            await file_transcription_routes.await_with_delayed_cancellation(asyncio.to_thread(blocking_failure))
            return
        await real_remove(path)

    monkeypatch.setattr(
        file_transcription_routes,
        "remove_tree_if_exists",
        fail_once_then_remove,
    )
    cleanup_task = asyncio.create_task(file_transcription_routes._cleanup_unowned_workspace(workspace))
    assert await asyncio.to_thread(cleanup_started.wait, 3)

    cleanup_task.cancel()
    cleanup_task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_task.done()
    allow_cleanup_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup_task

    assert cleanup_calls == 2
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_compression_uses_the_admitted_provider_limit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(file_transcription_routes, "UPLOAD_COMPRESSION_THRESHOLD_BYTES", 10_000)
    upload_path = tmp_path / "over-provider-limit.mp3"
    upload_path.write_bytes(b"x" * 4096)

    async def fake_transcode(source_path, target_path, *, bitrate):
        assert source_path == upload_path
        assert bitrate == file_transcription_routes.COMPRESSED_AUDIO_BITRATE
        target_path.write_bytes(b"y" * 1024)
        return target_path

    monkeypatch.setattr(file_transcription_routes, "_transcode_media_to_webm_audio", fake_transcode)

    result = await file_transcription_routes.maybe_compress_audio_upload(upload_path, max_bytes=2048)

    assert result.suffix == ".webm"
    assert result.read_bytes() == b"y" * 1024
    assert not upload_path.exists()


@pytest.mark.asyncio
async def test_compression_keeps_the_original_when_output_is_not_smaller(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(file_transcription_routes, "UPLOAD_COMPRESSION_THRESHOLD_BYTES", 2048)
    upload_path = tmp_path / "large.wav"
    upload_path.write_bytes(b"x" * 4096)

    async def fake_transcode(_source_path, target_path, *, bitrate):
        assert bitrate == file_transcription_routes.COMPRESSED_AUDIO_BITRATE
        target_path.write_bytes(b"y" * 8192)
        return target_path

    monkeypatch.setattr(file_transcription_routes, "_transcode_media_to_webm_audio", fake_transcode)

    result = await file_transcription_routes.maybe_compress_audio_upload(upload_path)

    assert result == upload_path
    assert upload_path.exists()


def test_file_route_port_matches_the_production_controller(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        FileTranscriptionControllerPort,
        ScriberWebController,
        methods={"plan_file_upload", "start_file_transcription", "resume_checkpointed_transcription"},
        properties={"file_upload_root"},
        returns={"plan_file_upload": FileUploadPlan, "resume_checkpointed_transcription": bool},
    )


@pytest.mark.asyncio
async def test_composition_queues_the_same_provider_route_used_for_admission(monkeypatch, tmp_path: Path) -> None:
    """A circuit change while bytes arrive must not change the admitted route."""

    from src import web_api

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    store = JobStore(db_path=tmp_path / "jobs.db")
    controller = web_api.ScriberWebController(asyncio.get_running_loop(), job_store=store)
    controller._downloads_dir = tmp_path / "downloads"
    selected_providers = iter(("assemblyai", "smallest"))
    monkeypatch.setattr(controller, "_select_available_provider", lambda: next(selected_providers))
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(controller, "_schedule_file_job", lambda *_args, **_kwargs: None)

    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        form = FormData()
        form.add_field(
            "file",
            b"RIFF\x00\x00\x00\x00WAVEfmt ",
            filename="admitted.wav",
            content_type="audio/wav",
        )
        response = await client.post("/api/file/transcribe", data=form)
        payload = await response.json()
        job = store.get_by_transcript_id(str(payload.get("id") or ""))
    finally:
        await client.close()

    assert response.status == 200, payload
    assert job is not None
    assert job.payload["executionRoute"]["provider"] == "assemblyai"


class _ChunkUploadField:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read_chunk(self, *, size: int) -> bytes:
        del size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_resume_file_uses_the_existing_transcript_identity(tmp_path: Path, captured_file_logs) -> None:
    controller = _Controller(tmp_path)
    transcript_id = "a" * 32
    client = await _client(controller)
    try:
        response = await client.post(f"/api/transcripts/{transcript_id}/resume-file")
        assert response.status == 202
        assert await response.json() == {"success": True, "id": transcript_id}
        assert response.headers["X-Scriber-Correlation-Id"] == transcript_id
    finally:
        await client.close()
    controller.resume_checkpointed_transcription.assert_awaited_once_with(transcript_id)
    events = _assert_upload_events(captured_file_logs, transcript_id)
    assert events[-1]["outcome"] == "success"
    assert controller.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [False, ValueError("Private C:/source/changed.wav")])
async def test_resume_file_rejects_ineligible_checkpoint_without_disclosing_source(
    tmp_path: Path, outcome, captured_file_logs
) -> None:
    controller = _Controller(tmp_path)
    transcript_id = "a" * 32
    if isinstance(outcome, Exception):
        controller.resume_checkpointed_transcription.side_effect = outcome
    else:
        controller.resume_checkpointed_transcription.return_value = outcome
    client = await _client(controller)
    try:
        response = await client.post(f"/api/transcripts/{transcript_id}/resume-file")
        assert response.status == 409
        assert await response.json() == {
            "message": "This transcription cannot be safely resumed from its saved progress.",
            "correlationId": transcript_id,
        }
        assert response.headers["X-Scriber-Correlation-Id"] == transcript_id
    finally:
        await client.close()
    events = _assert_upload_events(captured_file_logs, transcript_id)
    assert events[-1]["meta"] == {"status": 409}
    assert events[-1]["outcome"] == "rejected"
    assert "changed.wav" not in "".join(captured_file_logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript_id", ["a" * 32, "private-source.wav"])
async def test_resume_file_hides_unexpected_failure_details(tmp_path: Path, transcript_id, captured_file_logs) -> None:
    controller = _Controller(tmp_path)
    controller.resume_checkpointed_transcription.side_effect = RuntimeError("private-source.wav token=resume-secret")
    client = await _client(controller)
    try:
        response = await client.post(f"/api/transcripts/{transcript_id}/resume-file")
        assert response.status == 500
        correlation_id = response.headers["X-Scriber-Correlation-Id"]
        assert await response.json() == {"message": "Failed to resume transcription.", "correlationId": correlation_id}
    finally:
        await client.close()
    events = [json.loads(message)["record"]["extra"] for message in captured_file_logs]
    assert UUID(hex=correlation_id).hex == correlation_id
    assert events[-1]["trace_id"] == correlation_id
    assert events[-1]["meta"] == {"status": 500}
    if transcript_id == "a" * 32:
        assert correlation_id == transcript_id
        assert events[-1]["transcript_id"] == transcript_id
    else:
        assert "transcript_id" not in events[-1]
    assert "private-source.wav" not in "".join(captured_file_logs)
    assert "resume-secret" not in "".join(captured_file_logs)


def test_multipart_content_length_allows_framing_overhead_at_file_limit():
    file_limit = 25 * 1024 * 1024

    assert (
        file_transcription_routes._multipart_request_is_definitely_oversized(
            file_limit + file_transcription_routes._MULTIPART_CONTENT_LENGTH_ALLOWANCE_BYTES,
            file_limit=file_limit,
        )
        is False
    )
    assert (
        file_transcription_routes._multipart_request_is_definitely_oversized(
            file_limit + file_transcription_routes._MULTIPART_CONTENT_LENGTH_ALLOWANCE_BYTES + 1,
            file_limit=file_limit,
        )
        is True
    )


@pytest.mark.asyncio
async def test_write_upload_stream_to_disk_writes_chunks_off_hot_path(tmp_path):
    target = tmp_path / "upload.bin"
    field = _ChunkUploadField([b"abc", b"def"])

    bytes_read, too_large = await file_transcription_routes.write_upload_stream_to_disk(
        field,
        target,
        max_bytes=16,
    )

    assert bytes_read == 6
    assert too_large is False
    assert target.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_write_upload_stream_to_disk_stops_before_oversized_chunk(tmp_path):
    target = tmp_path / "upload.bin"
    field = _ChunkUploadField([b"abc", b"def"])

    bytes_read, too_large = await file_transcription_routes.write_upload_stream_to_disk(
        field,
        target,
        max_bytes=4,
    )

    assert bytes_read == 6
    assert too_large is True
    assert target.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_write_upload_stream_batches_disk_dispatches(monkeypatch, tmp_path):
    target = tmp_path / "upload.bin"
    field = _ChunkUploadField([b"ab"] * 10)
    real_to_thread = asyncio.to_thread
    write_calls = 0

    async def tracking_to_thread(func, /, *args, **kwargs):
        nonlocal write_calls
        if getattr(func, "__name__", "") == "write":
            write_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(file_transcription_routes.asyncio, "to_thread", tracking_to_thread)
    bytes_read, too_large = await file_transcription_routes.write_upload_stream_to_disk(
        field,
        target,
        max_bytes=64,
        chunk_size=2,
        write_batch_size=6,
    )

    assert bytes_read == 20
    assert too_large is False
    assert target.read_bytes() == b"ab" * 10
    assert write_calls == 4


@pytest.mark.asyncio
async def test_write_upload_stream_closes_a_file_opened_after_repeated_cancellation(monkeypatch, tmp_path):
    open_started = threading.Event()
    allow_open = threading.Event()

    class TrackedFile:
        closed = False

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            self.closed = True

    tracked = TrackedFile()

    def delayed_open(*_args, **_kwargs):
        open_started.set()
        assert allow_open.wait(timeout=2.0)
        return tracked

    monkeypatch.setattr(file_transcription_routes, "open", delayed_open, raising=False)
    task = asyncio.create_task(
        file_transcription_routes.write_upload_stream_to_disk(
            _ChunkUploadField([]),
            tmp_path / "upload.bin",
            max_bytes=16,
        )
    )
    assert await asyncio.to_thread(open_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_before_open = task.done()
    allow_open.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_before_open is False
    assert tracked.closed is True


@pytest.mark.asyncio
async def test_write_upload_stream_never_closes_while_a_canceled_write_is_running(monkeypatch, tmp_path):
    write_started = threading.Event()
    allow_write = threading.Event()

    class TrackedFile:
        closed = False
        close_overlapped_write = False

        def write(self, data: bytes) -> int:
            write_started.set()
            assert allow_write.wait(timeout=2.0)
            return len(data)

        def close(self) -> None:
            self.close_overlapped_write = not allow_write.is_set()
            self.closed = True

    tracked = TrackedFile()
    monkeypatch.setattr(file_transcription_routes, "open", lambda *_args, **_kwargs: tracked, raising=False)
    task = asyncio.create_task(
        file_transcription_routes.write_upload_stream_to_disk(
            _ChunkUploadField([b"abc"]),
            tmp_path / "upload.bin",
            max_bytes=16,
            chunk_size=1,
            write_batch_size=1,
        )
    )
    assert await asyncio.to_thread(write_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)
    completed_during_write = task.done()
    allow_write.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_during_write is False
    assert tracked.closed is True
    assert tracked.close_overlapped_write is False


@pytest.mark.asyncio
async def test_write_upload_stream_waits_for_close_after_repeated_cancellation(monkeypatch, tmp_path):
    close_started = threading.Event()
    allow_close = threading.Event()

    class TrackedFile:
        closed = False

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            close_started.set()
            assert allow_close.wait(timeout=2.0)
            self.closed = True

    tracked = TrackedFile()
    monkeypatch.setattr(file_transcription_routes, "open", lambda *_args, **_kwargs: tracked, raising=False)
    task = asyncio.create_task(
        file_transcription_routes.write_upload_stream_to_disk(
            _ChunkUploadField([]),
            tmp_path / "upload.bin",
            max_bytes=16,
        )
    )
    assert await asyncio.to_thread(close_started.wait, 1.0)

    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    completed_during_close = task.done()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed_during_close is False
    assert tracked.closed is True

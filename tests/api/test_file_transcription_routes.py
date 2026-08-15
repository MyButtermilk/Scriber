"""HTTP contract for the File transcription ingest domain."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from src.api import file_transcription_routes
from src.api.file_transcription_routes import (
    FileTranscriptionControllerPort,
    FileUploadPlan,
    register_file_transcription_routes,
)
from src.api.upload_policy import FileUploadLimits, UploadLimit
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
    ) -> _PublicRecord:
        self.started.append((file_path, original_filename, plan))
        return _PublicRecord()


async def _client(controller: _Controller) -> TestClient:
    app = web.Application()
    register_file_transcription_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_file_upload_reaches_durable_admission_through_the_domain_route(tmp_path: Path) -> None:
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
    assert payload == {"id": "file-record", "status": "processing", "includeContent": True}
    assert len(controller.started) == 1
    admitted_path, admitted_name, admitted_plan = controller.started[0]
    assert admitted_path.read_bytes() == b"RIFF-WAVE"
    assert admitted_name == "admitted.wav"
    assert admitted_plan is controller._plan


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
    assert payload == {"message": "Uploaded file is empty"}
    assert controller.started == []
    assert list(controller.file_upload_root.iterdir()) == []


@pytest.mark.asyncio
async def test_unexpected_start_failure_is_redacted_after_ownership_handoff(
    monkeypatch,
    tmp_path: Path,
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
    assert payload == {"message": "Failed to process file upload"}
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
async def test_video_extraction_failure_is_redacted(monkeypatch, tmp_path: Path) -> None:
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
    assert payload == {"message": "Failed to extract audio from video."}


@pytest.mark.asyncio
async def test_oversized_upload_stays_413_when_first_cleanup_attempt_fails(monkeypatch, tmp_path: Path) -> None:
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
            raise OSError("transient cleanup failure")
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
    assert payload == {"message": "File too large (max raw upload 4 bytes)."}
    assert cleanup_calls == 2


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
        methods={"plan_file_upload", "start_file_transcription"},
        properties={"file_upload_root"},
        returns={"plan_file_upload": FileUploadPlan},
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

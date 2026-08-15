"""Meeting import routes exercised without web_api.create_app.

The durable protocol is driven against the real ``MeetingImportStore`` rather
than a hand-written double: the interesting behaviour here is which state a job
is left in, and a fake store would only assert that the module calls the methods
this module already calls.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from dataclasses import FrozenInstanceError
from itertools import pairwise
from math import isclose
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api import meeting_import_routes
from src.api.meeting_import_routes import (
    APP_MEETING_IMPORT_SERVICE,
    MeetingImportDeps,
    MeetingImportStorePort,
    register_meeting_import_routes,
)
from src.api.upload_policy import FileUploadLimits, UploadLimit
from src.data.meeting_import_store import MeetingImportRecord, MeetingImportStatus, MeetingImportStore

_MEGABYTE = 1024 * 1024


def _payload(record: Any, *, upload_url: str = "") -> dict[str, Any]:
    payload = {
        "id": record.id,
        "state": record.status.value,
        "sourceFilename": record.source_filename,
        "expectedBytes": record.expected_bytes,
        "receivedBytes": record.received_bytes,
        "meetingId": record.meeting_id or None,
    }
    if upload_url:
        payload["uploadUrl"] = upload_url
    return payload


def _inbox(record: Any) -> dict[str, Any]:
    return {"id": record.id, "state": record.status.value}


class _Harness:
    """The durable store plus the mutable state the deps bundle exposes."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = MeetingImportStore(tmp_path / "imports.db")
        self.storage_root = tmp_path
        self.processing_tasks: dict[str, asyncio.Task[None]] = {}
        self.upload_tasks: dict[str, asyncio.Task[web.Response]] = {}
        self.broadcasts: list[tuple[str, float, str]] = []
        self.scheduled: list[str] = []
        self.shutting_down = False
        self.provider_error: Exception | None = None
        self.schedule_error: Exception | None = None
        self.audio_limit = 32 * _MEGABYTE
        self.video_limit = 64 * _MEGABYTE
        self.audio_limit_label = "32MB"
        self.video_limit_label = "64MB"

    async def _broadcast(self, record: Any, progress: float, status: str) -> None:
        self.broadcasts.append((record.id, progress, status))

    def _schedule(self, import_id: str) -> bool:
        if self.schedule_error is not None:
            raise self.schedule_error
        self.scheduled.append(import_id)
        return True

    def _validate_provider_ready(self, provider: str) -> None:
        if self.provider_error is not None:
            raise self.provider_error

    def deps(self) -> MeetingImportDeps:
        def upload_limits(_provider: str | None, *, source_is_video: bool) -> FileUploadLimits:
            max_bytes = self.video_limit if source_is_video else self.audio_limit
            label = self.video_limit_label if source_is_video else self.audio_limit_label
            return FileUploadLimits(
                source_is_video=source_is_video,
                ingest=UploadLimit(max_bytes, label),
                final_audio=UploadLimit(max_bytes, label),
            )

        return MeetingImportDeps(
            store=self.store,
            broadcast=self._broadcast,
            schedule=self._schedule,
            processing_tasks=self.processing_tasks,
            upload_tasks=self.upload_tasks,
            storage_root=self.storage_root,
            is_shutting_down=lambda: self.shutting_down,
            validate_provider_ready=self._validate_provider_ready,
            upload_limits=upload_limits,
        )

    def complete(self, import_id: str, *, meeting_id: str) -> None:
        """Walk an uploaded job all the way to COMPLETED.

        The store enforces the whole ladder, so a test that needs a finished job
        has to climb it rather than jump; each rung also has artifact
        preconditions attached.
        """
        self.store.transition(import_id, MeetingImportStatus.PROBING)
        self.store.transition(import_id, MeetingImportStatus.PREPARING)
        self.store.mark_prepared(
            import_id,
            relative_path=f"meeting-imports/{import_id}/normalized.wav",
            byte_count=4,
            sha256="0" * 64,
            probe={"durationMs": 1000},
        )
        self.store.transition(import_id, MeetingImportStatus.COMMITTING, meeting_id=meeting_id)
        self.store.transition(import_id, MeetingImportStatus.FINALIZING)
        self.store.transition(import_id, MeetingImportStatus.COMPLETED)

    def create_record(self, *, filename: str = "standup.wav", expected_bytes: int = 8):
        return self.store.create(
            source_filename=filename,
            expected_bytes=expected_bytes,
            profile_snapshot={"id": "default", "language": "de"},
            metadata={"title": "Standup"},
        )


@pytest.fixture
def harness(tmp_path):
    built = _Harness(tmp_path)
    try:
        yield built
    finally:
        built.store.close()


async def _client(harness: _Harness) -> TestClient:
    app = web.Application()
    register_meeting_import_routes(
        app,
        deps=harness.deps,
        record_payload=_payload,
        inbox_payload=_inbox,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_list_clamps_the_limit_and_serialises_through_the_inbox_shape(harness):
    harness.create_record()
    client = await _client(harness)
    try:
        response = await client.get("/api/meeting-imports", params={"limit": "9999"})
        assert response.status == 200
        body = await response.json()
        assert body["limit"] == 50
        assert body["total"] == 1
        # The inbox payload is deliberately narrower than the single-job payload.
        assert set(body["items"][0]) == {"id", "state"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_rejects_a_limit_that_is_not_a_number(harness):
    client = await _client(harness)
    try:
        response = await client.get("/api/meeting-imports", params={"limit": "soon"})
        assert response.status == 400
        assert (await response.json())["message"] == "Meeting import limit must be a whole number."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_returns_the_upload_url_the_client_has_to_use_next(harness):
    client = await _client(harness)
    try:
        response = await client.post(
            "/api/meeting-imports",
            json={"filename": "Customer interview.webm", "byteSize": 4096},
        )
        assert response.status == 201
        body = await response.json()
        assert body["uploadUrl"] == f"/api/meeting-imports/{body['id']}/content"
        assert body["state"] == MeetingImportStatus.CREATED.value
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_fragment"),
    [
        ({"filename": "notes.txt", "byteSize": 10}, "Unsupported meeting recording type"),
        ({"filename": "a.wav", "byteSize": 0}, "greater than zero"),
        (["not an object"], "Expected JSON object"),
    ],
)
async def test_create_rejects_a_body_it_cannot_turn_into_a_job(harness, body, expected_fragment):
    client = await _client(harness)
    try:
        response = await client.post("/api/meeting-imports", json=body)
        assert response.status == 400
        assert expected_fragment in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_reports_a_provider_that_is_not_configured(harness):
    harness.provider_error = RuntimeError("Set an OpenAI API key before importing recordings.")
    client = await _client(harness)
    try:
        response = await client.post("/api/meeting-imports", json={"filename": "a.wav", "byteSize": 10})
        assert response.status == 400
        assert "OpenAI API key" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_applies_the_video_limit_to_video_and_the_audio_limit_to_audio(harness):
    harness.audio_limit = 1024
    harness.video_limit = 4096
    harness.audio_limit_label = "reviewed audio label"
    client = await _client(harness)
    try:
        too_large_audio = await client.post("/api/meeting-imports", json={"filename": "a.wav", "byteSize": 2048})
        assert too_large_audio.status == 413
        assert (await too_large_audio.json())["message"] == (
            "Meeting recording is too large (max reviewed audio label)."
        )

        # The same byte count is inside the (larger) video limit.
        accepted_video = await client.post("/api/meeting-imports", json={"filename": "a.mp4", "byteSize": 2048})
        assert accepted_video.status == 201
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_separates_a_known_job_from_an_unknown_one(harness):
    record = harness.create_record()
    client = await _client(harness)
    try:
        assert (await client.get(f"/api/meeting-imports/{record.id}")).status == 200

        missing = await client.get("/api/meeting-imports/does-not-exist")
        assert missing.status == 404
        assert (await missing.json())["message"] == "Meeting import not found"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_commits_the_source_before_scheduling_processing(harness):
    record = harness.create_record(expected_bytes=4)
    client = await _client(harness)
    try:
        response = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd")
        assert response.status == 202
    finally:
        await client.close()

    persisted = harness.store.require(record.id)
    assert persisted.status == MeetingImportStatus.RECEIVED
    assert (harness.storage_root / persisted.original_relative_path).read_bytes() == b"abcd"
    assert harness.scheduled == [record.id]
    # The registry is the rendezvous with cancellation, so it has to be empty again.
    assert harness.upload_tasks == {}


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_fsync_before_discarding_staging(harness, monkeypatch):
    """The handler must not close or delete a file while fsync still owns it."""
    record = harness.create_record(expected_bytes=4)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_fsync = meeting_import_routes.os.fsync

    def blocking_fsync(fd: int) -> None:
        started.set()
        assert release.wait(timeout=5.0)
        real_fsync(fd)
        finished.set()

    monkeypatch.setattr(meeting_import_routes.os, "fsync", blocking_fsync)
    client = await _client(harness)
    request_task = asyncio.create_task(client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd"))
    try:
        assert await asyncio.to_thread(started.wait, 2.0)
        handler_task = harness.upload_tasks[record.id]
        for _ in range(3):
            handler_task.cancel()
            await asyncio.sleep(0)

        assert not handler_task.done()
        assert not finished.is_set()
        assert (harness.storage_root / "meeting-imports" / record.id).exists()
    finally:
        release.set()
        with suppress(BaseException):
            await request_task
        await client.close()

    assert finished.is_set()
    assert harness.store.require(record.id).status == MeetingImportStatus.FAILED
    assert not (harness.storage_root / "meeting-imports" / record.id).exists()


@pytest.mark.asyncio
async def test_committed_upload_survives_repeated_cancellation_when_status_reread_fails(
    harness,
    monkeypatch,
):
    """A completed mark_received call is enough proof to retain its source."""
    record = harness.create_record(expected_bytes=4)
    mark_committed = threading.Event()
    release_mark_return = threading.Event()
    reread_started = threading.Event()
    release_reread = threading.Event()
    original_mark_received = harness.store.mark_received
    original_require = harness.store.require
    reread_failures = 0

    def committed_mark_received(import_id: str, **kwargs: Any) -> MeetingImportRecord:
        persisted = original_mark_received(import_id, **kwargs)
        mark_committed.set()
        if not release_mark_return.wait(timeout=3.0):
            raise TimeoutError("test did not release the committed mark_received call")
        return persisted

    def failing_status_reread(import_id: str) -> MeetingImportRecord:
        nonlocal reread_failures
        if mark_committed.is_set() and reread_failures == 0:
            reread_started.set()
            if not release_reread.wait(timeout=3.0):
                raise TimeoutError("test did not release the uncertain status reread")
            reread_failures += 1
            raise OSError("transient status read failure")
        return original_require(import_id)

    monkeypatch.setattr(harness.store, "mark_received", committed_mark_received)
    monkeypatch.setattr(harness.store, "require", failing_status_reread)
    client = await _client(harness)
    request_task = asyncio.create_task(client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd"))
    try:
        assert await asyncio.to_thread(mark_committed.wait, 2.0)
        handler_task = harness.upload_tasks[record.id]
        handler_task.cancel()
        release_mark_return.set()
        assert await asyncio.to_thread(reread_started.wait, 2.0)

        for _ in range(2):
            handler_task.cancel()
            await asyncio.sleep(0)

        assert handler_task.done() is False
    finally:
        release_mark_return.set()
        release_reread.set()
        with suppress(BaseException):
            await request_task
        await client.close()

    persisted = original_require(record.id)
    assert reread_failures == 1
    assert persisted.status == MeetingImportStatus.RECEIVED
    assert (harness.storage_root / persisted.original_relative_path).read_bytes() == b"abcd"
    assert harness.upload_tasks == {}


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_failure_settlement_or_staging(harness):
    """Failure broadcast may suspend; cleanup still owns the request to completion."""
    record = harness.create_record(expected_bytes=4)
    body_started = asyncio.Event()
    body_release = asyncio.Event()
    failure_broadcast_started = asyncio.Event()
    failure_broadcast_release = asyncio.Event()

    async def streaming_body():
        yield b"ab"
        body_started.set()
        await body_release.wait()
        yield b"cd"

    async def blocking_broadcast(current, progress: float, status: str) -> None:
        harness.broadcasts.append((current.id, progress, status))
        if status == "Meeting import upload failed":
            failure_broadcast_started.set()
            await failure_broadcast_release.wait()

    harness._broadcast = blocking_broadcast
    client = await _client(harness)
    request_task = asyncio.create_task(client.put(f"/api/meeting-imports/{record.id}/content", data=streaming_body()))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=2.0)
        staging = harness.storage_root / "meeting-imports" / record.id / "source.part"

        async def active_handler() -> asyncio.Task[web.Response]:
            while not staging.exists() or record.id not in harness.upload_tasks:
                await asyncio.sleep(0.01)
            return harness.upload_tasks[record.id]

        handler_task = await asyncio.wait_for(active_handler(), timeout=2.0)
        handler_task.cancel()
        await asyncio.wait_for(failure_broadcast_started.wait(), timeout=2.0)

        for _ in range(2):
            handler_task.cancel()
            await asyncio.sleep(0)

        assert not handler_task.done()
        assert staging.exists()
    finally:
        body_release.set()
        failure_broadcast_release.set()
        with suppress(BaseException):
            await request_task
        await client.close()

    assert harness.store.require(record.id).status == MeetingImportStatus.FAILED
    assert not (harness.storage_root / "meeting-imports" / record.id).exists()


@pytest.mark.asyncio
async def test_an_accepted_upload_survives_a_scheduling_failure(harness):
    """The source is committed; the bookkeeping is repaired by startup recovery."""
    record = harness.create_record(expected_bytes=4)
    harness.schedule_error = RuntimeError("event loop is closed")
    client = await _client(harness)
    try:
        response = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd")
        assert response.status == 202
    finally:
        await client.close()

    assert harness.store.require(record.id).status == MeetingImportStatus.RECEIVED


@pytest.mark.asyncio
async def test_a_body_that_does_not_match_the_declared_size_fails_the_job(harness):
    record = harness.create_record(expected_bytes=16)
    client = await _client(harness)
    try:
        response = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"short")
        assert response.status == 409
    finally:
        await client.close()

    assert harness.store.require(record.id).status == MeetingImportStatus.FAILED
    assert not (harness.storage_root / "meeting-imports" / record.id).exists()


@pytest.mark.asyncio
async def test_a_replayed_upload_reports_the_committed_job_instead_of_a_conflict(harness):
    record = harness.create_record(expected_bytes=4)
    client = await _client(harness)
    try:
        assert (await client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd")).status == 202

        replayed = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"zzzz")
        assert replayed.status == 202
    finally:
        await client.close()

    unchanged = harness.store.require(record.id)
    assert unchanged.status == MeetingImportStatus.RECEIVED
    assert (harness.storage_root / unchanged.original_relative_path).read_bytes() == b"abcd"


@pytest.mark.asyncio
async def test_a_second_concurrent_upload_is_refused_while_the_first_still_owns_the_job(harness):
    record = harness.create_record(expected_bytes=4)
    blocked = asyncio.get_running_loop().create_future()
    harness.upload_tasks[record.id] = asyncio.ensure_future(blocked)
    client = await _client(harness)
    try:
        response = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd")
        assert response.status == 409
        assert "already active" in (await response.json())["message"]
    finally:
        await client.close()
        harness.upload_tasks[record.id].cancel()

    # The refused request must not have touched the job's durable state.
    assert harness.store.require(record.id).status == MeetingImportStatus.CREATED


@pytest.mark.asyncio
async def test_uploading_to_an_unknown_job_is_a_404(harness):
    client = await _client(harness)
    try:
        assert (await client.put("/api/meeting-imports/does-not-exist/content", data=b"abcd")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upload_reports_progress_once_per_megabyte(harness):
    record = harness.create_record(expected_bytes=3 * _MEGABYTE)
    client = await _client(harness)
    try:
        response = await client.put(f"/api/meeting-imports/{record.id}/content", data=b"x" * (3 * _MEGABYTE))
        assert response.status == 202
    finally:
        await client.close()

    uploading = [progress for _id, progress, status in harness.broadcasts if status == "Uploading recording"]
    # How many chunks the body arrives in is the transport's business, so the
    # contract is the reporting interval rather than a report count: progress
    # only moves forward, and never by less than the megabyte that triggers it.
    assert uploading
    assert uploading == sorted(uploading)
    steps = [later - earlier for earlier, later in pairwise(uploading)]
    minimum_step = 1 / 3 * 0.85
    assert all(step >= minimum_step or isclose(step, minimum_step, rel_tol=0.0, abs_tol=1e-12) for step in steps)
    # 0.85 is reserved for "upload finished"; streaming progress never passes it.
    assert uploading[-1] <= 0.85


@pytest.mark.asyncio
async def test_cancel_finishes_a_job_that_has_no_running_task(harness):
    record = harness.create_record()
    client = await _client(harness)
    try:
        assert (await client.delete(f"/api/meeting-imports/{record.id}")).status == 200
    finally:
        await client.close()

    assert harness.store.require(record.id).status == MeetingImportStatus.CANCELED
    assert harness.broadcasts[-1][2] == "Meeting import canceled"


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_committed_cancel_cleanup(harness, monkeypatch):
    record = harness.create_record()
    staging = harness.storage_root / "meeting-imports" / record.id
    staging.mkdir(parents=True)
    (staging / "source.part").write_bytes(b"private audio")
    mark_started = threading.Event()
    release_mark = threading.Event()
    original_mark_canceled = harness.store.mark_canceled

    def blocked_mark_canceled(import_id: str) -> MeetingImportRecord:
        mark_started.set()
        if not release_mark.wait(timeout=3.0):
            raise TimeoutError("test did not release the canceled-state commit")
        return original_mark_canceled(import_id)

    monkeypatch.setattr(harness.store, "mark_canceled", blocked_mark_canceled)
    client = await _client(harness)
    task = asyncio.create_task(
        meeting_import_routes.cancel_import(
            SimpleNamespace(
                app=client.app,
                match_info={"importId": record.id},
            )
        )
    )
    try:
        assert await asyncio.to_thread(mark_started.wait, 2.0)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert task.done() is False
        assert staging.exists()
    finally:
        release_mark.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()

    assert harness.store.require(record.id).status == MeetingImportStatus.CANCELED
    assert not staging.exists()
    assert harness.broadcasts[-1][2] == "Meeting import canceled"


@pytest.mark.asyncio
async def test_cancel_stops_a_running_processing_task_and_removes_its_staging(harness):
    record = harness.create_record()
    staging = harness.storage_root / "meeting-imports" / record.id
    staging.mkdir(parents=True)
    (staging / "source.part").write_bytes(b"partial")

    async def never_finishes() -> None:
        await asyncio.Event().wait()

    harness.processing_tasks[record.id] = asyncio.create_task(never_finishes())
    client = await _client(harness)
    try:
        assert (await client.delete(f"/api/meeting-imports/{record.id}")).status == 200
    finally:
        await client.close()

    assert harness.processing_tasks[record.id].cancelled()
    assert not staging.exists()


@pytest.mark.asyncio
async def test_cancel_reports_an_unknown_job(harness):
    client = await _client(harness)
    try:
        assert (await client.delete("/api/meeting-imports/does-not-exist")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_of_a_finished_job_hands_back_the_meeting_it_produced(harness):
    record = harness.create_record(expected_bytes=4)
    client = await _client(harness)
    try:
        await client.put(f"/api/meeting-imports/{record.id}/content", data=b"abcd")
        harness.complete(record.id, meeting_id="meeting-42")

        response = await client.delete(f"/api/meeting-imports/{record.id}")
        assert response.status == 409
        body = await response.json()
        assert body["meetingId"] == "meeting-42"
        assert "already finished" in body["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_legacy_multipart_import_answers_with_the_durable_replacement(harness):
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/import", data=b"")
        assert response.status == 410
        body = await response.json()
        assert body["createUrl"] == "/api/meeting-imports"
        assert "POST /api/meeting-imports" in body["message"]
    finally:
        await client.close()


def test_the_deps_bundle_stays_immutable(harness):
    """Handlers receive a snapshot; they must not reconfigure the domain."""
    deps = harness.deps()

    with pytest.raises(FrozenInstanceError):
        deps.store = object()


def test_the_store_port_matches_the_real_meeting_import_store(assert_protocol_contract):
    """The route domain names exactly the durable operations it consumes."""
    assert_protocol_contract(
        MeetingImportStorePort,
        MeetingImportStore,
        methods={
            "begin_receiving",
            "create",
            "list_inbox",
            "mark_canceled",
            "mark_failed",
            "mark_received",
            "request_cancel",
            "require",
            "update_receive_progress",
        },
        returns={
            "begin_receiving": MeetingImportRecord,
            "create": MeetingImportRecord,
            "list_inbox": list[MeetingImportRecord],
            "mark_canceled": MeetingImportRecord,
            "mark_failed": MeetingImportRecord,
            "mark_received": MeetingImportRecord,
            "request_cancel": MeetingImportRecord,
            "require": MeetingImportRecord,
            "update_receive_progress": MeetingImportRecord,
        },
    )


@pytest.mark.asyncio
async def test_create_app_serves_an_import_through_the_real_controller_adapter(tmp_path, monkeypatch):
    """Exercise lazy composition through HTTP, not by inspecting non-null fields."""
    from src import web_api
    from src.web_api import ScriberWebController

    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    store = MeetingImportStore(tmp_path / "composition-imports.db")
    record = store.create(
        source_filename="architecture-review.wav",
        expected_bytes=8,
        profile_snapshot={"id": "default", "language": "de"},
        metadata={"title": "Architecture review"},
    )
    controller = object.__new__(ScriberWebController)
    controller._meeting_import_store = store
    controller._shutting_down = False

    app = web_api.create_app(controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/api/meeting-imports/{record.id}")
        assert response.status == 200
        body = await response.json()
        assert body["id"] == record.id
        assert body["sourceFilename"] == "architecture-review.wav"

        deps = app[APP_MEETING_IMPORT_SERVICE].deps()
        assert deps.store is store
        assert deps.storage_root == tmp_path
        # The registries are created lazily and attached to the real controller,
        # so upload and cancellation requests rendezvous in the same mappings.
        assert controller._meeting_import_upload_tasks is deps.upload_tasks
        assert controller._meeting_import_tasks is deps.processing_tasks
    finally:
        await client.close()
        store.close()

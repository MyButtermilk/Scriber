"""Meeting import routes exercised without web_api.create_app.

The durable protocol is driven against the real ``MeetingImportStore`` rather
than a hand-written double: the interesting behaviour here is which state a job
is left in, and a fake store would only assert that the module calls the methods
this module already calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_import_routes import MeetingImportDeps, register_meeting_import_routes
from src.data.meeting_import_store import MeetingImportStatus, MeetingImportStore

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
        self.processing_tasks: dict[str, asyncio.Task] = {}
        self.upload_tasks: dict[str, asyncio.Task] = {}
        self.broadcasts: list[tuple[str, float, str]] = []
        self.scheduled: list[str] = []
        self.shutting_down = False
        self.provider_error: Exception | None = None
        self.schedule_error: Exception | None = None
        self.audio_limit = 32 * _MEGABYTE
        self.video_limit = 64 * _MEGABYTE

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
        return MeetingImportDeps(
            store=self.store,
            broadcast=self._broadcast,
            schedule=self._schedule,
            processing_tasks=self.processing_tasks,
            upload_tasks=self.upload_tasks,
            storage_root=self.storage_root,
            is_shutting_down=lambda: self.shutting_down,
            validate_provider_ready=self._validate_provider_ready,
            audio_max_bytes=lambda _provider: self.audio_limit,
            video_max_bytes=lambda: self.video_limit,
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
    client = await _client(harness)
    try:
        too_large_audio = await client.post("/api/meeting-imports", json={"filename": "a.wav", "byteSize": 2048})
        assert too_large_audio.status == 413
        assert "too large" in (await too_large_audio.json())["message"]

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
    assert all(step >= 1 / 3 * 0.85 for step in steps)
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


def test_create_app_supplies_every_dependency_the_protocol_declares(tmp_path, monkeypatch):
    """Guard the wiring: the bundle is assembled by attribute name in create_app.

    Nothing checks that assembly at import time, so a renamed controller
    attribute would only surface as a request-time AttributeError, in whichever
    suite happens to exercise the one route that needs it.
    """
    from types import SimpleNamespace

    from src import web_api
    from src.api.meeting_import_routes import APP_MEETING_IMPORT_SERVICE

    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    controller = SimpleNamespace(
        _meeting_import_store=object(),
        _broadcast_meeting_import=lambda *_args: None,
        schedule_meeting_import=lambda _import_id: True,
    )

    app = web_api.create_app(controller)
    deps = app[APP_MEETING_IMPORT_SERVICE].deps()

    assert all(getattr(deps, field.name) is not None for field in fields(MeetingImportDeps))
    assert deps.storage_root == tmp_path
    # The registries are created on demand and attached, so the upload and the
    # cancellation racing it meet in the same dict.
    assert controller._meeting_import_upload_tasks is deps.upload_tasks
    assert controller._meeting_import_tasks is deps.processing_tasks

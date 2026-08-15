"""Meeting capture transport exercised through its public HTTP seam."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_capture_routes import (
    MeetingCaptureControllerPort,
    MeetingCaptureOutcome,
    MeetingStartCommand,
    register_meeting_capture_routes,
)


class _InMemoryMeetingCaptureControl:
    def __init__(self) -> None:
        self.started: MeetingStartCommand | None = None
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.stopped: list[str] = []

    async def start_meeting_capture(self, command: MeetingStartCommand) -> MeetingCaptureOutcome:
        self.started = command
        return MeetingCaptureOutcome(
            status=201,
            payload={"id": "meeting-1", "state": "recording", "apiVersion": "1"},
        )

    async def pause_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome:
        self.paused.append(meeting_id)
        return MeetingCaptureOutcome(
            status=200,
            payload={"id": meeting_id, "state": "paused", "apiVersion": "1"},
        )

    async def resume_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome:
        self.resumed.append(meeting_id)
        return MeetingCaptureOutcome(
            status=200,
            payload={"id": meeting_id, "state": "recording", "apiVersion": "1"},
        )

    async def stop_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome:
        self.stopped.append(meeting_id)
        return MeetingCaptureOutcome(
            status=202,
            payload={"id": meeting_id, "state": "finalizing", "apiVersion": "1"},
        )


@pytest.mark.asyncio
async def test_start_normalizes_one_immutable_command_before_capture() -> None:
    control = _InMemoryMeetingCaptureControl()
    app = web.Application()
    register_meeting_capture_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings",
            json={
                "title": "  Architecture review  ",
                "microphoneDeviceId": "  mic-1  ",
                "renderNativeEndpointIdHash": "  render-hash  ",
            },
        )
        assert response.status == 201
        assert await response.json() == {
            "id": "meeting-1",
            "state": "recording",
            "apiVersion": "1",
        }
    finally:
        await client.close()

    assert control.started is not None
    assert control.started.title == "Architecture review"
    assert control.started.microphone_device_id == "mic-1"
    assert control.started.render_native_endpoint_id_hash == "render-hash"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "title",
        "language",
        "transcriptionMode",
        "liveProvider",
        "finalProvider",
        "analysisModel",
        "microphoneDeviceId",
        "renderDeviceId",
        "microphoneNativeEndpointIdHash",
        "renderNativeEndpointIdHash",
        "calendarEventId",
    ],
)
async def test_start_rejects_non_string_capture_fields_before_admission(field: str) -> None:
    control = _InMemoryMeetingCaptureControl()
    app = web.Application()
    register_meeting_capture_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings", json={field: ["not", "a", "string"]})
        assert response.status == 400
        assert await response.json() == {"message": "Invalid meeting capture payload."}
    finally:
        await client.close()

    assert control.started is None


@pytest.mark.asyncio
async def test_pause_routes_one_meeting_through_the_capture_control() -> None:
    control = _InMemoryMeetingCaptureControl()
    app = web.Application()
    register_meeting_capture_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings/meeting-1/pause")
        assert response.status == 200
        assert await response.json() == {
            "id": "meeting-1",
            "state": "paused",
            "apiVersion": "1",
        }
    finally:
        await client.close()

    assert control.paused == ["meeting-1"]


@pytest.mark.asyncio
async def test_resume_routes_one_meeting_through_the_capture_control() -> None:
    control = _InMemoryMeetingCaptureControl()
    app = web.Application()
    register_meeting_capture_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings/meeting-1/resume")
        assert response.status == 200
        assert await response.json() == {
            "id": "meeting-1",
            "state": "recording",
            "apiVersion": "1",
        }
    finally:
        await client.close()

    assert control.resumed == ["meeting-1"]


@pytest.mark.asyncio
async def test_stop_routes_one_meeting_through_the_capture_control() -> None:
    control = _InMemoryMeetingCaptureControl()
    app = web.Application()
    register_meeting_capture_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings/meeting-1/stop")
        assert response.status == 202
        assert await response.json() == {
            "id": "meeting-1",
            "state": "finalizing",
            "apiVersion": "1",
        }
    finally:
        await client.close()

    assert control.stopped == ["meeting-1"]


def test_controller_adapter_matches_the_meeting_capture_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        MeetingCaptureControllerPort,
        ScriberWebController,
        methods={
            "pause_meeting_capture",
            "resume_meeting_capture",
            "start_meeting_capture",
            "stop_meeting_capture",
        },
        returns={
            "pause_meeting_capture": MeetingCaptureOutcome,
            "resume_meeting_capture": MeetingCaptureOutcome,
            "start_meeting_capture": MeetingCaptureOutcome,
            "stop_meeting_capture": MeetingCaptureOutcome,
        },
    )


def test_create_app_wires_meeting_lifecycle_to_the_capture_domain() -> None:
    from src.web_api import ScriberWebController, create_app

    app = create_app(object.__new__(ScriberWebController))
    handlers = {
        route.resource.canonical: route.handler.__module__
        for route in app.router.routes()
        if route.method == "POST"
        and route.resource.canonical
        in {
            "/api/meetings",
            "/api/meetings/{id}/pause",
            "/api/meetings/{id}/resume",
            "/api/meetings/{id}/stop",
        }
    }

    assert handlers == {
        "/api/meetings": "src.api.meeting_capture_routes",
        "/api/meetings/{id}/pause": "src.api.meeting_capture_routes",
        "/api/meetings/{id}/resume": "src.api.meeting_capture_routes",
        "/api/meetings/{id}/stop": "src.api.meeting_capture_routes",
    }

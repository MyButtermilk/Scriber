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

    async def start_meeting_capture(self, command: MeetingStartCommand) -> MeetingCaptureOutcome:
        self.started = command
        return MeetingCaptureOutcome(
            status=201,
            payload={"id": "meeting-1", "state": "recording", "apiVersion": "1"},
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


def test_controller_adapter_matches_the_meeting_capture_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        MeetingCaptureControllerPort,
        ScriberWebController,
        methods={"start_meeting_capture"},
        returns={"start_meeting_capture": MeetingCaptureOutcome},
    )


def test_create_app_wires_meeting_start_to_the_capture_domain() -> None:
    from src.web_api import ScriberWebController, create_app

    app = create_app(object.__new__(ScriberWebController))
    route = next(
        route for route in app.router.routes() if route.method == "POST" and route.resource.canonical == "/api/meetings"
    )

    assert route.handler.__module__ == "src.api.meeting_capture_routes"

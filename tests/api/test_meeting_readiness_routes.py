"""Meeting device readiness exercised through its public HTTP seam."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_readiness_routes import (
    MeetingDeviceTestCommand,
    MeetingReadinessControllerPort,
    MeetingReadinessOutcome,
    register_meeting_readiness_routes,
)


class _InMemoryMeetingReadinessControl:
    def __init__(self) -> None:
        self.device_test_command: MeetingDeviceTestCommand | None = None
        self.capabilities_requested = 0
        self.audio_devices_requested = 0

    async def get_meeting_capabilities(self) -> MeetingReadinessOutcome:
        self.capabilities_requested += 1
        return MeetingReadinessOutcome(
            status=200,
            payload={"apiVersion": "1", "nativeMeetingCapture": True},
        )

    async def run_meeting_device_test(
        self,
        command: MeetingDeviceTestCommand,
    ) -> MeetingReadinessOutcome:
        self.device_test_command = command
        return MeetingReadinessOutcome(
            status=200,
            payload={
                "apiVersion": "1",
                "available": True,
                "durationMs": command.duration_ms,
            },
        )

    async def list_meeting_audio_devices(self) -> MeetingReadinessOutcome:
        self.audio_devices_requested += 1
        return MeetingReadinessOutcome(
            status=200,
            payload={"apiVersion": "1", "capture": [], "render": []},
        )


@pytest.mark.asyncio
async def test_device_test_parses_one_strict_command_before_native_admission() -> None:
    control = _InMemoryMeetingReadinessControl()
    app = web.Application()
    register_meeting_readiness_routes(
        app,
        control=control,
        max_device_test_duration_ms=lambda: 5_000,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings/device-test",
            json={
                "durationMs": 900,
                "microphoneNativeEndpointIdHash": "  microphone-hash  ",
                "renderNativeEndpointIdHash": "  render-hash  ",
                "aecEnabled": False,
                "playTestTone": True,
            },
        )
        assert response.status == 200
        assert await response.json() == {
            "apiVersion": "1",
            "available": True,
            "durationMs": 900,
        }
    finally:
        await client.close()

    assert control.device_test_command == MeetingDeviceTestCommand(
        duration_ms=900,
        microphone_native_endpoint_id_hash="microphone-hash",
        render_native_endpoint_id_hash="render-hash",
        aec_enabled=False,
        play_test_tone=True,
    )


@pytest.mark.asyncio
async def test_capabilities_route_uses_the_readiness_control() -> None:
    control = _InMemoryMeetingReadinessControl()
    app = web.Application()
    register_meeting_readiness_routes(
        app,
        control=control,
        max_device_test_duration_ms=lambda: 5_000,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/meetings/capabilities")
        assert response.status == 200
        assert await response.json() == {
            "apiVersion": "1",
            "nativeMeetingCapture": True,
        }
    finally:
        await client.close()

    assert control.capabilities_requested == 1


@pytest.mark.asyncio
async def test_audio_devices_route_uses_the_readiness_control() -> None:
    control = _InMemoryMeetingReadinessControl()
    app = web.Application()
    register_meeting_readiness_routes(
        app,
        control=control,
        max_device_test_duration_ms=lambda: 5_000,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/meetings/audio-devices")
        assert response.status == 200
        assert await response.json() == {
            "apiVersion": "1",
            "capture": [],
            "render": [],
        }
    finally:
        await client.close()

    assert control.audio_devices_requested == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"durationMs": True},
        {"durationMs": "900"},
        {"microphoneNativeEndpointIdHash": ["microphone"]},
        {"renderNativeEndpointIdHash": {"hash": "render"}},
        {"aecEnabled": 1},
        {"playTestTone": "yes"},
    ],
)
@pytest.mark.asyncio
async def test_device_test_rejects_non_strict_fields_before_control(payload) -> None:
    control = _InMemoryMeetingReadinessControl()
    app = web.Application()
    register_meeting_readiness_routes(
        app,
        control=control,
        max_device_test_duration_ms=lambda: 5_000,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings/device-test", json=payload)
        assert response.status == 400
    finally:
        await client.close()

    assert control.device_test_command is None


def test_controller_adapter_matches_the_readiness_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        MeetingReadinessControllerPort,
        ScriberWebController,
        methods={
            "get_meeting_capabilities",
            "list_meeting_audio_devices",
            "run_meeting_device_test",
        },
        returns={
            "get_meeting_capabilities": MeetingReadinessOutcome,
            "list_meeting_audio_devices": MeetingReadinessOutcome,
            "run_meeting_device_test": MeetingReadinessOutcome,
        },
    )


def test_create_app_wires_readiness_routes_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    expected = {
        ("GET", "/api/meetings/capabilities"),
        ("GET", "/api/meetings/audio-devices"),
        ("POST", "/api/meetings/device-test"),
    }
    app = create_app(object.__new__(ScriberWebController))
    handlers: Mapping[tuple[str, str], str] = {
        (route.method, route.resource.canonical): route.handler.__module__
        for route in app.router.routes()
        if (route.method, route.resource.canonical) in expected
    }

    assert handlers == {item: "src.api.meeting_readiness_routes" for item in expected}

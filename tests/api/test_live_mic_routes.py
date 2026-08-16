"""Live Mic transport exercised through its public HTTP seam."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.live_mic_routes import (
    LiveMicControllerPort,
    LiveMicOutcome,
    LiveMicReplayPort,
    LiveMicStartCommand,
    register_live_mic_routes,
)


class _InMemoryLiveMicControl:
    def __init__(self) -> None:
        self.started: list[LiveMicStartCommand] = []
        self.toggle_outcome: LiveMicOutcome | None = None
        self.stop_count = 0
        self.stop_request_count = 0

    async def start_live_mic(self, command: LiveMicStartCommand) -> LiveMicOutcome:
        self.started.append(command)
        return LiveMicOutcome(status=200, payload={"listening": True, "apiVersion": "1"})

    async def resolve_live_mic_toggle(self) -> LiveMicOutcome | None:
        return self.toggle_outcome

    async def stop_live_mic(self) -> LiveMicOutcome:
        self.stop_count += 1
        return LiveMicOutcome(status=200, payload={"listening": False, "apiVersion": "1"})

    def request_live_mic_stop(self) -> LiveMicOutcome:
        self.stop_request_count += 1
        return LiveMicOutcome(status=202, payload={"stopAccepted": True, "apiVersion": "1"})


class _InMemoryLiveMicReplay:
    @property
    def pending_activation(self) -> bool:
        return False

    async def activate(self, marker: Mapping[str, Any]) -> LiveMicOutcome:
        raise AssertionError(f"unexpected replay activation: {marker}")


@pytest.mark.asyncio
async def test_standard_start_routes_one_immutable_command_to_live_mic_control() -> None:
    control = _InMemoryLiveMicControl()
    app = web.Application()
    register_live_mic_routes(app, control=control, replay=_InMemoryLiveMicReplay())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/live-mic/start")
        assert response.status == 200
        assert await response.json() == {"listening": True, "apiVersion": "1"}
    finally:
        await client.close()

    assert control.started == [
        LiveMicStartCommand(
            post_process=False,
            tauri_hotkey_marker=None,
            provider_replay_activation=False,
        )
    ]


def test_controller_adapter_matches_the_live_mic_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        LiveMicControllerPort,
        ScriberWebController,
        methods={
            "request_live_mic_stop",
            "resolve_live_mic_toggle",
            "start_live_mic",
            "stop_live_mic",
        },
        returns={
            "request_live_mic_stop": LiveMicOutcome,
            "resolve_live_mic_toggle": LiveMicOutcome | None,
            "start_live_mic": LiveMicOutcome,
            "stop_live_mic": LiveMicOutcome,
        },
    )


def test_replay_adapter_matches_the_live_mic_replay_port(assert_protocol_contract) -> None:
    from src.web_api import _LiveMicReplayAdapter

    assert_protocol_contract(
        LiveMicReplayPort,
        _LiveMicReplayAdapter,
        methods={"activate"},
        properties={"pending_activation"},
        property_returns={"pending_activation": bool},
        returns={"activate": LiveMicOutcome},
    )


@pytest.mark.asyncio
async def test_remaining_live_mic_routes_use_the_public_control() -> None:
    control = _InMemoryLiveMicControl()
    app = web.Application()
    register_live_mic_routes(app, control=control, replay=_InMemoryLiveMicReplay())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        post_processing = await client.post("/api/live-mic/start-post-processing")
        assert post_processing.status == 200

        control.toggle_outcome = LiveMicOutcome(
            status=202,
            payload={"stopAccepted": True, "finalizing": True},
        )
        toggle = await client.post("/api/live-mic/toggle", data=b"not-json")
        assert toggle.status == 202
        assert await toggle.json() == {"stopAccepted": True, "finalizing": True}

        stopped = await client.post("/api/live-mic/stop")
        assert stopped.status == 200
        stop_requested = await client.post("/api/live-mic/stop-request")
        assert stop_requested.status == 202

        control.toggle_outcome = None
        toggle_post_processing = await client.post("/api/live-mic/toggle-post-processing")
        assert toggle_post_processing.status == 200
    finally:
        await client.close()

    assert [command.post_process for command in control.started] == [True, True]
    assert control.stop_count == 1
    assert control.stop_request_count == 1


def test_create_app_wires_all_live_mic_handlers_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    app = create_app(object.__new__(ScriberWebController))
    paths = {
        "/api/live-mic/start",
        "/api/live-mic/start-post-processing",
        "/api/live-mic/stop",
        "/api/live-mic/stop-request",
        "/api/live-mic/toggle",
        "/api/live-mic/toggle-post-processing",
    }
    handlers = {
        route.resource.canonical: route.handler.__module__
        for route in app.router.routes()
        if route.method == "POST" and route.resource.canonical in paths
    }

    assert handlers == {path: "src.api.live_mic_routes" for path in paths}

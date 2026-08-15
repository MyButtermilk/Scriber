"""Public WebSocket lifecycle contract."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from src.api import websocket_routes
from src.api.http_security import origin_allowed
from src.api.websocket_routes import (
    APP_WEBSOCKET_SERVICE,
    WebSocketControllerPort,
    WebSocketRoutesService,
    register_websocket_routes,
)


def test_origin_policy_defaults_to_loopback_and_tauri(monkeypatch) -> None:
    monkeypatch.delenv("SCRIBER_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("http://localhost:3000")
    assert origin_allowed("http://127.0.0.1:1234")
    assert origin_allowed("http://[::1]:5173")
    assert origin_allowed("http://tauri.localhost")
    assert origin_allowed("https://tauri.localhost")
    assert origin_allowed("tauri://localhost")
    assert not origin_allowed("https://evil.example")
    assert not origin_allowed("http://evil.localhost")
    assert not origin_allowed("null")


def test_origin_policy_reads_the_current_environment(monkeypatch) -> None:
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "https://example.com, http://localhost:3000")
    assert origin_allowed("https://example.com")
    assert origin_allowed("http://localhost:3000")
    assert not origin_allowed("http://localhost:4000")

    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "https://changed.example")
    assert origin_allowed("https://changed.example")
    assert not origin_allowed("https://example.com")


def test_origin_policy_allows_an_explicit_wildcard(monkeypatch) -> None:
    monkeypatch.setenv("SCRIBER_ALLOWED_ORIGINS", "*")
    assert origin_allowed("https://any.example")


class _Controller:
    def __init__(self, *, initial_send_succeeds: bool = True) -> None:
        self.initial_send_succeeds = initial_send_succeeds
        self.added: list[web.WebSocketResponse] = []
        self.removed: list[web.WebSocketResponse] = []
        self.sent: list[str] = []

    async def add_client(self, ws: web.WebSocketResponse) -> None:
        self.added.append(ws)

    async def remove_client(self, ws: web.WebSocketResponse) -> None:
        self.removed.append(ws)

    async def send_client_text(self, ws: web.WebSocketResponse, message: str) -> bool:
        self.sent.append(message)
        if len(self.sent) == 1 and not self.initial_send_succeeds:
            return False
        await ws.send_str(message)
        return True

    def get_state(self) -> dict[str, Any]:
        return {"status": "idle", "mode": "transcribe"}


async def _client(controller: _Controller) -> TestClient:
    app = web.Application()
    register_websocket_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_websocket_sends_initial_state_answers_ping_and_removes_client() -> None:
    controller = _Controller()
    client = await _client(controller)
    try:
        websocket = await client.ws_connect("/ws", headers={"Origin": "http://tauri.localhost"})
        initial = await websocket.receive_json()
        await websocket.send_str("ping")
        pong = await websocket.receive_str()
        await websocket.close()
    finally:
        await client.close()

    assert initial["type"] == "state"
    assert initial["status"] == "idle"
    assert initial["mode"] == "transcribe"
    assert pong == "pong"
    assert len(controller.added) == 1
    assert controller.removed == controller.added
    assert json.loads(controller.sent[0]) == initial


@pytest.mark.asyncio
async def test_websocket_rejects_untrusted_origin_before_registration() -> None:
    controller = _Controller()
    client = await _client(controller)
    try:
        with pytest.raises(WSServerHandshakeError) as handshake:
            await client.ws_connect("/ws", headers={"Origin": "https://evil.example"})
    finally:
        await client.close()

    assert handshake.value.status == 403
    assert controller.added == []
    assert controller.removed == []


@pytest.mark.asyncio
async def test_failed_initial_send_still_removes_registered_client() -> None:
    controller = _Controller(initial_send_succeeds=False)
    client = await _client(controller)
    try:
        websocket = await client.ws_connect("/ws")
        await websocket.receive()
        await websocket.close()
    finally:
        await client.close()

    assert len(controller.added) == 1
    assert controller.removed == controller.added


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_registered_client_removal(monkeypatch) -> None:
    receive_started = asyncio.Event()
    remove_started = asyncio.Event()
    allow_remove = asyncio.Event()

    class FakeWebSocket:
        async def prepare(self, _request) -> None:
            return None

        async def send_str(self, _message: str) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            receive_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class BlockingRemovalController(_Controller):
        async def remove_client(self, ws: web.WebSocketResponse) -> None:
            remove_started.set()
            await allow_remove.wait()
            self.removed.append(ws)

    fake_websocket = FakeWebSocket()
    controller = BlockingRemovalController()
    request = SimpleNamespace(
        headers={},
        app={APP_WEBSOCKET_SERVICE: WebSocketRoutesService(controller=controller)},
    )
    monkeypatch.setattr(
        websocket_routes.web,
        "WebSocketResponse",
        lambda *, heartbeat: fake_websocket,
    )

    handler_task = asyncio.create_task(websocket_routes.websocket_handler(request))
    await receive_started.wait()
    handler_task.cancel()
    await remove_started.wait()
    handler_task.cancel()
    await asyncio.sleep(0)
    assert not handler_task.done()

    allow_remove.set()
    with pytest.raises(asyncio.CancelledError):
        await handler_task

    assert controller.removed == [fake_websocket]


def test_websocket_port_matches_the_production_controller(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        WebSocketControllerPort,
        ScriberWebController,
        methods={"add_client", "remove_client", "send_client_text", "get_state"},
        returns={"send_client_text": bool, "get_state": dict[str, Any]},
    )

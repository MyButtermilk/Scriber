"""Device and autostart routes exercised without web_api.create_app."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.device_routes import register_device_routes


class _StubController:
    def __init__(self) -> None:
        self.refresh_hints: list[dict | None] = []

    def list_microphones(self) -> list[dict[str, str]]:
        return [{"id": "mic-1", "name": "Headset"}]

    def request_microphone_refresh(self, hint_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.refresh_hints.append(hint_payload)
        return {"scheduled": True}


async def _client(controller: _StubController) -> TestClient:
    app = web.Application()
    register_device_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_autostart_is_reported_unavailable_and_rejects_mutation():
    client = await _client(_StubController())
    try:
        read = await client.get("/api/autostart")
        assert read.status == 200
        assert await read.json() == {
            "enabled": False,
            "available": False,
            "message": "Desktop autostart is managed by the Tauri shell",
        }

        write = await client.post("/api/autostart")
        assert write.status == 409
        assert (await write.json())["available"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_autostart_contract_cannot_be_mutated_between_requests():
    """The handler answers from a copy, so one response cannot poison the next."""
    client = await _client(_StubController())
    try:
        first = await (await client.get("/api/autostart")).json()
        first["enabled"] = True
        second = await (await client.get("/api/autostart")).json()
        assert second["enabled"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_microphones_are_listed_from_the_controller():
    client = await _client(_StubController())
    try:
        payload = await (await client.get("/api/microphones")).json()
        assert payload == {"devices": [{"id": "mic-1", "name": "Headset"}]}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_refresh_accepts_an_empty_body_as_no_hint():
    controller = _StubController()
    client = await _client(controller)
    try:
        response = await client.post("/api/microphones/refresh")
        assert response.status == 200
        assert (await response.json())["scheduled"] is True
    finally:
        await client.close()
    assert controller.refresh_hints == [None]


@pytest.mark.asyncio
async def test_refresh_passes_a_hint_through_and_rejects_malformed_bodies():
    controller = _StubController()
    client = await _client(controller)
    try:
        assert (await client.post("/api/microphones/refresh", json={"reason": "device_change"})).status == 200

        invalid = await client.post("/api/microphones/refresh", data="not json")
        assert invalid.status == 400
        assert (await invalid.json())["message"] == "Invalid JSON"

        not_object = await client.post("/api/microphones/refresh", json=["nope"])
        assert not_object.status == 400
        assert (await not_object.json())["message"] == "Expected JSON object"
    finally:
        await client.close()
    assert controller.refresh_hints == [{"reason": "device_change"}]

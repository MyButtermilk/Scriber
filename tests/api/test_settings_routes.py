"""Settings routes exercised without web_api.create_app."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.settings_routes import SettingsControllerPort, register_settings_routes


class _StubController:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.update_error: Exception | None = None

    def get_settings(self) -> dict[str, Any]:
        return {"language": "de"}

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.update_error is not None:
            raise self.update_error
        self.received.append(payload)
        return {"language": payload.get("language", "de")}


async def _client(controller: _StubController) -> TestClient:
    app = web.Application()
    register_settings_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_get_returns_the_controller_snapshot():
    client = await _client(_StubController())
    try:
        assert await (await client.get("/api/settings")).json() == {"language": "de"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_rejects_a_body_that_is_not_json():
    client = await _client(_StubController())
    try:
        response = await client.put("/api/settings", data="not json")
        assert response.status == 400
        assert (await response.json())["message"] == "Invalid JSON"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_normalises_a_non_object_body_to_an_empty_update():
    controller = _StubController()
    client = await _client(controller)
    try:
        assert (await client.put("/api/settings", json=["nope"])).status == 200
    finally:
        await client.close()
    assert controller.received == [{}]


@pytest.mark.asyncio
async def test_put_reports_a_rejected_value_as_400_and_a_failure_as_500():
    controller = _StubController()
    client = await _client(controller)
    try:
        controller.update_error = ValueError("hotkey is already in use")
        rejected = await client.put("/api/settings", json={"hotkey": "ctrl+d"})
        assert rejected.status == 400
        assert (await rejected.json())["message"] == "hotkey is already in use"

        controller.update_error = RuntimeError("disk full")
        failed = await client.put("/api/settings", json={})
        assert failed.status == 500
        assert (await failed.json())["message"] == "disk full"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_falls_back_to_a_generic_message_for_an_empty_failure():
    controller = _StubController()
    controller.update_error = RuntimeError("")
    client = await _client(controller)
    try:
        response = await client.put("/api/settings", json={})
        assert response.status == 500
        assert (await response.json())["message"] == "Failed to update settings"
    finally:
        await client.close()


def test_controller_adapter_matches_the_settings_port(assert_protocol_contract):
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        SettingsControllerPort,
        ScriberWebController,
        methods={"get_settings", "update_settings"},
    )

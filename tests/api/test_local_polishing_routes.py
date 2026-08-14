"""Local polishing routes exercised without web_api.create_app."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.local_polishing_routes import register_local_polishing_routes
from src.local_polishing import CatalogError, LocalPolishingError


class _StubController:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    def get_local_polishing_models(self) -> dict[str, Any]:
        self._maybe_fail()
        return {"models": [{"variant": "small"}]}

    async def install_local_polishing_model(self, variant: str) -> dict[str, Any]:
        self.calls.append(("install", variant))
        self._maybe_fail()
        return {"operationId": "op-1", "variant": variant}

    async def cancel_local_polishing_operation(self, operation_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", operation_id))
        self._maybe_fail()
        return {"operationId": operation_id, "state": "cancelled"}

    async def remove_local_polishing_model(self, variant: str) -> dict[str, Any]:
        self.calls.append(("remove", variant))
        self._maybe_fail()
        return {"variant": variant, "installed": False}


async def _client(controller: _StubController) -> TestClient:
    app = web.Application()
    register_local_polishing_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_listing_models_passes_the_controller_snapshot_through():
    client = await _client(_StubController())
    try:
        payload = await (await client.get("/api/local-polishing/models")).json()
        assert payload["models"][0]["variant"] == "small"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_install_and_cancel_are_accepted_asynchronously():
    controller = _StubController()
    client = await _client(controller)
    try:
        install = await client.post("/api/local-polishing/models/small/install")
        assert install.status == 202
        assert (await install.json())["success"] is True

        cancel = await client.delete("/api/local-polishing/model-operations/op-1")
        assert cancel.status == 202

        remove = await client.delete("/api/local-polishing/models/small")
        assert remove.status == 200
    finally:
        await client.close()
    assert controller.calls == [("install", "small"), ("cancel", "op-1"), ("remove", "small")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("unknown_operation", 404),
        ("unknown_variant", 400),
        ("selected_model", 409),
        ("model_busy", 409),
        ("model_in_use", 409),
        ("closed", 503),
    ],
)
async def test_every_typed_failure_code_maps_onto_its_status(code, expected):
    controller = _StubController()
    controller.error = LocalPolishingError(code, code)
    client = await _client(controller)
    try:
        response = await client.post("/api/local-polishing/models/small/install")
        assert response.status == expected
        payload = await response.json()
        assert payload["success"] is False
        assert payload["code"] == code
        assert payload["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_missing_catalogue_is_reported_as_unavailable():
    controller = _StubController()
    controller.error = CatalogError("no catalogue in this build")
    client = await _client(controller)
    try:
        response = await client.post("/api/local-polishing/models/small/install")
        assert response.status == 503
        assert (await response.json())["code"] == "catalog_unavailable"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_unexpected_failure_never_leaks_its_detail():
    controller = _StubController()
    controller.error = RuntimeError("C:/models/secret-path exploded")
    client = await _client(controller)
    try:
        for response in (
            await client.get("/api/local-polishing/models"),
            await client.post("/api/local-polishing/models/small/install"),
            await client.delete("/api/local-polishing/models/small"),
        ):
            assert response.status == 503
            payload = await response.json()
            assert payload["code"] == "local_polishing_unavailable"
            assert "secret-path" not in payload["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_unknown_typed_code_falls_back_without_raising():
    controller = _StubController()
    controller.error = LocalPolishingError("brand_new_code", "brand new")
    client = await _client(controller)
    try:
        response = await client.delete("/api/local-polishing/models/small")
        assert response.status == 503
        assert (await response.json())["code"] == "brand_new_code"
    finally:
        await client.close()

"""ONNX model routes exercised without web_api.create_app."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.onnx_routes import APP_ONNX_SERVICE, register_onnx_routes


class _StubController:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast(self, event: dict) -> None:
        self.events.append(event)


async def _unused_download(*_args, **_kwargs):
    raise AssertionError("this test must not reach the download itself")


def _install_onnx_stub(monkeypatch, **overrides):
    """Stand in for src.onnx_stt, which the handlers import lazily."""
    defaults = {
        "is_onnx_available": lambda: True,
        "list_available_models": lambda **_kwargs: [{"id": "parakeet"}],
        "get_model_info": lambda model_id: (
            {
                "name": "Parakeet",
                "description": "",
                "languages": ["en"],
                "size_mb": 600,
            }
            if model_id == "parakeet"
            else None
        ),
        "get_model_status": lambda model_id, **_kwargs: {
            "downloaded": False,
            "status": "absent",
            "progress": 0.0,
            "message": "",
        },
        "is_model_downloading": lambda _model_id: False,
        "delete_model": lambda _model_id, **_kwargs: True,
        "download_model": _unused_download,
    }
    defaults.update(overrides)
    module = types.ModuleType("src.onnx_stt")
    for name, value in defaults.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "src.onnx_stt", module)
    return module


async def _client(controller: _StubController) -> TestClient:
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_list_models_reports_a_missing_onnx_runtime_as_unavailable(monkeypatch):
    _install_onnx_stub(monkeypatch, is_onnx_available=lambda: False)
    client = await _client(_StubController())
    try:
        payload = await (await client.get("/api/onnx/models")).json()
        assert payload["available"] is False
        assert payload["models"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_model_status_distinguishes_unknown_ids(monkeypatch):
    _install_onnx_stub(monkeypatch)
    client = await _client(_StubController())
    try:
        known = await client.get("/api/onnx/models/parakeet")
        assert known.status == 200
        assert (await known.json())["id"] == "parakeet"

        unknown = await client.get("/api/onnx/models/nope")
        assert unknown.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_refuses_to_start_twice(monkeypatch):
    _install_onnx_stub(monkeypatch, is_model_downloading=lambda _model_id: True)
    client = await _client(_StubController())
    try:
        response = await client.post("/api/onnx/download", json={"modelId": "parakeet"})
        assert response.status == 409
        assert (await response.json())["success"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_requires_a_model_id(monkeypatch):
    _install_onnx_stub(monkeypatch)
    client = await _client(_StubController())
    try:
        response = await client.post("/api/onnx/download", json={})
        assert response.status == 400
        assert (await response.json())["message"] == "Missing modelId"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_progress_broadcasts_are_held_until_they_complete(monkeypatch):
    """A progress task with no strong reference can be collected mid-flight."""
    released = asyncio.Event()

    async def fake_download(_model_id, *, quantization, on_progress):
        on_progress(42.0, "halfway")
        await released.wait()
        return True

    _install_onnx_stub(
        monkeypatch,
        download_model=fake_download,
        get_model_status=lambda _model_id, **_kwargs: {
            "downloaded": False,
            "status": "ready",
            "progress": 100.0,
            "message": "done",
        },
    )

    controller = _StubController()
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        request = asyncio.ensure_future(client.post("/api/onnx/download", json={"modelId": "parakeet"}))
        # The progress callback hops threads via call_soon_threadsafe.
        for _ in range(50):
            await asyncio.sleep(0)
            if app[APP_ONNX_SERVICE].broadcast_tasks or controller.events:
                break
        released.set()
        response = await request
        assert response.status == 200
    finally:
        await client.close()

    progress_events = [event for event in controller.events if event["progress"] == 42.0]
    assert progress_events and progress_events[0]["status"] == "downloading"
    # Every held reference is dropped once the task finishes.
    assert app[APP_ONNX_SERVICE].broadcast_tasks == set()


@pytest.mark.asyncio
async def test_delete_reports_unknown_busy_and_deleted_states(monkeypatch):
    controller = _StubController()

    _install_onnx_stub(monkeypatch)
    client = await _client(controller)
    try:
        unknown = await client.delete("/api/onnx/models/nope")
        assert unknown.status == 404

        deleted = await client.delete("/api/onnx/models/parakeet")
        assert deleted.status == 200
        assert controller.events[-1]["type"] == "onnx_models_updated"
    finally:
        await client.close()

    _install_onnx_stub(monkeypatch, is_model_downloading=lambda _model_id: True)
    client = await _client(controller)
    try:
        busy = await client.delete("/api/onnx/models/parakeet")
        assert busy.status == 409
    finally:
        await client.close()

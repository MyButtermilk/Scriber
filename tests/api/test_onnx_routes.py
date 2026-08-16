"""ONNX model routes exercised without web_api.create_app."""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.onnx_routes import APP_ONNX_SERVICE, OnnxControllerPort, register_onnx_routes


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
@pytest.mark.parametrize("payload", [[], "parakeet", 7])
async def test_download_rejects_json_values_that_are_not_objects(monkeypatch, payload):
    _install_onnx_stub(monkeypatch)
    client = await _client(_StubController())
    try:
        response = await client.post("/api/onnx/download", json=payload)
        assert response.status == 400
        assert (await response.json())["message"] == "JSON body must be an object"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_progress_broadcasts_are_held_until_they_complete(monkeypatch):
    """Progress must finish before the authoritative final status is emitted."""
    progress_started = asyncio.Event()
    release_progress = asyncio.Event()

    class BlockingController(_StubController):
        async def broadcast(self, event: dict) -> None:
            if event["progress"] == 42.0:
                progress_started.set()
                await release_progress.wait()
            self.events.append(event)

    async def fake_download(_model_id, *, quantization, on_progress):
        on_progress(42.0, "halfway")
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

    controller = BlockingController()
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        request = asyncio.ensure_future(client.post("/api/onnx/download", json={"modelId": "parakeet"}))
        await asyncio.wait_for(progress_started.wait(), timeout=1.0)
        service = app[APP_ONNX_SERVICE]
        assert len(service.progress_scopes) == 1
        assert sum(scope.pending_count for scope in service.progress_scopes) == 1
        await asyncio.sleep(0.02)
        if request.done():
            early_response = await request
            pytest.fail(f"request completed early ({early_response.status}): {await early_response.text()}")

        release_progress.set()
        response = await request
        assert response.status == 200
    finally:
        await client.close()

    progress_events = [event for event in controller.events if event["progress"] == 42.0]
    assert progress_events and progress_events[0]["status"] == "downloading"
    assert app[APP_ONNX_SERVICE].progress_scopes == set()


@pytest.mark.asyncio
async def test_app_cleanup_cancels_pending_progress_broadcasts():
    progress_started = asyncio.Event()
    progress_cancelled = asyncio.Event()
    release_progress = asyncio.Event()

    class BlockingController(_StubController):
        async def broadcast(self, event: dict) -> None:
            if event["progress"] != 42.0:
                self.events.append(event)
                return
            progress_started.set()
            try:
                await release_progress.wait()
            except asyncio.CancelledError:
                progress_cancelled.set()
                raise

    controller = BlockingController()
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    service = app[APP_ONNX_SERVICE]
    scope = service.open_progress_scope(model_id="cleanup-test")
    assert scope is not None
    task = scope.spawn(
        controller.broadcast({"progress": 42.0}),
        name="onnx_cleanup_test_broadcast",
    )
    assert task is not None
    try:
        await asyncio.wait_for(progress_started.wait(), timeout=1.0)
        await client.close()

        assert progress_cancelled.is_set()
        assert service.progress_scopes == set()
        assert service.accepting_downloads is False
    finally:
        release_progress.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_parallel_downloads_do_not_cancel_each_others_progress(monkeypatch):
    monkeypatch.setattr("src.api.onnx_routes._PROGRESS_BROADCAST_DRAIN_SECONDS", 0.02)
    b_progress_started = asyncio.Event()
    b_progress_cancelled = asyncio.Event()
    release_b_progress = asyncio.Event()
    release_b_download = asyncio.Event()

    class BlockingController(_StubController):
        async def broadcast(self, event: dict) -> None:
            if event["quantization"] == "int8" and event["progress"] == 42.0:
                b_progress_started.set()
                try:
                    await release_b_progress.wait()
                except asyncio.CancelledError:
                    b_progress_cancelled.set()
                    raise
            self.events.append(event)

    async def fake_download(_model_id, *, quantization, on_progress):
        on_progress(42.0, "halfway")
        if quantization == "int8":
            await release_b_download.wait()
        else:
            await b_progress_started.wait()
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
    controller = BlockingController()
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    request_b = asyncio.create_task(
        client.post(
            "/api/onnx/download",
            json={"modelId": "parakeet", "quantization": "int8"},
        )
    )
    request_a = None
    try:
        await asyncio.wait_for(b_progress_started.wait(), timeout=1.0)
        request_a = asyncio.create_task(
            client.post(
                "/api/onnx/download",
                json={"modelId": "parakeet", "quantization": "fp32"},
            )
        )
        response_a = await asyncio.wait_for(request_a, timeout=1.0)
        assert response_a.status == 200
        assert not b_progress_cancelled.is_set()
        assert not request_b.done()

        release_b_progress.set()
        release_b_download.set()
        response_b = await asyncio.wait_for(request_b, timeout=1.0)
        assert response_b.status == 200
    finally:
        release_b_progress.set()
        release_b_download.set()
        request_b.cancel()
        if request_a is not None:
            request_a.cancel()
        await asyncio.gather(
            request_b,
            *(tuple() if request_a is None else (request_a,)),
            return_exceptions=True,
        )
        await client.close()


@pytest.mark.asyncio
async def test_app_cleanup_rejects_late_worker_progress_callbacks(monkeypatch):
    callback_ready = asyncio.Event()
    callbacks = []

    async def fake_download(_model_id, *, quantization, on_progress):
        callbacks.append(on_progress)
        callback_ready.set()
        await asyncio.Event().wait()

    _install_onnx_stub(monkeypatch, download_model=fake_download)
    controller = _StubController()
    app = web.Application()
    register_onnx_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    request = asyncio.create_task(client.post("/api/onnx/download", json={"modelId": "parakeet"}))
    try:
        await asyncio.wait_for(callback_ready.wait(), timeout=1.0)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()

        worker = threading.Thread(target=lambda: callbacks[0](42.0, "late"))
        worker.start()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        await asyncio.sleep(0)

        assert controller.events == []
        assert app[APP_ONNX_SERVICE].progress_scopes == set()
    finally:
        request.cancel()
        await client.close()


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


def test_controller_adapter_matches_the_onnx_port(assert_protocol_contract):
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        OnnxControllerPort,
        ScriberWebController,
        methods={"broadcast"},
    )

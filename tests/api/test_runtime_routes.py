"""Runtime routes exercised without web_api.create_app.

The point of the extraction is that this domain now stands on its own: a bare
aiohttp application plus a stub controller is enough. None of these tests
construct a ScriberWebController, a pipeline, or an audio device.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.runtime_routes import APP_SHUTDOWN_EVENT, register_runtime_routes
from src.core.rest_contracts import REST_API_VERSION


class _StubController:
    def __init__(self) -> None:
        self.broadcasts: list[dict] = []
        self.flush_result: dict | None = {
            "sourceInstanceId": "instance-a",
            "heartbeatSequence": 7,
        }

    def get_health(self) -> dict:
        return {"ok": True}

    def get_state(self) -> dict:
        return {"state": "idle"}

    def get_runtime_info(self) -> dict:
        return {"runtimeMode": "python-web"}

    def get_post_processing_diagnostics(self, *, limit: int) -> dict:
        return {"limit": limit}

    def get_audio_diagnostics(self) -> dict:
        return {"microphone": {}}

    def get_hot_path_metrics(self, *, limit: int, include_active: bool) -> dict:
        return {"limit": limit, "includeActive": include_active}

    def request_frontend_performance_flush(self, source_instance_id: str) -> dict | None:
        assert source_instance_id == "instance-a"
        return self.flush_result

    async def broadcast(self, event: dict) -> None:
        self.broadcasts.append(event)


async def _client(
    controller: _StubController,
    *,
    shutdown_event: asyncio.Event | None = None,
) -> TestClient:
    app = web.Application()
    register_runtime_routes(app, controller=controller)
    if shutdown_event is not None:
        app[APP_SHUTDOWN_EVENT] = shutdown_event
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_health_and_state_read_through_to_the_controller():
    controller = _StubController()
    client = await _client(controller)
    try:
        health = await client.get("/api/health")
        state = await client.get("/api/state")
        assert await health.json() == {"ok": True}
        assert await state.json() == {"state": "idle"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_diagnostics_limits_fall_back_to_defaults_on_junk_input():
    controller = _StubController()
    client = await _client(controller)
    try:
        parsed = await client.get("/api/runtime/post-processing-diagnostics?limit=5")
        junk = await client.get("/api/runtime/post-processing-diagnostics?limit=abc")
        assert await parsed.json() == {"limit": 5}
        assert await junk.json() == {"limit": 20}

        metrics = await client.get("/api/metrics/hot-path?limit=3&includeActive=yes")
        assert await metrics.json() == {"limit": 3, "includeActive": True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_frontend_performance_rejects_out_of_range_and_unbounded_query():
    controller = _StubController()
    client = await _client(controller)
    try:
        negative = await client.get("/api/runtime/frontend-performance?afterSequence=-1")
        assert negative.status == 400

        unparsable = await client.get("/api/runtime/frontend-performance?afterSequence=x")
        assert unparsable.status == 400

        oversized = await client.get("/api/runtime/frontend-performance?sourceInstanceId=" + "a" * 65)
        assert oversized.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_flush_request_broadcasts_and_reports_a_changed_source():
    controller = _StubController()
    client = await _client(controller)
    try:
        accepted = await client.post(
            "/api/runtime/frontend-performance/flush-request",
            json={"apiVersion": REST_API_VERSION, "sourceInstanceId": "instance-a"},
        )
        assert accepted.status == 202
        assert len(controller.broadcasts) == 1

        controller.flush_result = None
        conflict = await client.post(
            "/api/runtime/frontend-performance/flush-request",
            json={"apiVersion": REST_API_VERSION, "sourceInstanceId": "instance-a"},
        )
        assert conflict.status == 409
        assert len(controller.broadcasts) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_endpoints_reject_a_non_object_body():
    controller = _StubController()
    client = await _client(controller)
    try:
        response = await client.post("/api/runtime/frontend-ready", json=["not", "an", "object"])
        assert response.status == 400
        assert (await response.json())["message"] == "Expected JSON object"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_requires_a_configured_session_token(monkeypatch):
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    controller = _StubController()
    client = await _client(controller)
    try:
        unconfigured = await client.post("/api/runtime/shutdown")
        assert unconfigured.status == 403

        monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "a-secret")
        unauthenticated = await client.post("/api/runtime/shutdown")
        assert unauthenticated.status == 401

        # run_server publishes the event after create_app returns, so a
        # request that arrives before that must not claim success.
        missing_event = await client.post(
            "/api/runtime/shutdown",
            headers={"X-Scriber-Token": "a-secret"},
        )
        assert missing_event.status == 503
    finally:
        await client.close()

    stop_event = asyncio.Event()
    client = await _client(controller, shutdown_event=stop_event)
    try:
        accepted = await client.post(
            "/api/runtime/shutdown",
            headers={"X-Scriber-Token": "a-secret"},
        )
        assert accepted.status == 200
        assert stop_event.is_set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_logs_report_a_collector_failure_as_500(monkeypatch):
    def explode(**_kwargs):
        raise OSError("log directory unreadable")

    monkeypatch.setattr("src.api.runtime_routes.collect_debug_logs", explode)
    controller = _StubController()
    client = await _client(controller)
    try:
        response = await client.get("/api/runtime/logs")
        assert response.status == 500
        assert (await response.json())["message"] == "Failed to collect runtime logs"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_support_bundle_sets_an_injection_safe_attachment_header(monkeypatch, tmp_path):
    bundle = tmp_path / 'scriber "support".zip'
    bundle.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setattr(
        "src.api.runtime_routes.create_support_bundle",
        lambda **_kwargs: bundle,
    )
    controller = _StubController()
    client = await _client(controller)
    try:
        response = await client.post("/api/runtime/support-bundle")
        assert response.status == 200
        disposition = response.headers["Content-Disposition"]
        assert '"' not in disposition.split("filename=")[1].split(";")[0].strip('"')
        assert "filename*=UTF-8''" in disposition
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_runtime_routes_does_not_need_a_real_controller():
    """The service holds whatever object it is given; no web_api import."""
    app = web.Application()
    register_runtime_routes(app, controller=SimpleNamespace(get_health=lambda: {"ok": "stub"}))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/health")
        assert await response.json() == {"ok": "stub"}
    finally:
        await client.close()

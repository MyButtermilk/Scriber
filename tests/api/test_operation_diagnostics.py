from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api import operation_diagnostics as diagnostics


@pytest.mark.asyncio
async def test_current_workflows_record_bounded_route_timings_without_content(monkeypatch):
    events = []
    monkeypatch.setattr(diagnostics, "emit_event", lambda *args, **kwargs: events.append(kwargs))

    async def action(request):
        await request.json()
        return web.json_response({"ok": True}, status=202)

    app = web.Application(middlewares=[diagnostics.operation_diagnostics_middleware])
    app.router.add_post("/api/meetings/{id}/analyze", action)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/meetings/private-meeting-id/analyze?token=private-query",
            json={"transcript": "private transcript"},
            headers={"Authorization": "Bearer private-credential"},
        )
        assert response.status == 202
    assert len(events) == 1
    event = events[0]
    assert event["workflow"] == "meetings"
    assert event["stage"] == "http"
    assert event["outcome"] == "success"
    assert event["duration_ms"] >= 0
    assert event["meta"] == {"method": "POST", "route": "/api/meetings/{id}/analyze", "status": 202}
    assert "private" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [web.HTTPConflict(text="private failure"), RuntimeError("private failure")])
async def test_failures_keep_http_semantics_without_exception_content(monkeypatch, failure):
    events = []
    monkeypatch.setattr(diagnostics, "emit_event", lambda *args, **kwargs: events.append(kwargs))

    async def action(_request):
        raise failure

    app = web.Application(middlewares=[diagnostics.operation_diagnostics_middleware])
    app.router.add_post("/api/podcasts/subscriptions", action)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/podcasts/subscriptions")
        assert response.status == (409 if isinstance(failure, web.HTTPException) else 500)
    assert events[0]["outcome"] in {"rejected", "failure"}
    assert events[0]["error_category"] == type(failure).__name__
    assert "private" not in json.dumps(events)


@pytest.mark.asyncio
async def test_polls_and_diagnostics_ingestion_do_not_log_themselves(monkeypatch):
    events = []
    monkeypatch.setattr(diagnostics, "emit_event", lambda *args, **kwargs: events.append(kwargs))

    async def action(_request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[diagnostics.operation_diagnostics_middleware])
    app.router.add_get("/api/meetings", action)
    app.router.add_get("/api/runtime/logs", action)
    app.router.add_post("/api/runtime/frontend-performance", action)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/meetings")).status == 200
        assert (await client.get("/api/runtime/logs")).status == 200
        assert (await client.post("/api/runtime/frontend-performance")).status == 200
        assert (await client.post("/unknown/private")).status == 404
    assert events == []


@pytest.mark.parametrize(
    ("route", "workflow"),
    [
        ("/api/meetings/speaker-profiles/enroll", "voice"),
        ("/api/meeting-imports/{importId}/content", "meeting_import"),
        ("/api/local-polishing/models/{variant}/install", "local_polishing"),
        ("/api/calendar/outlook/sync", "calendar"),
        ("/api/settings", "settings"),
        ("/api/live-mic/stop", "live_mic"),
        ("/api/podcasts/subscriptions", "podcasts"),
        ("/api/meetings-unknown", None),
    ],
)
def test_workflow_ownership(route, workflow):
    assert diagnostics.operation_workflow(route) == workflow


@pytest.mark.asyncio
async def test_cancellation_is_logged_and_propagated(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    events = []
    monkeypatch.setattr(diagnostics, "emit_event", lambda *args, **kwargs: events.append(kwargs))
    app = web.Application()

    async def action(_request):
        raise asyncio.CancelledError

    route = app.router.add_post("/api/meetings", action)
    request = make_mocked_request("POST", "/api/meetings", app=app)
    request.match_info._route = route
    with pytest.raises(asyncio.CancelledError):
        await diagnostics.operation_diagnostics_middleware(request, action)
    assert events[0]["outcome"] == "cancelled"

"""Outlook calendar routes exercised without web_api.create_app.

These need no controller at all -- the domain depends on the calendar
collaborator directly, so a stub calendar is the whole fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.app_keys import APP_HTTP_SESSION
from src.api.outlook_calendar_routes import OutlookCalendarPort, register_outlook_calendar_routes


class _StubCalendar:
    def __init__(self) -> None:
        self.authorization_pending = False
        self.sync_error: Exception | None = None
        self.complete_error: Exception | None = None
        self.begin_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.events_error: Exception | None = None
        self.recorded_errors: list[str] = []
        self.completed: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.begin_calls: list[bool] = []
        self.event_queries: list[dict[str, str]] = []
        self.synced = 0

    async def status(self) -> dict[str, Any]:
        return {"connected": True}

    def begin_connect(self, *, open_browser: bool = True) -> dict[str, Any]:
        if self.begin_error is not None:
            raise self.begin_error
        self.begin_calls.append(open_browser)
        return {"authorizationUrl": "https://login.example/auth"}

    def cancel_connect(self, state: str) -> None:
        self.cancelled.append(state)

    async def complete_connect(self, state: str, code: str) -> None:
        if self.complete_error is not None:
            raise self.complete_error
        self.completed.append((state, code))

    async def sync(self, session: Any) -> int:
        if self.sync_error is not None:
            raise self.sync_error
        self.synced += 1
        return 3

    async def disconnect(self) -> None:
        if self.disconnect_error is not None:
            raise self.disconnect_error

    def record_sync_error(self, error_type: str) -> None:
        self.recorded_errors.append(error_type)

    def events_for_day(self, **kwargs: str) -> dict[str, Any]:
        if self.events_error is not None:
            raise self.events_error
        self.event_queries.append(dict(kwargs))
        return {"events": []}


async def _client(calendar: _StubCalendar) -> TestClient:
    app = web.Application()
    register_outlook_calendar_routes(app, get_calendar=lambda: calendar)
    app[APP_HTTP_SESSION] = object()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_calendar_adapter_matches_the_route_port(assert_protocol_contract):
    from src.outlook_calendar import OutlookCalendarService

    assert_protocol_contract(
        OutlookCalendarPort,
        OutlookCalendarService,
        methods={
            "status",
            "begin_connect",
            "cancel_connect",
            "complete_connect",
            "sync",
            "disconnect",
            "record_sync_error",
            "events_for_day",
        },
        properties={"authorization_pending"},
    )


@pytest.mark.asyncio
async def test_status_carries_the_api_version():
    client = await _client(_StubCalendar())
    try:
        payload = await (await client.get("/api/calendar/outlook/status")).json()
        assert payload["connected"] is True
        assert payload["apiVersion"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_connect_defaults_to_opening_the_browser_and_honours_an_opt_out():
    calendar = _StubCalendar()
    client = await _client(calendar)
    try:
        assert (await client.post("/api/calendar/outlook/connect")).status == 202
        assert (await client.post("/api/calendar/outlook/connect", json={"openBrowser": False})).status == 202
        assert (await client.post("/api/calendar/outlook/connect", json={"openBrowser": True})).status == 202
        assert (await client.post("/api/calendar/outlook/connect", json=["nope"])).status == 202
    finally:
        await client.close()
    assert calendar.begin_calls == [True, False, True, True]


@pytest.mark.asyncio
async def test_connect_reports_a_conflicting_handshake():
    calendar = _StubCalendar()
    calendar.begin_error = ValueError("a sign-in is already pending")
    client = await _client(calendar)
    try:
        response = await client.post("/api/calendar/outlook/connect")
        assert response.status == 409
        assert (await response.json())["message"] == "a sign-in is already pending"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_callback_answers_the_browser_in_html_on_every_path():
    calendar = _StubCalendar()
    client = await _client(calendar)
    try:
        ok = await client.get("/api/calendar/outlook/callback", params={"state": "s1", "code": "c1"})
        assert ok.status == 200
        assert ok.content_type == "text/html"
        assert "connected" in (await ok.text()).lower()

        cancelled = await client.get("/api/calendar/outlook/callback", params={"error": "access_denied", "state": "s2"})
        assert cancelled.status == 400
        assert cancelled.content_type == "text/html"
    finally:
        await client.close()

    assert calendar.completed == [("s1", "c1")]
    assert calendar.cancelled == ["s2"]


@pytest.mark.asyncio
async def test_callback_never_leaks_the_authorization_failure_into_the_page():
    calendar = _StubCalendar()
    calendar.complete_error = RuntimeError("client_secret=hunter2 rejected")
    client = await _client(calendar)
    try:
        response = await client.get("/api/calendar/outlook/callback", params={"state": "s", "code": "c"})
        assert response.status == 400
        body = await response.text()
        assert "hunter2" not in body
        assert "try again" in body.lower()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_callback_keeps_the_connection_when_only_the_first_sync_fails():
    calendar = _StubCalendar()
    calendar.sync_error = TimeoutError()
    client = await _client(calendar)
    try:
        response = await client.get("/api/calendar/outlook/callback", params={"state": "s", "code": "c"})
        assert response.status == 200
        assert "Sync now" in await response.text()
    finally:
        await client.close()

    assert calendar.completed == [("s", "c")]
    assert calendar.recorded_errors == ["TimeoutError"]


@pytest.mark.asyncio
async def test_sync_reports_the_change_count_with_fresh_status():
    calendar = _StubCalendar()
    client = await _client(calendar)
    try:
        payload = await (await client.post("/api/calendar/outlook/sync")).json()
        assert payload["changed"] == 3
        assert payload["connected"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "recorded"),
    [
        (ValueError("not connected"), 409, "ValueError"),
        (TimeoutError(), 504, "TimeoutError"),
        (RuntimeError("graph 500"), 502, "RuntimeError"),
    ],
)
async def test_every_sync_failure_is_recorded_before_the_response(error, expected_status, recorded):
    calendar = _StubCalendar()
    calendar.sync_error = error
    client = await _client(calendar)
    try:
        response = await client.post("/api/calendar/outlook/sync")
        assert response.status == expected_status
        assert (await response.json())["message"]
    finally:
        await client.close()

    # A degraded connection has to stay visible in status() afterwards.
    assert calendar.recorded_errors == [recorded]


@pytest.mark.asyncio
async def test_sync_failure_message_does_not_leak_the_provider_detail():
    calendar = _StubCalendar()
    calendar.sync_error = RuntimeError("https://graph.microsoft.com/v1.0/me?token=abc")
    client = await _client(calendar)
    try:
        response = await client.post("/api/calendar/outlook/sync")
        assert response.status == 502
        assert "token=abc" not in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_events_are_refused_while_a_sign_in_is_pending():
    calendar = _StubCalendar()
    calendar.authorization_pending = True
    client = await _client(calendar)
    try:
        response = await client.get("/api/calendar/outlook/events")
        assert response.status == 409
        assert "sign-in" in (await response.json())["message"]
    finally:
        await client.close()
    assert calendar.event_queries == []


@pytest.mark.asyncio
async def test_events_pass_the_whole_day_window_through():
    calendar = _StubCalendar()
    client = await _client(calendar)
    try:
        response = await client.get(
            "/api/calendar/outlook/events",
            params={"date": "2026-08-14", "timeZone": "Europe/Berlin", "start": "08:00", "end": "18:00"},
        )
        assert response.status == 200
    finally:
        await client.close()

    assert calendar.event_queries == [
        {
            "day_value": "2026-08-14",
            "time_zone_name": "Europe/Berlin",
            "start_value": "08:00",
            "end_value": "18:00",
        }
    ]


@pytest.mark.asyncio
async def test_events_reject_an_unparsable_window():
    calendar = _StubCalendar()
    calendar.events_error = ValueError("start must precede end")
    client = await _client(calendar)
    try:
        response = await client.get("/api/calendar/outlook/events")
        assert response.status == 400
        assert (await response.json())["message"] == "start must precede end"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_confirms_and_reports_a_conflict():
    calendar = _StubCalendar()
    client = await _client(calendar)
    try:
        assert (await (await client.delete("/api/calendar/outlook")).json())["disconnected"] is True

        calendar.disconnect_error = ValueError("nothing connected")
        conflict = await client.delete("/api/calendar/outlook")
        assert conflict.status == 409
    finally:
        await client.close()

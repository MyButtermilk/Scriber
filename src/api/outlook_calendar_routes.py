"""Outlook calendar connection, sync, and event routes.

Eighth domain lifted out of ``web_api.create_app``, and the first that needs no
controller at all. Every handler here talks to the calendar collaborator that
the controller happens to hold; nothing else about the controller is involved.
The service therefore depends on the calendar directly, so the port describes
what this domain actually uses instead of routing through an object it does not
need.

Two behaviours are load-bearing and easy to lose in a rewrite. The OAuth
callback is reached by the system browser rather than the frontend, so it
answers in HTML and never leaks an exception detail into that page. And every
sync failure is recorded on the calendar before the response is built, so a
degraded connection stays visible in ``status()`` instead of vanishing with
the request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import ClientSession, web
from loguru import logger

from src.api.app_keys import APP_HTTP_SESSION
from src.core.rest_contracts import REST_API_VERSION

_CONNECTED_PAGE = "<h1>Outlook connected</h1><p>You can close this window and return to Scriber.</p>"
_CONNECTED_WITH_SYNC_WARNING_PAGE = (
    "<h1>Outlook connected</h1><p>The account is connected, but the first calendar sync failed. "
    "Return to Scriber and choose Sync now.</p>"
)
_CANCELED_PAGE = "<h1>Outlook connection canceled</h1><p>You can close this window.</p>"
_FAILED_PAGE = "<h1>Outlook connection failed</h1><p>Return to Scriber and try again.</p>"

_SYNC_TIMEOUT_MESSAGE = "Outlook did not respond in time. Your saved calendar remains available."
_SYNC_FAILED_MESSAGE = "Outlook calendar could not be refreshed. Your saved calendar remains available."


class OutlookCalendarPort(Protocol):
    """The calendar surface consumed by these routes."""

    @property
    def authorization_pending(self) -> bool: ...

    async def status(self) -> dict[str, Any]: ...

    def begin_connect(self, *, open_browser: bool = True) -> dict[str, Any]: ...

    def cancel_connect(self, state: str) -> None: ...

    async def complete_connect(self, state: str, code: str) -> None: ...

    async def sync(self, session: ClientSession) -> int: ...

    async def select_calendar(self, calendar_id: str) -> None: ...

    async def disconnect(self) -> None: ...

    def record_sync_error(self, error_type: str) -> None: ...

    def events_for_day(
        self,
        *,
        day_value: str = "",
        time_zone_name: str = "",
        start_value: str = "",
        end_value: str = "",
    ) -> dict[str, Any]: ...


CalendarProvider = Callable[[], OutlookCalendarPort]


@dataclass(frozen=True)
class OutlookCalendarRoutesService:
    """Dependencies the Outlook calendar domain needs from the surrounding app.

    The calendar is resolved per request rather than captured at
    registration. Composition must stay able to build an application whose
    controller never materializes a calendar -- several lifecycle tests do
    exactly that -- so reading it eagerly here would break them.
    """

    get_calendar: CalendarProvider


APP_OUTLOOK_CALENDAR_SERVICE: web.AppKey[OutlookCalendarRoutesService] = web.AppKey(
    "outlook_calendar_routes_service",
    OutlookCalendarRoutesService,
)


def _calendar(request: web.Request) -> OutlookCalendarPort:
    return request.app[APP_OUTLOOK_CALENDAR_SERVICE].get_calendar()


def _html(body: str, *, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/html", status=status)


async def status(request: web.Request) -> web.Response:
    payload = await _calendar(request).status()
    return web.json_response({"apiVersion": REST_API_VERSION, **payload})


async def connect(request: web.Request) -> web.Response:
    calendar = _calendar(request)
    try:
        raw = await request.json() if request.can_read_body else {}
        open_browser = not isinstance(raw, dict) or raw.get("openBrowser") is not False
        payload = calendar.begin_connect(open_browser=open_browser)
        return web.json_response({"apiVersion": REST_API_VERSION, **payload}, status=202)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=409)


async def callback(request: web.Request) -> web.Response:
    """Complete the OAuth handshake for the system browser.

    This endpoint is opened by the browser, not the frontend, so it answers in
    HTML. It is also the one route exempt from the session token, guarded
    instead by the single-use PKCE state value validated below.
    """

    calendar = _calendar(request)
    if request.query.get("error"):
        calendar.cancel_connect(request.query.get("state", ""))
        return _html(_CANCELED_PAGE, status=400)

    try:
        await calendar.complete_connect(request.query.get("state", ""), request.query.get("code", ""))
    except Exception as exc:
        logger.warning("Outlook OAuth callback failed: {}", type(exc).__name__)
        return _html(_FAILED_PAGE, status=400)

    # Authorization already succeeded, so a failing first sync degrades the
    # page rather than the connection.
    try:
        await calendar.sync(request.app[APP_HTTP_SESSION])
    except Exception as sync_exc:
        calendar.record_sync_error(type(sync_exc).__name__)
        logger.warning(
            "Initial Outlook calendar sync failed after successful authorization: {}",
            type(sync_exc).__name__,
        )
        return _html(_CONNECTED_WITH_SYNC_WARNING_PAGE)
    return _html(_CONNECTED_PAGE)


async def sync(request: web.Request) -> web.Response:
    calendar = _calendar(request)
    try:
        changed = await calendar.sync(request.app[APP_HTTP_SESSION])
        current = await calendar.status()
        return web.json_response({"apiVersion": REST_API_VERSION, "changed": changed, **current})
    except ValueError as exc:
        calendar.record_sync_error(type(exc).__name__)
        return web.json_response({"message": str(exc)}, status=409)
    except TimeoutError:
        calendar.record_sync_error("TimeoutError")
        return web.json_response({"message": _SYNC_TIMEOUT_MESSAGE}, status=504)
    except Exception as exc:
        error_type = type(exc).__name__
        calendar.record_sync_error(error_type)
        logger.warning("Manual Outlook calendar sync failed: {}", error_type)
        return web.json_response({"message": _SYNC_FAILED_MESSAGE}, status=502)


async def events(request: web.Request) -> web.Response:
    calendar = _calendar(request)
    if calendar.authorization_pending:
        return web.json_response(
            {"message": "Finish the Outlook sign-in before loading calendar events."},
            status=409,
        )
    try:
        payload = await asyncio.to_thread(
            calendar.events_for_day,
            day_value=request.query.get("date", ""),
            time_zone_name=request.query.get("timeZone", ""),
            start_value=request.query.get("start", ""),
            end_value=request.query.get("end", ""),
        )
        return web.json_response({"apiVersion": REST_API_VERSION, **payload})
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def select_calendar(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
    except ValueError, TypeError:
        raw = None
    calendar_id = str(raw.get("calendarId") or "").strip() if isinstance(raw, dict) else ""
    try:
        calendar = _calendar(request)
        await calendar.select_calendar(calendar_id)
        current = await calendar.status()
        return web.json_response({"apiVersion": REST_API_VERSION, **current})
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def disconnect(request: web.Request) -> web.Response:
    try:
        await _calendar(request).disconnect()
        return web.json_response({"apiVersion": REST_API_VERSION, "disconnected": True})
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=409)


def register_outlook_calendar_routes(
    app: web.Application,
    *,
    get_calendar: CalendarProvider,
) -> None:
    """Register the Outlook calendar domain without web_api closure coupling."""

    app[APP_OUTLOOK_CALENDAR_SERVICE] = OutlookCalendarRoutesService(get_calendar=get_calendar)

    app.router.add_get("/api/calendar/outlook/status", status)
    app.router.add_post("/api/calendar/outlook/connect", connect)
    app.router.add_get("/api/calendar/outlook/callback", callback)
    app.router.add_post("/api/calendar/outlook/sync", sync)
    app.router.add_post("/api/calendar/outlook/calendar", select_calendar)
    app.router.add_get("/api/calendar/outlook/events", events)
    app.router.add_delete("/api/calendar/outlook", disconnect)

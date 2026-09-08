"""Content-free operation timings across the current HTTP workflow domains.

Register after authentication. Only registered route templates enter logs; body,
query, headers, concrete path parameters and exception messages never do. Polls
stay silent, so opening the diagnostic console cannot generate its own traffic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter

from aiohttp import web
from loguru import logger

from src.core.logging_setup import emit_event

_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SILENT_ROUTES = frozenset(
    {
        "/api/runtime/frontend-ready",
        "/api/runtime/frontend-performance",
        "/api/runtime/frontend-performance/flush-request",
        "/api/runtime/logs",
    }
)


def operation_workflow(route: str) -> str | None:
    if route.startswith("/api/meetings/") and any(
        marker in route for marker in ("/speaker-", "/speakers/", "/diarization-component")
    ):
        return "voice"
    for prefix, workflow in (
        ("/api/meeting-imports", "meeting_import"),
        ("/api/meetings", "meetings"),
        ("/api/live-mic", "live_mic"),
        ("/api/podcasts", "podcasts"),
        ("/api/youtube", "youtube"),
        ("/api/file", "file"),
        ("/api/transcripts", "transcripts"),
        ("/api/local-polishing", "local_polishing"),
        ("/api/onnx", "models"),
        ("/api/calendar/outlook", "calendar"),
        ("/api/settings", "settings"),
        ("/api/microphones", "devices"),
        ("/api/autostart", "settings"),
        ("/api/runtime", "runtime"),
    ):
        if route == prefix or route.startswith(prefix + "/"):
            return workflow
    return None


@web.middleware
async def operation_diagnostics_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    resource = request.match_info.route.resource
    route = resource.canonical if resource is not None else ""
    workflow = operation_workflow(route)
    if request.method not in _MUTATIONS or not workflow or route in _SILENT_ROUTES:
        return await handler(request)

    started = perf_counter()
    status = 500
    outcome = "failure"
    error_type: str | None = None
    try:
        response = await handler(request)
        status = response.status
        outcome = "success" if status < 400 else "rejected" if status < 500 else "failure"
        return response
    except web.HTTPException as exc:
        status = exc.status
        outcome = "rejected" if status < 500 else "failure"
        error_type = type(exc).__name__
        raise
    except asyncio.CancelledError:
        status = 499
        outcome = "cancelled"
        raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        emit_event(
            logger.bind(component="http"),
            "Application operation completed",
            level="ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
            event="runtime.operation.completed",
            workflow=workflow,
            stage="http",
            duration_ms=round((perf_counter() - started) * 1000.0, 3),
            outcome=outcome,
            error_category=error_type,
            meta={"method": request.method, "route": route, "status": status},
        )

"""Runtime, diagnostics, and health routes.

This is the first domain lifted out of ``web_api.create_app``. It follows the
shape established by :mod:`src.api.meeting_delivery_routes`: handlers are
module-level functions that resolve their dependencies from the application
via a typed :class:`aiohttp.web.AppKey`, instead of closing over locals of the
factory. That keeps each handler importable and testable on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from src.api.http_security import (
    attachment_content_disposition,
    configured_session_token,
    is_loopback_request,
    request_has_valid_session_token,
)
from src.core.rest_contracts import (
    REST_API_VERSION,
    RESTContractError,
    validate_frontend_performance_flush_request_payload,
    validate_frontend_performance_request_payload,
    validate_frontend_ready_request_payload,
)
from src.core.ws_contracts import frontend_performance_flush_event
from src.runtime.debug_logs import clear_debug_logs, collect_debug_logs
from src.runtime.support_bundle import create_support_bundle


@dataclass(frozen=True)
class RuntimeRoutesService:
    """Dependencies the runtime domain needs from the surrounding app.

    ``controller`` is typed loosely on purpose: annotating it as
    ``ScriberWebController`` would import ``web_api``, which imports this
    module in turn.
    """

    controller: Any


APP_RUNTIME_SERVICE: web.AppKey[RuntimeRoutesService] = web.AppKey(
    "runtime_routes_service",
    RuntimeRoutesService,
)

# Owned here rather than in web_api because the shutdown handler below is its
# only reader. run_server sets it after create_app returns, so the handler must
# keep resolving it per request.
APP_SHUTDOWN_EVENT: web.AppKey[asyncio.Event] = web.AppKey("shutdown_event", asyncio.Event)


def _controller(request: web.Request) -> Any:
    return request.app[APP_RUNTIME_SERVICE].controller


async def _json_object_body(request: web.Request) -> dict[str, Any] | web.Response:
    """Return a decoded JSON object, or the 400 response to send instead."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON payload"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"message": "Expected JSON object"}, status=400)
    return payload


def _bounded_int_query(request: web.Request, key: str, default: int) -> int:
    try:
        return int(request.query.get(key, str(default)))
    except ValueError:
        return default


async def health(request: web.Request) -> web.Response:
    return web.json_response(_controller(request).get_health())


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(_controller(request).get_state())


async def get_runtime(request: web.Request) -> web.Response:
    return web.json_response(_controller(request).get_runtime_info())


async def get_frontend_ready(request: web.Request) -> web.Response:
    return web.json_response(_controller(request).get_frontend_ready())


async def post_frontend_ready(request: web.Request) -> web.Response:
    payload = await _json_object_body(request)
    if isinstance(payload, web.Response):
        return payload
    try:
        validate_frontend_ready_request_payload(payload)
    except RESTContractError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    return web.json_response(_controller(request).record_frontend_ready(payload, request))


async def get_frontend_performance(request: web.Request) -> web.Response:
    raw_after_sequence = request.query.get("afterSequence")
    after_sequence: int | None = None
    if raw_after_sequence is not None:
        try:
            after_sequence = int(raw_after_sequence)
        except ValueError:
            return web.json_response(
                {"message": "afterSequence must be a non-negative integer"},
                status=400,
            )
        if after_sequence < 0:
            return web.json_response(
                {"message": "afterSequence must be a non-negative integer"},
                status=400,
            )
    source_instance_id = request.query.get("sourceInstanceId")
    if source_instance_id is not None and (
        not source_instance_id
        or len(source_instance_id) > 64
        or not all(char.isalnum() or char in "-_" for char in source_instance_id)
    ):
        return web.json_response(
            {"message": "sourceInstanceId must be a bounded opaque identifier"},
            status=400,
        )
    return web.json_response(
        _controller(request).get_frontend_performance(
            after_sequence=after_sequence,
            source_instance_id=source_instance_id,
        )
    )


async def post_frontend_performance(request: web.Request) -> web.Response:
    payload = await _json_object_body(request)
    if isinstance(payload, web.Response):
        return payload
    try:
        validate_frontend_performance_request_payload(payload)
    except RESTContractError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    return web.json_response(_controller(request).record_frontend_performance(payload))


async def request_frontend_performance_flush(request: web.Request) -> web.Response:
    controller = _controller(request)
    payload = await _json_object_body(request)
    if isinstance(payload, web.Response):
        return payload
    try:
        validate_frontend_performance_flush_request_payload(payload)
    except RESTContractError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    flush = controller.request_frontend_performance_flush(payload["sourceInstanceId"])
    if flush is None:
        return web.json_response(
            {"message": "Frontend performance source changed"},
            status=409,
        )
    await controller.broadcast(
        frontend_performance_flush_event(
            flush["sourceInstanceId"],
            flush["heartbeatSequence"],
        )
    )
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "accepted": True,
            **flush,
        },
        status=202,
    )


async def get_audio_diagnostics(request: web.Request) -> web.Response:
    payload = await asyncio.to_thread(_controller(request).get_audio_diagnostics)
    return web.json_response(payload)


async def get_post_processing_diagnostics(request: web.Request) -> web.Response:
    limit = _bounded_int_query(request, "limit", 20)
    return web.json_response(_controller(request).get_post_processing_diagnostics(limit=limit))


async def get_runtime_logs(request: web.Request) -> web.Response:
    limit = _bounded_int_query(request, "limit", 500)
    try:
        payload = await asyncio.to_thread(collect_debug_logs, limit=limit)
    except Exception:
        logger.exception("Failed to collect runtime logs")
        return web.json_response({"message": "Failed to collect runtime logs"}, status=500)
    return web.json_response(payload)


async def delete_runtime_logs(request: web.Request) -> web.Response:
    try:
        payload = await asyncio.to_thread(clear_debug_logs)
    except Exception:
        logger.exception("Failed to clear runtime logs")
        return web.json_response({"message": "Failed to clear runtime logs"}, status=500)
    status = 200 if payload.get("ok") else 500
    return web.json_response(payload, status=status)


async def shutdown_runtime(request: web.Request) -> web.Response:
    if not is_loopback_request(request):
        return web.json_response({"message": "Runtime shutdown is only available on loopback"}, status=403)

    token = configured_session_token()
    if not token:
        return web.json_response({"message": "Runtime shutdown token is not configured"}, status=403)
    if not request_has_valid_session_token(request, token):
        return web.json_response({"message": "Session token required"}, status=401)

    stop_event = request.app.get(APP_SHUTDOWN_EVENT)
    if not isinstance(stop_event, asyncio.Event):
        return web.json_response({"message": "Runtime shutdown is not available"}, status=503)

    stop_event.set()
    return web.json_response({"ok": True, "message": "Shutdown requested"})


async def create_runtime_support_bundle(request: web.Request) -> web.StreamResponse:
    controller = _controller(request)
    runtime_info = controller.get_runtime_info()
    app_state = controller.get_state()
    post_processing_diagnostics = controller.get_post_processing_diagnostics(limit=30)

    def build_bundle() -> Path:
        return create_support_bundle(
            runtime_info=runtime_info,
            app_state=app_state,
            audio_diagnostics=controller.get_audio_diagnostics(),
            post_processing_diagnostics=post_processing_diagnostics,
        )

    try:
        bundle_path = await asyncio.to_thread(build_bundle)
    except Exception:
        logger.exception("Failed to create support bundle")
        return web.json_response({"message": "Failed to create support bundle"}, status=500)

    return web.FileResponse(
        bundle_path,
        headers={
            "Content-Disposition": attachment_content_disposition(bundle_path.name),
        },
    )


async def get_hot_path_metrics(request: web.Request) -> web.Response:
    limit = _bounded_int_query(request, "limit", 50)
    include_active = str(request.query.get("includeActive", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    payload = await asyncio.to_thread(
        _controller(request).get_hot_path_metrics,
        limit=limit,
        include_active=include_active,
    )
    return web.json_response(payload)


def register_runtime_routes(app: web.Application, *, controller: Any) -> None:
    """Register the runtime domain without web_api closure coupling."""

    app[APP_RUNTIME_SERVICE] = RuntimeRoutesService(controller=controller)

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/runtime", get_runtime)
    app.router.add_get("/api/runtime/frontend-ready", get_frontend_ready)
    app.router.add_post("/api/runtime/frontend-ready", post_frontend_ready)
    app.router.add_get("/api/runtime/frontend-performance", get_frontend_performance)
    app.router.add_post("/api/runtime/frontend-performance", post_frontend_performance)
    app.router.add_post(
        "/api/runtime/frontend-performance/flush-request",
        request_frontend_performance_flush,
    )
    app.router.add_get("/api/runtime/audio-diagnostics", get_audio_diagnostics)
    app.router.add_get("/api/runtime/post-processing-diagnostics", get_post_processing_diagnostics)
    app.router.add_get("/api/runtime/logs", get_runtime_logs)
    app.router.add_delete("/api/runtime/logs", delete_runtime_logs)
    app.router.add_post("/api/runtime/shutdown", shutdown_runtime)
    app.router.add_post("/api/runtime/support-bundle", create_runtime_support_bundle)
    app.router.add_get("/api/metrics/hot-path", get_hot_path_metrics)

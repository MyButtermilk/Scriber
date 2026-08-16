"""Local polishing model lifecycle routes.

Sixth domain lifted out of ``web_api.create_app``.

The domain earns its own module through its error taxonomy rather than its
size: a catalogue that may be absent from a build, and a small set of typed
failure codes that each map onto a distinct status. Keeping that mapping in one
table beside the handlers is the point -- it was previously a nested closure
that every handler in the group called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web
from loguru import logger

from src.local_polishing import CatalogError, LocalPolishingError

_UNAVAILABLE_CODE = "local_polishing_unavailable"

# Typed failure code -> (status, message shown to the user).
_ERRORS: dict[str, tuple[int, str]] = {
    "catalog_unavailable": (503, "Local polishing models are not available in this build."),
    "unknown_operation": (404, "This local polishing operation no longer exists."),
    "unknown_variant": (400, "This local polishing model is not supported."),
    "selected_model": (409, "Switch away from this local model before removing it."),
    "model_busy": (409, "Wait for the model download to finish or cancel it first."),
    "model_in_use": (409, "The local model is currently in use and cannot be removed."),
    "closed": (503, "Local polishing is shutting down."),
}
_FALLBACK = (503, "Local polishing is temporarily unavailable.")


class LocalPolishingControllerPort(Protocol):
    """The controller surface consumed by the local polishing routes."""

    def get_local_polishing_models(self) -> dict[str, Any]: ...

    async def install_local_polishing_model(self, variant: str) -> dict[str, Any]: ...

    async def cancel_local_polishing_operation(self, operation_id: str) -> dict[str, Any]: ...

    async def remove_local_polishing_model(self, variant: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalPolishingRoutesService:
    """Dependencies the local polishing domain needs from the surrounding app."""

    controller: LocalPolishingControllerPort


APP_LOCAL_POLISHING_SERVICE: web.AppKey[LocalPolishingRoutesService] = web.AppKey(
    "local_polishing_routes_service",
    LocalPolishingRoutesService,
)


def _controller(request: web.Request) -> LocalPolishingControllerPort:
    return request.app[APP_LOCAL_POLISHING_SERVICE].controller


def error_response(exc: Exception) -> web.Response:
    """Map a local polishing failure onto its status and public message."""

    if isinstance(exc, CatalogError):
        code = "catalog_unavailable"
    elif isinstance(exc, LocalPolishingError):
        code = exc.code
    else:
        code = _UNAVAILABLE_CODE
    status, message = _ERRORS.get(code, _FALLBACK)
    return web.json_response({"success": False, "code": code, "message": message}, status=status)


async def list_models(request: web.Request) -> web.Response:
    try:
        return web.json_response(_controller(request).get_local_polishing_models())
    except Exception:
        logger.exception("Failed to read local-polishing model state")
        return error_response(RuntimeError())


async def install_model(request: web.Request) -> web.Response:
    variant = str(request.match_info.get("variant") or "")
    try:
        model = await _controller(request).install_local_polishing_model(variant)
    except (CatalogError, LocalPolishingError) as exc:
        return error_response(exc)
    except Exception:
        logger.exception("Failed to start local-polishing model installation")
        return error_response(RuntimeError())
    return web.json_response({"success": True, **model}, status=202)


async def cancel_operation(request: web.Request) -> web.Response:
    operation_id = str(request.match_info.get("operationId") or "")
    try:
        operation = await _controller(request).cancel_local_polishing_operation(operation_id)
    except LocalPolishingError as exc:
        return error_response(exc)
    except Exception:
        logger.exception("Failed to cancel local-polishing model installation")
        return error_response(RuntimeError())
    return web.json_response({"success": True, **operation}, status=202)


async def remove_model(request: web.Request) -> web.Response:
    variant = str(request.match_info.get("variant") or "")
    try:
        model = await _controller(request).remove_local_polishing_model(variant)
    except LocalPolishingError as exc:
        return error_response(exc)
    except Exception:
        logger.exception("Failed to remove local-polishing model")
        return error_response(RuntimeError())
    return web.json_response({"success": True, **model})


def register_local_polishing_routes(
    app: web.Application,
    *,
    controller: LocalPolishingControllerPort,
) -> None:
    """Register the local polishing domain without web_api closure coupling."""

    app[APP_LOCAL_POLISHING_SERVICE] = LocalPolishingRoutesService(controller=controller)

    app.router.add_get("/api/local-polishing/models", list_models)
    app.router.add_post("/api/local-polishing/models/{variant}/install", install_model)
    app.router.add_delete("/api/local-polishing/model-operations/{operationId}", cancel_operation)
    app.router.add_delete("/api/local-polishing/models/{variant}", remove_model)

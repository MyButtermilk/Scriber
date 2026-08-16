"""Application settings routes.

Fifth domain lifted out of ``web_api.create_app``.

Reading and writing settings are both bounded controller operations already:
``update_settings`` owns the settings lock, the validation, and the persistence
protocol behind its public signature. These handlers only shape the transport,
so this module stays thin by design.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web
from loguru import logger


class SettingsControllerPort(Protocol):
    """The controller surface consumed by the settings routes."""

    def get_settings(self) -> dict[str, Any]: ...

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SettingsRoutesService:
    """Dependencies the settings domain needs from the surrounding app."""

    controller: SettingsControllerPort


APP_SETTINGS_SERVICE: web.AppKey[SettingsRoutesService] = web.AppKey(
    "settings_routes_service",
    SettingsRoutesService,
)


def _controller(request: web.Request) -> SettingsControllerPort:
    return request.app[APP_SETTINGS_SERVICE].controller


async def get_settings(request: web.Request) -> web.Response:
    # Reading settings touches the settings file, so it stays off the loop.
    return web.json_response(await asyncio.to_thread(_controller(request).get_settings))


async def put_settings(request: web.Request) -> web.Response:
    controller = _controller(request)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Invalid JSON"}, status=400)
    try:
        updated = await controller.update_settings(payload if isinstance(payload, dict) else {})
        return web.json_response(updated)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Failed to update settings")
        return web.json_response({"message": str(exc) or "Failed to update settings"}, status=500)


def register_settings_routes(app: web.Application, *, controller: SettingsControllerPort) -> None:
    """Register the settings domain without web_api closure coupling."""

    app[APP_SETTINGS_SERVICE] = SettingsRoutesService(controller=controller)

    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)

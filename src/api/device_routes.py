"""Microphone enumeration and desktop autostart routes.

Seventh domain lifted out of ``web_api.create_app``.

Autostart lives here because it is the shell's counterpart to device
management, not because it does any work: the installed Tauri shell owns
autostart, so the backend answers with an explicit unavailable contract and
rejects legacy mutations. Both endpoints stay so an older frontend gets a clear
answer rather than a 404.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

_AUTOSTART_CONTRACT = {
    "enabled": False,
    "available": False,
    "message": "Desktop autostart is managed by the Tauri shell",
}


class DeviceControllerPort(Protocol):
    """The controller surface consumed by the device routes."""

    def list_microphones(self) -> list[dict[str, str]]: ...

    def request_microphone_refresh(self, hint_payload: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DeviceRoutesService:
    """Dependencies the device domain needs from the surrounding app."""

    controller: DeviceControllerPort


APP_DEVICE_SERVICE: web.AppKey[DeviceRoutesService] = web.AppKey(
    "device_routes_service",
    DeviceRoutesService,
)


def _controller(request: web.Request) -> DeviceControllerPort:
    return request.app[APP_DEVICE_SERVICE].controller


async def get_autostart(request: web.Request) -> web.Response:
    """Report unavailable outside the Tauri-owned desktop command surface."""
    return web.json_response(dict(_AUTOSTART_CONTRACT))


async def set_autostart(request: web.Request) -> web.Response:
    """Reject legacy backend mutations; the installed shell owns autostart."""
    return web.json_response(dict(_AUTOSTART_CONTRACT), status=409)


async def list_microphones(request: web.Request) -> web.Response:
    # Device enumeration is a blocking WASAPI call.
    devices = await asyncio.to_thread(_controller(request).list_microphones)
    return web.json_response({"devices": devices})


async def refresh_microphones(request: web.Request) -> web.Response:
    payload: dict[str, Any] | None = None
    if request.can_read_body:
        try:
            raw_payload = await request.json()
        except Exception:
            return web.json_response({"message": "Invalid JSON"}, status=400)
        if not isinstance(raw_payload, dict):
            return web.json_response({"message": "Expected JSON object"}, status=400)
        payload = raw_payload
    return web.json_response(_controller(request).request_microphone_refresh(payload))


def register_device_routes(app: web.Application, *, controller: DeviceControllerPort) -> None:
    """Register the device domain without web_api closure coupling."""

    app[APP_DEVICE_SERVICE] = DeviceRoutesService(controller=controller)

    app.router.add_get("/api/autostart", get_autostart)
    app.router.add_post("/api/autostart", set_autostart)
    app.router.add_get("/api/microphones", list_microphones)
    app.router.add_post("/api/microphones/refresh", refresh_microphones)

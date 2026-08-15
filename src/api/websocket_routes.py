"""WebSocket connection lifecycle and browser-origin boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import WSMsgType, web

from src.api.http_security import origin_allowed
from src.core.ws_contracts import state_event
from src.runtime.cancellation import await_with_delayed_cancellation


class WebSocketControllerPort(Protocol):
    """Only the connection operations consumed by the WebSocket domain."""

    async def add_client(self, ws: web.WebSocketResponse) -> None: ...

    async def remove_client(self, ws: web.WebSocketResponse) -> None: ...

    async def send_client_text(self, ws: web.WebSocketResponse, message: str) -> bool: ...

    def get_state(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class WebSocketRoutesService:
    controller: WebSocketControllerPort


APP_WEBSOCKET_SERVICE: web.AppKey[WebSocketRoutesService] = web.AppKey(
    "websocket_routes_service",
    WebSocketRoutesService,
)


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    origin = request.headers.get("Origin")
    if origin and not origin_allowed(origin):
        return web.json_response({"message": "Origin not allowed"}, status=403)

    websocket = web.WebSocketResponse(heartbeat=30)
    await websocket.prepare(request)
    controller = request.app[APP_WEBSOCKET_SERVICE].controller
    await controller.add_client(websocket)

    try:
        initial_sent = await controller.send_client_text(
            websocket,
            json.dumps(state_event(controller.get_state())),
        )
        if not initial_sent:
            return websocket
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                if message.data == "ping" and not await controller.send_client_text(websocket, "pong"):
                    break
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        _, removal_cancel = await await_with_delayed_cancellation(controller.remove_client(websocket))
        if removal_cancel is not None:
            raise removal_cancel
    return websocket


def register_websocket_routes(
    app: web.Application,
    *,
    controller: WebSocketControllerPort,
) -> None:
    app[APP_WEBSOCKET_SERVICE] = WebSocketRoutesService(controller=controller)
    app.router.add_get("/ws", websocket_handler)

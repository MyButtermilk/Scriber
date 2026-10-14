"""Soniox's connection-time authentication and private wire logging boundary."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

# websockets DEBUG logs include the complete HTTP upgrade headers and frames.
# Keep this logger private (outside logging's registry), even when root DEBUG
# logging is enabled. Provider lifecycle diagnostics belong to the callers.
_WIRE_LOGGER = logging.Logger("scriber.soniox.websocket")
_WIRE_LOGGER.propagate = False
_WIRE_LOGGER.disabled = True


def soniox_connect_options(api_key: str) -> dict:
    return {
        "additional_headers": {"Authorization": f"Bearer {api_key}"},
        "logger": _WIRE_LOGGER,
    }


async def _private_operation(operation: Awaitable[Any]) -> Any:
    try:
        return await operation
    except StopAsyncIteration:
        raise
    except ConnectionClosed as exc:
        # Preserve closure classes, codes, and ordering for Pipecat's reconnect
        # policy, but never forward a provider-controlled close reason.
        received = Close(exc.rcvd.code, "") if exc.rcvd is not None else None
        sent = Close(exc.sent.code, "") if exc.sent is not None else None
        raise type(exc)(received, sent, exc.rcvd_then_sent) from None
    except Exception as exc:
        raise ConnectionError(f"Soniox WebSocket operation failed ({type(exc).__name__})") from None


class _PrivateSonioxWebSocket:
    """Expose only the transport operations consumed by Meeting and Pipecat."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._messages = websocket.__aiter__()

    @property
    def state(self) -> Any:
        return self._websocket.state

    async def send(self, message: Any) -> None:
        await _private_operation(self._websocket.send(message))

    async def ping(self) -> Any:
        return await _private_operation(self._websocket.ping())

    async def close(self) -> None:
        await _private_operation(self._websocket.close())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await _private_operation(self._messages.__anext__())


async def connect_soniox_websocket(url: str, api_key: str, *, connect_factory: Callable[..., Any] | None = None) -> Any:
    try:
        websocket = await (connect_factory or connect)(
            url,
            **soniox_connect_options(api_key),
        )
        return _PrivateSonioxWebSocket(websocket)
    except Exception as exc:
        # Handshake exceptions can retain the request, response, and headers.
        raise ConnectionError(f"Soniox WebSocket connection failed ({type(exc).__name__})") from None

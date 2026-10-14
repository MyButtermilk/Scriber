"""Soniox's connection-time authentication and private wire logging boundary."""

import logging
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect

# websockets DEBUG logs include the complete HTTP upgrade headers and frames.
# Keep this logger private (outside logging's registry), even when root DEBUG
# logging is enabled. Provider lifecycle diagnostics belong to the callers.
_WIRE_LOGGER = logging.Logger("scriber.soniox.websocket", level=logging.CRITICAL + 1)
_WIRE_LOGGER.addHandler(logging.NullHandler())
_WIRE_LOGGER.propagate = False


async def connect_soniox_websocket(url: str, api_key: str, *, connect_factory: Callable[..., Any] | None = None) -> Any:
    try:
        return await (connect_factory or connect)(
            url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            logger=_WIRE_LOGGER,
        )
    except Exception as exc:
        # Handshake exceptions can retain the request, response, and headers.
        raise ConnectionError(f"Soniox WebSocket connection failed ({type(exc).__name__})") from None

"""Credential-safe connection options shared by Soniox realtime clients."""

import logging

# websockets DEBUG records include HTTP headers and frame bodies. Keep this
# transport's wire logger private and disabled, even when root DEBUG is enabled.
_WIRE_LOGGER = logging.Logger("scriber.soniox.websocket")
_WIRE_LOGGER.disabled = True
_WIRE_LOGGER.propagate = False


def soniox_connect_options(api_key: str) -> dict:
    return {
        "additional_headers": {"Authorization": f"Bearer {api_key}"},
        "logger": _WIRE_LOGGER,
    }

"""Validated Soniox data-residency regions and provider endpoints.

Soniox regional projects use region-specific API keys and domains.  Keep the
mapping in one dependency-free module so realtime, Meeting preview, and async
uploads can never drift to different regions.
"""
from __future__ import annotations

from typing import Final


DEFAULT_SONIOX_REGION: Final = "us"
SUPPORTED_SONIOX_REGIONS: Final = frozenset({"us", "eu"})

_REST_API_BASE_URLS: Final = {
    "us": "https://api.soniox.com/v1",
    "eu": "https://api.eu.soniox.com/v1",
}
_REALTIME_WEBSOCKET_URLS: Final = {
    "us": "wss://stt-rt.soniox.com/transcribe-websocket",
    "eu": "wss://stt-rt.eu.soniox.com/transcribe-websocket",
}


def normalize_soniox_region(value: object, *, strict: bool = False) -> str:
    """Return a canonical region, defaulting safely to the US deployment.

    ``strict`` is used at the Settings API boundary so an invalid value cannot
    silently route audio somewhere the user did not select. Startup remains
    backwards compatible with old or manually edited ``.env`` files.
    """

    normalized = str(value or "").strip().lower()
    if not normalized:
        return DEFAULT_SONIOX_REGION
    if normalized in SUPPORTED_SONIOX_REGIONS:
        return normalized
    if strict:
        allowed = ", ".join(sorted(SUPPORTED_SONIOX_REGIONS))
        raise ValueError(f"Invalid Soniox region '{value}'. Allowed: {allowed}")
    return DEFAULT_SONIOX_REGION


def soniox_rest_api_base_url(region: object) -> str:
    return _REST_API_BASE_URLS[normalize_soniox_region(region)]


def soniox_realtime_websocket_url(region: object) -> str:
    return _REALTIME_WEBSOCKET_URLS[normalize_soniox_region(region)]

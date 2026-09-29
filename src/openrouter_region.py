"""One allowlisted OpenRouter origin for every STT and text request."""

from __future__ import annotations

from typing import Final

DEFAULT_OPENROUTER_REGION: Final = "eu"
SUPPORTED_OPENROUTER_REGIONS: Final = frozenset({"eu", "us", "global"})

_API_BASE_URLS: Final = {
    "eu": "https://eu.openrouter.ai/api/v1",
    "us": "https://us.openrouter.ai/api/v1",
    "global": "https://openrouter.ai/api/v1",
}


def normalize_openrouter_region(value: object, *, strict: bool = False) -> str:
    """Missing legacy settings adopt EU; invalid explicit values fail closed."""

    normalized = str(value if value is not None else "").strip().lower()
    if not normalized and not strict:
        return DEFAULT_OPENROUTER_REGION
    if normalized in SUPPORTED_OPENROUTER_REGIONS:
        return normalized
    raise ValueError("Invalid OpenRouter region. Choose eu, us, or global.")


def openrouter_api_base_url(region: object) -> str:
    return _API_BASE_URLS[normalize_openrouter_region(region)]


def openrouter_stt_url(region: object) -> str:
    return f"{openrouter_api_base_url(region)}/audio/transcriptions"

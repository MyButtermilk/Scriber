"""HTTP and WebSocket transport security shared by every route module.

These live outside ``web_api`` so route domains extracted from ``create_app``
can enforce the same loopback and session-token rules without importing the
module that registers them, which would be circular.

Every helper reads its configuration at call time. Tests therefore steer them
by setting or clearing ``SCRIBER_SESSION_TOKEN`` rather than by patching.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from pathlib import Path
from urllib.parse import quote, urlparse

from aiohttp import web

SESSION_TOKEN_ENV = "SCRIBER_SESSION_TOKEN"
SESSION_TOKEN_HEADER = "X-Scriber-Token"
SESSION_TOKEN_QUERY = "scriberToken"
ALLOWED_ORIGINS_ENV = "SCRIBER_ALLOWED_ORIGINS"
_DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "tauri.localhost"})
_DEFAULT_ALLOWED_CUSTOM_ORIGINS = frozenset({"tauri://localhost"})


def attachment_content_disposition(filename: str) -> str:
    """Build an injection-safe attachment header with a UTF-8 filename."""
    cleaned = "".join(
        character
        for character in str(filename or "")
        if ord(character) >= 32 and character not in {'"', "\\", "/", "\x7f"}
    ).strip()
    if not cleaned:
        cleaned = "download"
    raw_suffix = Path(cleaned).suffix
    ascii_suffix = "".join(
        character for character in raw_suffix if character.isascii() and (character.isalnum() or character in ".-_")
    )
    raw_stem = cleaned[: -len(raw_suffix)] if raw_suffix else cleaned
    ascii_stem = "".join(
        character if character.isascii() and (character.isalnum() or character in " .-_") else "_"
        for character in raw_stem
    ).strip(" ._")
    ascii_fallback = f"{ascii_stem or 'download'}{ascii_suffix}"
    encoded = quote(cleaned, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def configured_session_token() -> str:
    return os.getenv(SESSION_TOKEN_ENV, "").strip()


def session_token_required() -> bool:
    return bool(configured_session_token())


def is_loopback_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    normalized = normalized.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def validate_server_bind_security(host: str, session_token: str) -> None:
    """Fail closed before exposing an unauthenticated non-loopback listener."""

    if is_loopback_bind_host(host):
        return
    token = str(session_token or "").strip()
    if len(token.encode("utf-8", errors="strict")) < 32:
        raise RuntimeError(
            "SCRIBER_SESSION_TOKEN must contain at least 32 bytes when SCRIBER_WEB_HOST is not loopback."
        )


def is_loopback_request(request: web.Request) -> bool:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and peername:
        host = str(peername[0]).split("%", 1)[0].lower()
        if host == "localhost":
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(address.is_loopback or (mapped and mapped.is_loopback))
    return False


def request_session_token(request: web.Request) -> str:
    header_token = request.headers.get(SESSION_TOKEN_HEADER, "").strip()
    if header_token:
        return header_token

    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return request.query.get(SESSION_TOKEN_QUERY, "").strip()


def request_has_valid_session_token(request: web.Request, token: str | None = None) -> bool:
    expected = (token if token is not None else configured_session_token()).strip()
    if not expected:
        return False
    provided = request_session_token(request)
    return bool(provided) and hmac.compare_digest(provided, expected)


def origin_allowed(origin: str) -> bool:
    """Return whether a browser origin may reach the local HTTP/WS server."""

    normalized = str(origin or "").strip()
    if not normalized:
        return False
    configured = tuple(
        entry.strip().rstrip("/") for entry in os.getenv(ALLOWED_ORIGINS_ENV, "").split(",") if entry.strip()
    )
    if "*" in configured:
        return True
    if configured:
        return normalized in configured
    if normalized.rstrip("/") in _DEFAULT_ALLOWED_CUSTOM_ORIGINS:
        return True
    parsed = urlparse(normalized)
    return bool(parsed.scheme in {"http", "https"} and parsed.hostname in _DEFAULT_ALLOWED_HOSTS)

from __future__ import annotations

from typing import Any
import json


class ResponseTooLargeError(RuntimeError):
    pass


async def read_response_text_limited(response: Any, max_bytes: int) -> str:
    """Read an aiohttp-style response without allowing an unbounded body."""
    limit = max(1, int(max_bytes))
    content_length = getattr(response, "content_length", None)
    if isinstance(content_length, int) and content_length > limit:
        raise ResponseTooLargeError(f"HTTP response exceeds {limit} bytes")

    content = getattr(response, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        body = bytearray()
        async for chunk in iter_chunked(min(64 * 1024, limit + 1)):
            body.extend(chunk)
            if len(body) > limit:
                raise ResponseTooLargeError(f"HTTP response exceeds {limit} bytes")
        charset = getattr(response, "charset", None) or "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            # Remote servers control Content-Type. An invalid charset label
            # should not turn an otherwise readable response into a provider
            # crash.
            return body.decode("utf-8", errors="replace")

    # Lightweight provider fakes used by unit tests commonly expose only
    # text(). Keep that interface supported while still enforcing the cap.
    text = await response.text()
    if len(text.encode("utf-8", errors="replace")) > limit:
        raise ResponseTooLargeError(f"HTTP response exceeds {limit} bytes")
    return text


async def read_response_json_limited(response: Any, max_bytes: int) -> Any:
    """Parse a bounded JSON response, including lightweight test doubles."""
    if callable(getattr(response, "text", None)) or getattr(response, "content", None) is not None:
        raw = await read_response_text_limited(response, max_bytes)
        return json.loads(raw) if raw else {}

    payload = await response.json()
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    if len(encoded) > max(1, int(max_bytes)):
        raise ResponseTooLargeError(f"HTTP response exceeds {max_bytes} bytes")
    return payload

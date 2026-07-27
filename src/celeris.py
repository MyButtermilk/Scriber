"""Bounded Celeris chat-completions transport.

Celeris exposes an OpenAI-compatible endpoint, but ``celeris-1`` has a strict
8,192-token context window and accepts output budgets only in multiples of 256.
This boundary validates those constraints before any transcript text leaves the
process and keeps provider errors free of request or response content.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import aiohttp
from loguru import logger

from src.config import Config
from src.runtime.http_response import read_response_text_limited

CELERIS_MODEL = "celeris-1"
CELERIS_BASE_URL = "https://inference.celeris.ai/celeris-1/v1"
CELERIS_CHAT_COMPLETIONS_URL = f"{CELERIS_BASE_URL}/chat/completions"
CELERIS_CONTEXT_TOKENS = 8_192
CELERIS_TOKEN_QUANTUM = 256
CELERIS_PRODUCT_OUTPUT_CAP_TOKENS = 3_072
CELERIS_PROMPT_OVERHEAD_TOKENS = 128
CELERIS_RESPONSE_LIMIT_BYTES = 8 * 1024 * 1024
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_celeris_model(model: str | None) -> bool:
    return str(model or "").strip().casefold() == CELERIS_MODEL


def estimate_celeris_prompt_tokens(prompt: str) -> int:
    """Return a deliberately conservative upper bound for request planning.

    A tokenizer is not published for ``celeris-1``. UTF-8 byte length is a safe
    upper bound for byte-level tokenization and avoids optimistic language-
    specific character ratios. The fixed reserve covers the chat envelope.
    """

    encoded_bytes = len(str(prompt or "").encode("utf-8", errors="replace"))
    return max(1, encoded_bytes + CELERIS_PROMPT_OVERHEAD_TOKENS)


def celeris_request_max_tokens(prompt: str, requested: int) -> int:
    """Quantize an output budget while keeping the complete request in context."""

    prompt_tokens = estimate_celeris_prompt_tokens(prompt)
    available = CELERIS_CONTEXT_TOKENS - prompt_tokens
    available_quantized = (available // CELERIS_TOKEN_QUANTUM) * CELERIS_TOKEN_QUANTUM
    if available_quantized < CELERIS_TOKEN_QUANTUM:
        raise ValueError("Celeris prompt exceeds the supported context window; split the input before generation.")
    requested_quantized = max(
        CELERIS_TOKEN_QUANTUM,
        math.ceil(max(1, int(requested)) / CELERIS_TOKEN_QUANTUM) * CELERIS_TOKEN_QUANTUM,
    )
    return min(
        CELERIS_PRODUCT_OUTPUT_CAP_TOKENS,
        requested_quantized,
        available_quantized,
    )


def celeris_prompt_fits(prompt: str, requested: int) -> bool:
    requested_quantized = min(
        CELERIS_PRODUCT_OUTPUT_CAP_TOKENS,
        max(
            CELERIS_TOKEN_QUANTUM,
            math.ceil(max(1, int(requested)) / CELERIS_TOKEN_QUANTUM) * CELERIS_TOKEN_QUANTUM,
        ),
    )
    try:
        available = celeris_request_max_tokens(prompt, requested)
    except ValueError:
        return False
    return available >= requested_quantized


def _response_text(data: Any) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
    return ""


def _error_code(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "unknown"
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    normalized = str(code or "").strip().lower()
    if normalized and len(normalized) <= 80 and all(value.isalnum() or value in "._-" for value in normalized):
        return normalized
    return "unknown"


def _retry_after_seconds(response: Any, attempt: int) -> float:
    raw = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
    try:
        retry_after = float(raw)
    except (TypeError, ValueError):
        retry_after = 0.75 * (2**attempt)
    if not math.isfinite(retry_after):
        retry_after = 0.75 * (2**attempt)
    return min(10.0, max(0.05, retry_after))


async def celeris_chat_completion(
    prompt: str,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
    api_key: str | None = None,
    session: Any | None = None,
) -> str:
    """Generate one complete Celeris response with bounded retries."""

    key = str(api_key or getattr(Config, "CELERIS_API_KEY", "") or "").strip()
    if not key:
        raise ValueError("Celeris API key not configured. Please add it in Settings.")

    output_tokens = celeris_request_max_tokens(prompt, max_output_tokens)
    timeout_value = max(15.0, float(timeout_seconds))
    request_timeout = aiohttp.ClientTimeout(
        total=timeout_value,
        connect=min(15.0, timeout_value),
        sock_connect=min(15.0, timeout_value),
        sock_read=timeout_value,
    )
    payload = {
        "model": CELERIS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_tokens,
        "temperature": 0,
        "seed": 7,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    owned_session: aiohttp.ClientSession | None = None
    client = session
    if client is None:
        owned_session = aiohttp.ClientSession(
            timeout=request_timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        client = owned_session

    try:
        for attempt in range(3):
            try:
                async with client.post(
                    CELERIS_CHAT_COMPLETIONS_URL,
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                ) as response:
                    raw = await read_response_text_limited(
                        response,
                        CELERIS_RESPONSE_LIMIT_BYTES,
                    )
                    if response.status >= 400:
                        code = _error_code(raw)
                        if response.status in _RETRYABLE_STATUSES and attempt < 2:
                            await asyncio.sleep(_retry_after_seconds(response, attempt))
                            continue
                        raise RuntimeError(f"Celeris API error {response.status} (code={code}).")
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("Celeris returned invalid JSON.") from exc
                    content = _response_text(data)
                    if not content:
                        raise RuntimeError("Celeris returned an empty response.")
                    logger.info(
                        "Celeris generation complete: {} chars (model={}, max_tokens={})",
                        len(content),
                        CELERIS_MODEL,
                        output_tokens,
                    )
                    return content
            except TimeoutError:
                raise
            except aiohttp.ClientError as exc:
                # A connection failure after request transmission may have
                # consumed provider work. Do not automatically duplicate it.
                raise RuntimeError(f"Celeris request failed ({type(exc).__name__}).") from exc
    finally:
        if owned_session is not None:
            await owned_session.close()

    raise RuntimeError("Celeris request failed.")


__all__ = [
    "CELERIS_BASE_URL",
    "CELERIS_CHAT_COMPLETIONS_URL",
    "CELERIS_CONTEXT_TOKENS",
    "CELERIS_MODEL",
    "CELERIS_TOKEN_QUANTUM",
    "celeris_chat_completion",
    "celeris_prompt_fits",
    "celeris_request_max_tokens",
    "estimate_celeris_prompt_tokens",
    "is_celeris_model",
]

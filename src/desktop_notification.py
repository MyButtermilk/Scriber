"""Trusted desktop notifications delivered through Tauri shell IPC."""

from __future__ import annotations

import asyncio
import functools
import time
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from src.runtime.shell_ipc import available as shell_ipc_available
from src.runtime.shell_ipc import call_shell_ipc

_FALLBACK_NOTIFICATION_TIMEOUT_SECONDS = 0.75
_FALLBACK_NOTIFICATION_SHELL_TIMEOUT_SECONDS = 0.6
_FALLBACK_NOTIFICATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="scriber-fallback-notification",
)


def show_post_processing_fallback_notification(
    primary_model: str,
    fallback_model: str,
    event_id: str,
    *,
    deadline_monotonic: float | None = None,
) -> bool:
    """Ask Windows to accept the fallback toast without exposing transcript text."""

    primary = str(primary_model or "").strip()
    fallback = str(fallback_model or "").strip()
    normalized_event_id = str(event_id or "").strip()
    if not primary or not fallback or not normalized_event_id or not shell_ipc_available():
        return False
    request_payload = {
        "eventId": normalized_event_id,
        "primaryModel": primary,
        "fallbackModel": fallback,
    }
    deadline = (
        float(deadline_monotonic)
        if deadline_monotonic is not None
        else time.monotonic() + _FALLBACK_NOTIFICATION_TIMEOUT_SECONDS
    )
    remaining = deadline - time.monotonic()
    if remaining < 0.05:
        return False
    try:
        response = call_shell_ipc(
            "postProcessingFallbackNotify",
            request_payload,
            timeout_seconds=min(_FALLBACK_NOTIFICATION_SHELL_TIMEOUT_SECONDS, remaining),
        )
    except Exception as exc:
        logger.debug(
            "Desktop fallback notification request failed: error_type={}",
            type(exc).__name__,
        )
        return False
    payload = response.get("payload") if isinstance(response, dict) else None
    return bool(
        isinstance(response, dict)
        and response.get("success") is True
        and isinstance(payload, dict)
        and payload.get("accepted") is True
    )


async def request_post_processing_fallback_notification(
    primary_model: str,
    fallback_model: str,
    event_id: str,
) -> bool:
    """Run the bounded native request on its own executor after text injection."""

    deadline = time.monotonic() + _FALLBACK_NOTIFICATION_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    call = functools.partial(
        show_post_processing_fallback_notification,
        primary_model,
        fallback_model,
        event_id,
        deadline_monotonic=deadline,
    )
    try:
        return bool(await loop.run_in_executor(_FALLBACK_NOTIFICATION_EXECUTOR, call))
    except Exception as exc:
        logger.debug(
            "Desktop fallback notification executor failed: error_type={}",
            type(exc).__name__,
        )
        return False

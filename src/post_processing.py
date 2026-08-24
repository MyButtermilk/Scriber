from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Protocol

from loguru import logger

from src.config import Config
from src.core.provider_errors import ProviderTransportError
from src.runtime.env_values import env_float, env_int
from src.runtime.provider_http import ProviderHttpTransport
from src.summarization import generate_live_mic_text

_OUTPUT_PLACEHOLDER = "${output}"
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


class LocalTranscriptPolisher(Protocol):
    async def polish(self, transcript: str, variant: str) -> Any: ...


def build_post_processing_prompt(raw_text: str, prompt_template: str | None = None) -> str:
    """Build a live-mic post-processing prompt from the configured template."""
    transcript = (raw_text or "").strip()
    template = (prompt_template or Config.POST_PROCESSING_PROMPT or "").strip()
    if not template:
        template = Config._DEFAULT_POST_PROCESSING_PROMPT
    if _OUTPUT_PLACEHOLDER in template:
        return template.replace(_OUTPUT_PLACEHOLDER, transcript)
    return f"{template.rstrip()}\n\nRaw transcript:\n{transcript}"


def clean_post_processing_output(text: str) -> str:
    cleaned = _THINK_TAG_RE.sub("", text or "").strip()
    cleaned = re.sub(r"^\s*(final answer|output|cleaned text)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip().strip("`").strip()


def post_processing_output_token_budget(raw_text: str) -> int:
    words = max(1, len((raw_text or "").split()))
    multiplier = env_float(
        "SCRIBER_POST_PROCESSING_TOKEN_MULTIPLIER",
        2.2,
        minimum=0.1,
        maximum=20.0,
    )
    overhead = env_int(
        "SCRIBER_POST_PROCESSING_TOKEN_OVERHEAD",
        256,
        minimum=0,
        maximum=65536,
    )
    minimum = env_int(
        "SCRIBER_POST_PROCESSING_MIN_OUTPUT_TOKENS",
        768,
        minimum=1,
        maximum=65536,
    )
    maximum = env_int(
        "SCRIBER_POST_PROCESSING_MAX_OUTPUT_TOKENS",
        4096,
        minimum=minimum,
        maximum=65536,
    )
    estimated = int(words * multiplier) + overhead
    return max(minimum, min(maximum, estimated))


def live_post_processing_timeout_seconds() -> float:
    """Return the cloud model-routing deadline for one Live Mic result."""
    return env_float(
        "SCRIBER_LIVE_POST_PROCESSING_TIMEOUT_SEC",
        7.0,
        minimum=0.05,
        maximum=30.0,
    )


def live_post_processing_openrouter_max_semantic_attempts() -> int:
    """Return the semantic OpenRouter generation cap for one Live Mic result."""
    return env_int(
        "SCRIBER_LIVE_POST_PROCESSING_OPENROUTER_MAX_SEMANTIC_ATTEMPTS",
        1,
        minimum=1,
        maximum=4,
    )


async def post_process_live_transcript(
    raw_text: str,
    *,
    model: str | None = None,
    engine: str | None = None,
    local_polisher: LocalTranscriptPolisher | None = None,
    local_variant: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    provider_http_transport: ProviderHttpTransport | None = None,
) -> str:
    """Clean live-mic transcript text before insertion into the active app."""
    transcript = (raw_text or "").strip()
    if not transcript:
        if diagnostics is not None:
            diagnostics.update(
                {
                    "status": "skipped",
                    "skipReason": "empty_input",
                    "rawChars": 0,
                    "rawWords": 0,
                }
            )
        return ""
    selected_engine = (engine or Config.POST_PROCESSING_ENGINE or "cloud").strip().lower()
    if selected_engine == "local":
        if local_polisher is None:
            raise RuntimeError("Local post-processing is not available.")
        variant = (local_variant or Config.LOCAL_POLISHING_VARIANT or "q8_0").strip().lower()
        if diagnostics is not None:
            diagnostics.update(
                {
                    "status": "started",
                    "engine": "local",
                    "model": f"local:{variant}",
                    "rawChars": len(transcript),
                    "rawWords": len(transcript.split()),
                }
            )
        outcome = await local_polisher.polish(transcript, variant)
        processed = str(getattr(outcome, "text", "") or "")
        accepted = getattr(outcome, "status", None) == "accepted"
        reason_codes = tuple(str(value) for value in (getattr(outcome, "reason_codes", ()) or ()))
        if diagnostics is not None:
            diagnostics.update(
                {
                    "status": "completed" if accepted else "original_fallback",
                    "engine": "local",
                    "model": f"local:{variant}",
                    "durationMs": float(getattr(outcome, "duration_ms", 0.0) or 0.0),
                    "runtimeBackend": getattr(outcome, "runtime_backend", None),
                    "reasonCodes": list(reason_codes),
                    "fallbackToRaw": not accepted,
                    "cleanedChars": len(processed),
                    "outputChanged": accepted and processed != transcript,
                }
            )
        # The local boundary is deliberately fail-closed: a rejected or failed
        # generation returns the original transcript and never falls through to
        # a cloud provider.
        return processed if accepted and processed.strip() else transcript
    if selected_engine != "cloud":
        raise RuntimeError("Unsupported post-processing engine.")

    selected_model = model or Config.POST_PROCESSING_MODEL or Config.DEFAULT_POST_PROCESSING_MODEL
    prompt = build_post_processing_prompt(transcript)
    max_output_tokens = post_processing_output_token_budget(transcript)
    deadline_seconds = live_post_processing_timeout_seconds()
    openrouter_max_semantic_attempts = live_post_processing_openrouter_max_semantic_attempts()
    if diagnostics is not None:
        diagnostics.update(
            {
                "status": "started",
                "engine": "cloud",
                "model": selected_model,
                "rawChars": len(transcript),
                "rawWords": len(transcript.split()),
                "promptChars": len(prompt),
                "maxOutputTokens": max_output_tokens,
                "deadlineMs": deadline_seconds * 1000.0,
                "openRouterReasoningEffort": "low",
                "openRouterSemanticAttemptLimit": openrouter_max_semantic_attempts,
                "fallbackPolicy": "bounded_cross_provider",
            }
        )
    logger.info(
        "Post-processing live transcript with {} ({} chars, max_output_tokens={}, deadline_seconds={})",
        selected_model,
        len(transcript),
        max_output_tokens,
        deadline_seconds,
    )
    started = time.monotonic()
    try:
        async with asyncio.timeout(deadline_seconds):
            processed = await generate_live_mic_text(
                prompt,
                selected_model,
                max_output_tokens=max_output_tokens,
                openrouter_reasoning_effort="low",
                openrouter_max_semantic_attempts=openrouter_max_semantic_attempts,
                routing_diagnostics=diagnostics,
                provider_http_transport=provider_http_transport,
            )
    except TimeoutError as exc:
        duration_ms = (time.monotonic() - started) * 1000
        if diagnostics is not None:
            diagnostics.update(
                {
                    "status": "deadline_exceeded",
                    "fallbackToRaw": True,
                    "reasonCodes": ["deadline_exceeded"],
                    "durationMs": duration_ms,
                }
            )
        raise RuntimeError(f"Live mic post-processing deadline exceeded after {deadline_seconds:g}s.") from exc
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000
        failure_reason_codes = ["provider_error" if isinstance(exc, ProviderTransportError) else "generation_failed"]
        if isinstance(exc, ProviderTransportError):
            if exc.code:
                failure_reason_codes.append(exc.code)
            elif exc.status is not None:
                failure_reason_codes.append(f"http_{exc.status}")
        if diagnostics is not None:
            diagnostics.update(
                {
                    "status": "failed",
                    "fallbackToRaw": True,
                    "reasonCodes": failure_reason_codes,
                    "durationMs": duration_ms,
                }
            )
        raise
    cleaned = clean_post_processing_output(processed)
    duration_ms = (time.monotonic() - started) * 1000
    if diagnostics is not None:
        diagnostics.update(
            {
                "status": "completed" if cleaned else "empty_output",
                "engine": "cloud",
                "providerResponseChars": len(processed or ""),
                "cleanedChars": len(cleaned or ""),
                "outputChanged": cleaned != transcript,
                "durationMs": duration_ms,
                "fallbackToRaw": False,
            }
        )
    if not cleaned:
        raise RuntimeError("Post-processing returned an empty response.")
    return cleaned

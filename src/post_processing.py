from __future__ import annotations

import os
import re
import time
from typing import Any

from loguru import logger

from src.config import Config
from src.summarization import generate_text_with_model
from src.runtime.env_values import env_float, env_int

_OUTPUT_PLACEHOLDER = "${output}"
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


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
        512,
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


async def post_process_live_transcript(
    raw_text: str,
    *,
    model: str | None = None,
    diagnostics: dict[str, Any] | None = None,
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
    selected_model = model or Config.POST_PROCESSING_MODEL or Config.DEFAULT_POST_PROCESSING_MODEL
    prompt = build_post_processing_prompt(transcript)
    max_output_tokens = post_processing_output_token_budget(transcript)
    if diagnostics is not None:
        diagnostics.update(
            {
                "status": "started",
                "model": selected_model,
                "rawChars": len(transcript),
                "rawWords": len(transcript.split()),
                "promptChars": len(prompt),
                "maxOutputTokens": max_output_tokens,
            }
        )
    logger.info(
        "Post-processing live transcript with {} ({} chars, max_output_tokens={})",
        selected_model,
        len(transcript),
        max_output_tokens,
    )
    started = time.monotonic()
    processed = await generate_text_with_model(
        prompt,
        selected_model,
        max_output_tokens=max_output_tokens,
    )
    cleaned = clean_post_processing_output(processed)
    duration_ms = (time.monotonic() - started) * 1000
    if diagnostics is not None:
        diagnostics.update(
            {
                "status": "completed" if cleaned else "empty_output",
                "providerResponseChars": len(processed or ""),
                "cleanedChars": len(cleaned or ""),
                "outputChanged": cleaned != transcript,
                "durationMs": duration_ms,
            }
        )
    if not cleaned:
        raise RuntimeError("Post-processing returned an empty response.")
    return cleaned

from __future__ import annotations

import os
import re

from loguru import logger

from src.config import Config
from src.summarization import generate_text_with_model

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
    multiplier = float(os.getenv("SCRIBER_POST_PROCESSING_TOKEN_MULTIPLIER", "2.2") or "2.2")
    overhead = int(os.getenv("SCRIBER_POST_PROCESSING_TOKEN_OVERHEAD", "256") or "256")
    minimum = int(os.getenv("SCRIBER_POST_PROCESSING_MIN_OUTPUT_TOKENS", "512") or "512")
    maximum = int(os.getenv("SCRIBER_POST_PROCESSING_MAX_OUTPUT_TOKENS", "4096") or "4096")
    estimated = int(words * multiplier) + overhead
    return max(minimum, min(maximum, estimated))


async def post_process_live_transcript(raw_text: str, *, model: str | None = None) -> str:
    """Clean live-mic transcript text before insertion into the active app."""
    transcript = (raw_text or "").strip()
    if not transcript:
        return ""
    selected_model = model or Config.POST_PROCESSING_MODEL or Config.SUMMARIZATION_MODEL
    prompt = build_post_processing_prompt(transcript)
    max_output_tokens = post_processing_output_token_budget(transcript)
    logger.info(
        "Post-processing live transcript with {} ({} chars, max_output_tokens={})",
        selected_model,
        len(transcript),
        max_output_tokens,
    )
    processed = await generate_text_with_model(
        prompt,
        selected_model,
        max_output_tokens=max_output_tokens,
    )
    cleaned = clean_post_processing_output(processed)
    if not cleaned:
        raise RuntimeError("Post-processing returned an empty response.")
    return cleaned

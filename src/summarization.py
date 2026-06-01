"""
LLM-based transcript summarization.
Supports OpenAI (GPT-4) and Google Gemini models.
"""

from __future__ import annotations
import asyncio
import json
import math
import os
import re
from typing import Any, Literal

import aiohttp
from loguru import logger

from src.config import Config

SummarizationModel = Literal[
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-pro-preview",
    "gpt-5.2",
    "gpt-5-mini",
    "gpt-5-nano",
]
_MODEL_OUTPUT_TOKEN_CAPS = {
    "gpt-5-nano": 4096,
    "gpt-5-mini": 8192,
    "gpt-5.2": 8192,
    "gemini-flash-latest": 65536,
    "gemini-3.5-flash": 65536,
    "gemini-3-flash-preview": 8192,
    "gemini-3.1-flash-lite-preview": 8192,
    "gemini-3-pro-preview": 12288,
}
_MARKDOWN_OUTPUT_GUARDRAIL = (
    "Ausgabeformat (verbindlich):\n"
    "- Gib die Antwort als valides Markdown aus.\n"
    "- Verwende fuer Aufzaehlungen nur Markdown-Listensyntax ('-' oder '*'), niemals den Bullet-Charakter '•'.\n"
    "- Hauptabschnitte als '##', Unterabschnitte als '###'.\n"
    "- Zwischen Abschnitten eine Leerzeile lassen."
)


def _summary_timeout_seconds() -> float:
    """Global timeout guard for a single summarization request."""
    raw = os.getenv("SCRIBER_SUMMARY_TIMEOUT_SEC", "240").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 240.0
    # Keep a sane lower bound to avoid accidental immediate timeouts.
    return max(15.0, value)


def _is_retryable_gemini_failure(message: str) -> bool:
    lower = (message or "").lower()
    retry_markers = (
        "gemini api error 429",
        "gemini api error 500",
        "gemini api error 503",
        "resource_exhausted",
        "unavailable",
        "high demand",
        "rate limit",
        "timeout",
    )
    return any(marker in lower for marker in retry_markers)


def _should_fallback_to_openai() -> bool:
    return os.getenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", "1").strip().lower() not in {"0", "false", "no"}


def _is_gemini_thinking_model(model: str) -> bool:
    return model.startswith("gemini-3") or model == "gemini-flash-latest"


def _summary_budget_for_text(
    text: str,
    model: str,
    *,
    duration_seconds: int | None = None,
) -> tuple[int, int, int]:
    """
    Derive dynamic summary length and token budget from transcript size.

    Returns:
        tuple: (input_word_count, target_summary_words, max_output_tokens)
    """
    input_words = max(1, len((text or "").split()))

    # Short transcripts should still produce rich summaries (not one-liners),
    # while long transcripts remain compressed.
    if input_words <= 800:
        ratio = 0.28
    elif input_words <= 2_000:
        ratio = 0.24
    elif input_words <= 5_000:
        ratio = 0.15
    elif input_words <= 10_000:
        ratio = 0.12
    else:
        ratio = 0.10

    min_words = max(80, int(os.getenv("SCRIBER_SUMMARY_MIN_WORDS", "180")))
    max_words = max(min_words, int(os.getenv("SCRIBER_SUMMARY_MAX_WORDS", "2200")))
    short_input_max_words = max(1, int(os.getenv("SCRIBER_SUMMARY_SHORT_INPUT_MAX_WORDS", "2500")))
    short_min_words = max(min_words, int(os.getenv("SCRIBER_SUMMARY_SHORT_MIN_WORDS", "320")))
    target_words = int(round(input_words * ratio))
    target_words = max(min_words, min(max_words, target_words))
    if input_words <= short_input_max_words:
        target_words = max(target_words, short_min_words)

    # Approximate model tokens needed for the requested output length.
    # Defaults are intentionally generous to avoid clipping long summaries.
    token_multiplier = max(1.0, float(os.getenv("SCRIBER_SUMMARY_TOKEN_MULTIPLIER", "1.8")))
    token_overhead = max(0, int(os.getenv("SCRIBER_SUMMARY_TOKEN_OVERHEAD", "220")))
    min_tokens = max(256, int(os.getenv("SCRIBER_SUMMARY_MIN_OUTPUT_TOKENS", "512")))
    max_tokens = max(min_tokens, int(os.getenv("SCRIBER_SUMMARY_MAX_OUTPUT_TOKENS", "8192")))
    short_min_tokens = max(min_tokens, int(os.getenv("SCRIBER_SUMMARY_SHORT_MIN_OUTPUT_TOKENS", "900")))

    model_cap = _MODEL_OUTPUT_TOKEN_CAPS.get(model, max_tokens)
    budget_cap = max(min_tokens, min(max_tokens, model_cap))

    requested_tokens = int(math.ceil(target_words * token_multiplier)) + token_overhead
    if input_words <= short_input_max_words:
        requested_tokens = max(requested_tokens, short_min_tokens)
    output_tokens = max(min_tokens, min(budget_cap, requested_tokens))

    # For very long recordings (e.g. >30 min), allow a larger first-pass output.
    long_video_min_seconds = max(1, int(os.getenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")))
    long_video_token_bonus = max(0, int(os.getenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "600")))
    if duration_seconds and duration_seconds >= long_video_min_seconds and long_video_token_bonus > 0:
        output_tokens = min(budget_cap, output_tokens + long_video_token_bonus)

    # Gemini 3 uses hidden "thinking" budget within max_output_tokens.
    # Reserve additional tokens so visible output is not cut to 1-2 lines.
    if _is_gemini_thinking_model(model):
        thinking_reserve = max(0, int(os.getenv("SCRIBER_SUMMARY_GEMINI_THINKING_RESERVE_TOKENS", "2400")))
        if thinking_reserve > 0:
            output_tokens = min(budget_cap, output_tokens + thinking_reserve)

    return input_words, target_words, output_tokens


def _dynamic_length_instruction(input_words: int, target_words: int) -> str:
    return (
        "Zusätzliche Längenregel (automatisch): "
        f"Der Input hat ungefähr {input_words} Wörter. "
        f"Erstelle eine inhaltlich vollständige Zusammenfassung mit ungefähr {target_words} Wörtern (Toleranz ±15%). "
        "Bei langen Inputs sollen alle Hauptthemen, Entscheidungen, offenen Punkte und relevanten Details enthalten sein."
    )


def _normalize_summary_markdown(text: str) -> str:
    """Normalize common LLM markdown quirks for stable rendering."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u00a0", " ").replace("\u200b", "")
    normalized = re.sub(r"(?m)^(?P<indent>[ \t]*)[•●◦▪▫‣⁃]\s+", r"\g<indent>- ", normalized)
    return normalized.strip()


def _parse_duration_seconds(duration: str | None) -> int | None:
    raw = (duration or "").strip()
    if not raw or raw in {"--", "--:--", "-:--"}:
        return None

    parts = raw.split(":")
    if len(parts) not in (2, 3):
        return None

    try:
        values = [int(part) for part in parts]
    except ValueError:
        return None

    if len(values) == 2:
        minutes, seconds = values
        if minutes < 0 or seconds < 0:
            return None
        return minutes * 60 + seconds

    hours, minutes, seconds = values
    if hours < 0 or minutes < 0 or seconds < 0:
        return None
    return hours * 3600 + minutes * 60 + seconds


async def summarize_text(
    text: str,
    model: SummarizationModel | None = None,
    *,
    duration: str | None = None,
) -> str:
    """
    Summarize text using the configured LLM model.
    
    Args:
        text: The transcript text to summarize
        model: Optional override for the model (uses Config.SUMMARIZATION_MODEL if not provided)
    
    Returns:
        The summarized text
    
    Raises:
        ValueError: If no API key is configured for the selected model
        RuntimeError: If the API call fails
    """
    if not text or not text.strip():
        return ""
    
    model = model or getattr(Config, "SUMMARIZATION_MODEL", Config.DEFAULT_SUMMARIZATION_MODEL)
    base_prompt = Config.SUMMARIZATION_PROMPT or "Summarize the following transcript:"
    duration_seconds = _parse_duration_seconds(duration)
    input_words, target_words, output_tokens = _summary_budget_for_text(
        text,
        model,
        duration_seconds=duration_seconds,
    )
    length_instruction = _dynamic_length_instruction(input_words, target_words)
    full_prompt = f"{base_prompt}\n\n{length_instruction}\n\n{_MARKDOWN_OUTPUT_GUARDRAIL}\n\n{text}"
    
    logger.info(
        "Summarizing transcript with {} ({} chars, ~{} words, target ~{} words, duration_s={}, max_output_tokens={})",
        model,
        len(text),
        input_words,
        target_words,
        duration_seconds,
        output_tokens,
    )

    timeout_seconds = _summary_timeout_seconds()

    if model.startswith("gpt-"):
        summarize_coro = _summarize_openai(full_prompt, model, output_tokens)
    elif model.startswith("gemini-"):
        summarize_coro = _summarize_gemini(full_prompt, model, output_tokens)
    else:
        raise ValueError(f"Unknown summarization model: {model}")

    try:
        summary = await asyncio.wait_for(summarize_coro, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        timeout_display = max(1, int(round(timeout_seconds)))
        logger.error(
            "Summarization timed out after {}s (model={})",
            timeout_seconds,
            model,
        )
        raise RuntimeError(
            f"Summarization timed out after {timeout_display}s. Please try again."
        ) from exc
    except RuntimeError as exc:
        # Gemini can occasionally return transient 429/503 ("high demand").
        # If configured and OpenAI key is available, fall back automatically.
        if (
            model.startswith("gemini-")
            and _should_fallback_to_openai()
            and bool(Config.OPENAI_API_KEY)
            and _is_retryable_gemini_failure(str(exc))
        ):
            fallback_model = (os.getenv("SCRIBER_SUMMARY_FALLBACK_MODEL", "gpt-5-mini") or "").strip()
            if fallback_model.startswith("gpt-"):
                logger.warning(
                    "Gemini summarization failed with retryable error. Falling back to OpenAI model '{}'.",
                    fallback_model,
                )
                try:
                    summary = await asyncio.wait_for(
                        _summarize_openai(full_prompt, fallback_model, output_tokens),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError as timeout_exc:
                    timeout_display = max(1, int(round(timeout_seconds)))
                    raise RuntimeError(
                        f"Summarization timed out after {timeout_display}s (fallback model: {fallback_model}). Please try again."
                    ) from timeout_exc
            else:
                raise
        else:
            raise
    return _normalize_summary_markdown(summary)


async def _summarize_openai(prompt: str, model: str, max_output_tokens: int) -> str:
    """Summarize using OpenAI API."""
    api_key = Config.OPENAI_API_KEY
    if not api_key:
        raise ValueError("OpenAI API key not configured. Please add it in Settings.")
    
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai library not installed. Run: pip install openai")
    
    timeout_seconds = _summary_timeout_seconds()
    try:
        client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)
    except TypeError:
        # Older SDK versions may not expose timeout in the constructor.
        client = openai.AsyncOpenAI(api_key=api_key)
    
    try:
        # gpt-5 models are most reliable with the Responses API and max_output_tokens.
        if model.startswith("gpt-5") and hasattr(client, "responses"):
            response = await client.responses.create(
                model=model,
                input=prompt,
                max_output_tokens=max_output_tokens,
            )
            content = _extract_openai_response_text(response)
            logger.info(f"OpenAI summarization complete: {len(content or '')} chars")
            return content or ""

        chat_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
        }
        if not model.startswith("gpt-5"):
            chat_kwargs["temperature"] = 0.3
        response = await client.chat.completions.create(**chat_kwargs)
        content = response.choices[0].message.content
        logger.info(f"OpenAI summarization complete: {len(content or '')} chars")
        return content or ""

    except openai.APIError as e:
        logger.error(f"OpenAI API error: {e}")
        raise RuntimeError(f"OpenAI API error: {e}")
    except Exception as e:
        logger.exception("OpenAI summarization failed")
        raise RuntimeError(f"OpenAI summarization failed: {e}")


def _extract_openai_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output_items = getattr(response, "output", None)
    if not isinstance(output_items, list):
        return ""

    chunks: list[str] = []
    for item in output_items:
        parts = getattr(item, "content", None)
        if not isinstance(parts, list):
            continue
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks).strip()


async def _summarize_gemini(prompt: str, model: str, max_output_tokens: int) -> str:
    """Summarize using Google Gemini API."""
    api_key = Config.GOOGLE_API_KEY
    if not api_key:
        raise ValueError("Gemini API key not configured. Please add it in Settings.")

    try:
        timeout_seconds = _summary_timeout_seconds()
        timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=min(15, timeout_seconds),
            sock_connect=min(15, timeout_seconds),
            sock_read=timeout_seconds,
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        generation_config: dict[str, Any] = {
            "maxOutputTokens": max_output_tokens,
        }
        temperature_raw = os.getenv("SCRIBER_SUMMARY_GEMINI_TEMPERATURE", "").strip()
        if temperature_raw:
            generation_config["temperature"] = min(1.0, max(0.0, float(temperature_raw)))

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

        retries = max(0, int(os.getenv("SCRIBER_SUMMARY_GEMINI_RETRIES", "2")))
        data: dict[str, Any] = {}
        last_error: RuntimeError | None = None

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(retries + 1):
                async with session.post(url, json=payload) as resp:
                    raw = await resp.text()
                    if resp.status >= 400:
                        detail = raw[:600]
                        err = RuntimeError(f"Gemini API error {resp.status}: {detail}")
                        if resp.status in {429, 500, 503} and attempt < retries:
                            delay = min(8.0, 1.5 * (2 ** attempt))
                            logger.warning(
                                "Gemini API transient error (status={}) on attempt {}/{}. Retrying in {:.1f}s.",
                                resp.status,
                                attempt + 1,
                                retries + 1,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            last_error = err
                            continue
                        raise err
                    data = json.loads(raw)
                    last_error = None
                    break

        if last_error is not None:
            raise last_error

        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        first = candidates[0] if candidates else {}
        content_parts = first.get("content", {}).get("parts", []) if isinstance(first, dict) else []
        content = "".join(
            part.get("text", "")
            for part in content_parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ).strip()

        finish_reason = first.get("finishReason") if isinstance(first, dict) else None
        usage = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
        candidate_tokens = usage.get("candidatesTokenCount")
        total_tokens = usage.get("totalTokenCount")

        logger.info(
            "Gemini summarization complete: {} chars (finish_reason={}, candidate_tokens={}, total_tokens={})",
            len(content or ""),
            finish_reason,
            candidate_tokens,
            total_tokens,
        )

        if finish_reason == "MAX_TOKENS":
            logger.warning(
                "Gemini stopped due MAX_TOKENS (max_output_tokens={}). Consider increasing SCRIBER_SUMMARY_GEMINI_THINKING_RESERVE_TOKENS.",
                max_output_tokens,
            )

        if not content:
            prompt_feedback = data.get("promptFeedback") if isinstance(data, dict) else None
            raise RuntimeError(f"Gemini returned empty response. promptFeedback={prompt_feedback}")

        return content

    except aiohttp.ClientError as e:
        logger.exception("Gemini summarization HTTP error")
        raise RuntimeError(f"Gemini summarization failed: {e}")
    except json.JSONDecodeError as e:
        logger.exception("Gemini summarization parse error")
        raise RuntimeError(f"Gemini response parse failed: {e}")
    except Exception as e:
        logger.exception("Gemini summarization failed")
        raise RuntimeError(f"Gemini summarization failed: {e}")

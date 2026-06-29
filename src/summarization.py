"""
LLM-based transcript summarization.
Supports OpenAI, Google Gemini, and OpenRouter models.
"""

from __future__ import annotations
import asyncio
import json
import math
import os
import re
from typing import Any, Literal, Sequence

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
    "minimax/minimax-m3:nitro",
    "z-ai/glm-5.2:nitro",
]
_OPENROUTER_DEFAULT_MODELS = ("minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro")
_MODEL_OUTPUT_TOKEN_CAPS = {
    "gpt-5-nano": 4096,
    "gpt-5-mini": 8192,
    "gpt-5.2": 8192,
    "gemini-flash-latest": 65536,
    "gemini-3.5-flash": 65536,
    "gemini-3-flash-preview": 8192,
    "gemini-3.1-flash-lite-preview": 8192,
    "gemini-3-pro-preview": 12288,
    "minimax/minimax-m3:nitro": 8192,
    "z-ai/glm-5.2:nitro": 8192,
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
        "gemini hit max_tokens",
        "finish_reason=max_tokens",
        "resource_exhausted",
        "unavailable",
        "high demand",
        "rate limit",
        "timeout",
    )
    return any(marker in lower for marker in retry_markers)


def _should_fallback_to_openai() -> bool:
    # Cross-provider fallback is surprising in the UI: if Gemini is selected,
    # users should see Gemini errors unless they explicitly opt into fallback.
    return os.getenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", "0").strip().lower() in {"1", "true", "yes"}


def _should_fallback_to_openrouter() -> bool:
    return os.getenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _is_openrouter_model(model: str) -> bool:
    return "/" in (model or "") and not model.startswith(("http://", "https://"))


def _openrouter_nitro_model(model: str) -> str:
    raw = (model or "").strip()
    if not raw:
        return _OPENROUTER_DEFAULT_MODELS[0]
    base = raw.split(":", 1)[0]
    return f"{base}:nitro"


def _openrouter_model_family(model: str) -> str:
    base = (model or "").strip().split(":", 1)[0].lower()
    return re.sub(r"-\d{8}$", "", base)


def _openrouter_fallback_models() -> list[str]:
    raw = os.getenv("SCRIBER_SUMMARY_OPENROUTER_FALLBACK_MODELS", "").strip()
    candidates = [item.strip() for item in raw.split(",") if item.strip()] if raw else list(_OPENROUTER_DEFAULT_MODELS)
    normalized: list[str] = []
    for candidate in candidates:
        model = _openrouter_nitro_model(candidate)
        if model and model not in normalized:
            normalized.append(model)
    return normalized or list(_OPENROUTER_DEFAULT_MODELS)


def _openrouter_model_candidates(models: str | Sequence[str]) -> list[str]:
    raw_models = [models] if isinstance(models, str) else list(models)
    normalized: list[str] = []
    for candidate in raw_models:
        model = _openrouter_nitro_model(str(candidate or ""))
        if model and model not in normalized:
            normalized.append(model)
    return normalized or list(_OPENROUTER_DEFAULT_MODELS)


def _same_openrouter_model(left: str, right: str) -> bool:
    return _openrouter_model_family(left) == _openrouter_model_family(right)


def _is_openrouter_reasoning_model(model: str) -> bool:
    raw = os.getenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_MODELS", "z-ai/glm-5.2").strip()
    families = {_openrouter_model_family(item) for item in raw.split(",") if item.strip()}
    return _openrouter_model_family(model) in families


def _is_gemini_thinking_model(model: str) -> bool:
    return model.startswith("gemini-3") or model == "gemini-flash-latest"


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


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
    max_words = max(min_words, int(os.getenv("SCRIBER_SUMMARY_MAX_WORDS", "3200")))
    short_input_max_words = max(1, int(os.getenv("SCRIBER_SUMMARY_SHORT_INPUT_MAX_WORDS", "2500")))
    short_min_words = max(min_words, int(os.getenv("SCRIBER_SUMMARY_SHORT_MIN_WORDS", "450")))
    target_words = int(round(input_words * ratio))
    target_words = max(min_words, min(max_words, target_words))
    if input_words <= short_input_max_words:
        target_words = max(target_words, short_min_words)

    # Approximate model tokens needed for the requested output length.
    # Defaults are intentionally generous to avoid clipping long summaries.
    token_multiplier = max(1.0, float(os.getenv("SCRIBER_SUMMARY_TOKEN_MULTIPLIER", "2.2")))
    token_overhead = max(0, int(os.getenv("SCRIBER_SUMMARY_TOKEN_OVERHEAD", "320")))
    min_tokens = max(256, int(os.getenv("SCRIBER_SUMMARY_MIN_OUTPUT_TOKENS", "1024")))
    max_tokens = max(min_tokens, int(os.getenv("SCRIBER_SUMMARY_MAX_OUTPUT_TOKENS", "8192")))
    short_min_tokens = max(min_tokens, int(os.getenv("SCRIBER_SUMMARY_SHORT_MIN_OUTPUT_TOKENS", "1600")))

    model_key = _openrouter_nitro_model(model) if _is_openrouter_model(model) else model
    model_cap = _MODEL_OUTPUT_TOKEN_CAPS.get(model_key, max_tokens)
    budget_cap = max(min_tokens, min(max_tokens, model_cap))

    requested_tokens = int(math.ceil(target_words * token_multiplier)) + token_overhead
    if input_words <= short_input_max_words:
        requested_tokens = max(requested_tokens, short_min_tokens)
    output_tokens = max(min_tokens, min(budget_cap, requested_tokens))

    # For very long recordings (e.g. >30 min), allow a larger first-pass output.
    long_video_min_seconds = max(1, int(os.getenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")))
    long_video_token_bonus = max(0, int(os.getenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "1500")))
    if duration_seconds and duration_seconds >= long_video_min_seconds and long_video_token_bonus > 0:
        output_tokens = min(budget_cap, output_tokens + long_video_token_bonus)

    # Gemini 3 uses hidden "thinking" budget within max_output_tokens.
    # Reserve additional tokens so visible output is not cut to 1-2 lines.
    if _is_gemini_thinking_model(model):
        thinking_reserve = max(0, int(os.getenv("SCRIBER_SUMMARY_GEMINI_THINKING_RESERVE_TOKENS", "2400")))
        if thinking_reserve > 0:
            output_tokens = min(budget_cap, output_tokens + thinking_reserve)

    # Some OpenRouter models, currently GLM 5.2, spend completion tokens on
    # hidden/provider reasoning before emitting visible content.
    if _is_openrouter_model(model) and _is_openrouter_reasoning_model(model_key):
        reasoning_reserve = _env_int(
            "SCRIBER_SUMMARY_OPENROUTER_REASONING_RESERVE_TOKENS",
            4096,
            min_value=0,
        )
        if reasoning_reserve > 0:
            output_tokens = min(budget_cap, output_tokens + reasoning_reserve)
        reasoning_min_tokens = _env_int(
            "SCRIBER_SUMMARY_OPENROUTER_REASONING_MIN_OUTPUT_TOKENS",
            6144,
            min_value=min_tokens,
        )
        output_tokens = min(budget_cap, max(output_tokens, reasoning_min_tokens))

    return input_words, target_words, output_tokens


def _dynamic_length_instruction(input_words: int, target_words: int) -> str:
    return (
        "Zusätzliche Längenregel (automatisch): "
        f"Der Input hat ungefähr {input_words} Wörter. "
        f"Erstelle eine inhaltlich vollständige Zusammenfassung mit ungefähr {target_words} Wörtern (Toleranz ±15%). "
        "Nutze das verfügbare Ausgabebudget großzügig, statt künstlich kurz zu bleiben. "
        "Bei langen Inputs sollen alle Hauptthemen, Entscheidungen, offenen Punkte und relevanten Details enthalten sein. "
        "Beende die Antwort immer mit einem vollständig abgeschlossenen Satz und Abschnitt."
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


def _gemini_thinking_level_for_model(model: str) -> str | None:
    if not _is_gemini_thinking_model(model):
        return None
    raw = os.getenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", "medium").strip().lower()
    if raw == "":
        return None
    if raw not in {"minimal", "low", "medium", "high"}:
        logger.warning(
            "Invalid SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL='{}'; using 'medium'.",
            raw,
        )
        raw = "medium"
    return raw.upper()


def _gemini_retry_output_cap(model: str, initial_max_output_tokens: int) -> int:
    model_cap = _MODEL_OUTPUT_TOKEN_CAPS.get(model, max(initial_max_output_tokens, 16_384))
    requested_cap = _env_int(
        "SCRIBER_SUMMARY_GEMINI_RETRY_MAX_OUTPUT_TOKENS",
        16_384,
        min_value=initial_max_output_tokens,
    )
    return max(initial_max_output_tokens, min(model_cap, requested_cap))


def _gemini_next_output_budget(current_tokens: int, retry_cap: int) -> int:
    growth = _env_float("SCRIBER_SUMMARY_GEMINI_MAX_TOKENS_RETRY_GROWTH", 2.0, min_value=1.1)
    grown = int(math.ceil(current_tokens * growth))
    return min(retry_cap, max(current_tokens + 512, grown))


async def _summarize_with_model(prompt: str, model: str, max_output_tokens: int) -> str:
    if model.startswith("gpt-"):
        return await _summarize_openai(prompt, model, max_output_tokens)
    if model.startswith("gemini-"):
        return await _summarize_gemini(prompt, model, max_output_tokens)
    if _is_openrouter_model(model):
        return await _summarize_openrouter(prompt, model, max_output_tokens)
    raise ValueError(f"Unknown summarization model: {model}")


async def _try_openrouter_summary_fallback(
    prompt: str,
    *,
    primary_model: str,
    primary_error: Exception,
    max_output_tokens: int,
    timeout_seconds: float,
) -> str | None:
    if _is_openrouter_model(primary_model):
        return None
    if not _should_fallback_to_openrouter():
        return None
    if not (getattr(Config, "OPENROUTER_API_KEY", "") or "").strip():
        return None

    fallback_models = _openrouter_fallback_models()
    logger.warning(
        "Summarization with {} failed ({}). Falling back to OpenRouter models {}.",
        primary_model,
        primary_error,
        fallback_models,
    )
    try:
        return await asyncio.wait_for(
            _summarize_openrouter(prompt, fallback_models, max_output_tokens),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        timeout_display = max(1, int(round(timeout_seconds)))
        raise RuntimeError(
            f"{primary_model} summarization failed and OpenRouter fallback timed out after {timeout_display}s."
        ) from exc
    except Exception as fallback_exc:
        raise RuntimeError(
            f"{primary_model} summarization failed and the OpenRouter fallback also failed: {fallback_exc}"
        ) from fallback_exc


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
    if _is_openrouter_model(model):
        model = _openrouter_nitro_model(model)
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

    try:
        summary = await asyncio.wait_for(
            _summarize_with_model(full_prompt, model, output_tokens),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        timeout_display = max(1, int(round(timeout_seconds)))
        logger.error(
            "Summarization timed out after {}s (model={})",
            timeout_seconds,
            model,
        )
        timeout_error = RuntimeError(
            f"Summarization timed out after {timeout_display}s. Please try again."
        )
        fallback = await _try_openrouter_summary_fallback(
            full_prompt,
            primary_model=model,
            primary_error=timeout_error,
            max_output_tokens=output_tokens,
            timeout_seconds=timeout_seconds,
        )
        if fallback is not None:
            summary = fallback
        else:
            raise timeout_error from exc
    except Exception as exc:
        fallback = await _try_openrouter_summary_fallback(
            full_prompt,
            primary_model=model,
            primary_error=exc,
            max_output_tokens=output_tokens,
            timeout_seconds=timeout_seconds,
        )
        if fallback is not None:
            summary = fallback
        # Gemini can occasionally return transient 429/503 ("high demand").
        # The legacy OpenAI fallback remains opt-in for existing power users,
        # but OpenRouter is the default automatic fallback when configured.
        elif isinstance(exc, RuntimeError):
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
                    except Exception as fallback_exc:
                        raise RuntimeError(
                            "Gemini summarization failed and the configured OpenAI fallback also failed: "
                            f"{fallback_exc}"
                        ) from fallback_exc
                else:
                    raise
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


def _build_openrouter_payload(
    prompt: str,
    models: str | Sequence[str],
    max_output_tokens: int,
) -> dict[str, Any]:
    normalized_models = _openrouter_model_candidates(models)

    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
        "temperature": 0.3,
        "reasoning": _openrouter_reasoning_config(),
    }
    if len(normalized_models) == 1:
        payload["model"] = normalized_models[0]
    else:
        payload["models"] = normalized_models
    return payload


def _openrouter_error_detail(status: int, raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:600]

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        parts = []
        code = error.get("code")
        message = error.get("message")
        if code is not None:
            parts.append(str(code))
        if message:
            parts.append(str(message))
        if parts:
            return ": ".join(parts)[:600]
    return raw[:600] or str(status)


def _openrouter_reasoning_config() -> dict[str, Any]:
    config: dict[str, Any] = {"exclude": True}
    raw = os.getenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", "medium").strip().lower()
    if not raw:
        return config
    allowed = {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
    if raw not in allowed:
        logger.warning(
            "Invalid SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT='{}'; using 'medium'.",
            raw,
        )
        raw = "medium"
    config["effort"] = raw
    return config


def _extract_openrouter_message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
                    continue
                nested_content = part.get("content")
                if isinstance(nested_content, str):
                    chunks.append(nested_content)
        return "".join(chunks)
    return ""


def _openrouter_usage_summary(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
    }


def _openrouter_choice_diagnostics(choice: dict[str, Any]) -> dict[str, Any]:
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    error = choice.get("error") if isinstance(choice, dict) else None
    if isinstance(error, dict):
        choice_error = {
            "code": error.get("code"),
            "message": str(error.get("message") or "")[:240] or None,
        }
    else:
        choice_error = None

    reasoning = message.get("reasoning")
    reasoning_details = message.get("reasoning_details")
    content_type = type(content).__name__ if content is not None else "None"
    return {
        "finish_reason": choice.get("finish_reason"),
        "native_finish_reason": choice.get("native_finish_reason"),
        "content_type": content_type,
        "content_chars": len(_extract_openrouter_message_content(message).strip()),
        "reasoning_chars": len(reasoning.strip()) if isinstance(reasoning, str) else None,
        "reasoning_details_count": len(reasoning_details) if isinstance(reasoning_details, list) else None,
        "error": choice_error,
    }


def _extract_openrouter_response_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = _extract_openrouter_message_content(message).strip()
        if content:
            return content
    return ""


def _openrouter_empty_response_detail(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    choice_detail = _openrouter_choice_diagnostics(first_choice) if isinstance(first_choice, dict) else {}
    usage = _openrouter_usage_summary(data)
    detail = {
        "model": data.get("model") if isinstance(data, dict) else None,
        "choice": choice_detail,
        "usage": usage,
    }
    return json.dumps(detail, ensure_ascii=True, sort_keys=True)


def _openrouter_primary_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") if isinstance(data, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    return first_choice if isinstance(first_choice, dict) else {}


def _openrouter_should_retry_with_more_tokens(data: dict[str, Any]) -> bool:
    choice = _openrouter_primary_choice(data)
    finish_reason = str(choice.get("finish_reason") or "").lower()
    native_finish_reason = str(choice.get("native_finish_reason") or "").lower()
    return finish_reason == "length" or native_finish_reason == "length"


async def _post_openrouter_chat_completion(
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: aiohttp.ClientTimeout,
) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                detail = _openrouter_error_detail(resp.status, raw)
                raise RuntimeError(f"OpenRouter API error {resp.status}: {detail}")
            return json.loads(raw)


def _openrouter_used_model(data: dict[str, Any], fallback: str) -> str:
    model = data.get("model") if isinstance(data, dict) else None
    return str(model or fallback)


def _openrouter_retry_candidates(
    attempted: Sequence[str],
    *,
    used_model: str,
    allow_default_fallbacks: bool,
) -> list[str]:
    source = list(attempted)
    if allow_default_fallbacks:
        source.extend(_openrouter_fallback_models())
    retry: list[str] = []
    for candidate in source:
        if used_model and _same_openrouter_model(candidate, used_model):
            continue
        model = _openrouter_nitro_model(candidate)
        if model and model not in retry:
            retry.append(model)
    return retry


def _openrouter_retry_output_cap(models: Sequence[str], initial_max_output_tokens: int) -> int:
    model_caps = [
        _MODEL_OUTPUT_TOKEN_CAPS.get(_openrouter_nitro_model(model), initial_max_output_tokens)
        for model in models
    ]
    model_cap = max(model_caps) if model_caps else initial_max_output_tokens
    requested_cap = _env_int(
        "SCRIBER_SUMMARY_OPENROUTER_RETRY_MAX_TOKENS",
        8192,
        min_value=initial_max_output_tokens,
    )
    return max(initial_max_output_tokens, min(model_cap, requested_cap))


def _openrouter_next_output_budget(
    current_tokens: int,
    retry_cap: int,
    data: dict[str, Any],
) -> int:
    usage = _openrouter_usage_summary(data)
    completion_tokens = usage.get("completion_tokens")
    reasoning_tokens = usage.get("reasoning_tokens")
    minimum_increment = 512
    if isinstance(reasoning_tokens, int) and reasoning_tokens > 0:
        minimum_increment = max(minimum_increment, reasoning_tokens)
    if isinstance(completion_tokens, int) and completion_tokens > current_tokens:
        current_tokens = completion_tokens
    growth = _env_float("SCRIBER_SUMMARY_OPENROUTER_MAX_TOKENS_RETRY_GROWTH", 2.0, min_value=1.1)
    grown = int(math.ceil(current_tokens * growth))
    return min(retry_cap, max(current_tokens + minimum_increment, grown))


async def _summarize_openrouter(
    prompt: str,
    models: str | Sequence[str],
    max_output_tokens: int,
) -> str:
    """Summarize through OpenRouter Chat Completions."""
    api_key = getattr(Config, "OPENROUTER_API_KEY", "") or ""
    if not api_key:
        raise ValueError("OpenRouter API key not configured. Please add it in Settings.")

    timeout_seconds = _summary_timeout_seconds()
    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=min(15, timeout_seconds),
        sock_connect=min(15, timeout_seconds),
        sock_read=timeout_seconds,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://scriber.local",
        "X-OpenRouter-Title": "Scriber",
    }
    requested_models = _openrouter_model_candidates(models)
    if isinstance(models, str):
        initial_models = _openrouter_retry_candidates(
            requested_models,
            used_model="",
            allow_default_fallbacks=True,
        )
    else:
        initial_models = requested_models

    attempts: list[list[str]] = [initial_models]
    attempt_budgets: list[int] = [max_output_tokens]
    seen_attempts: set[tuple[tuple[str, ...], int]] = {(tuple(initial_models), max_output_tokens)}
    last_empty_detail = ""
    retry_cap = _openrouter_retry_output_cap(initial_models, max_output_tokens)

    try:
        attempt_index = 0
        while attempt_index < len(attempts):
            attempt_models = attempts[attempt_index]
            attempt_max_tokens = attempt_budgets[attempt_index]
            payload = _build_openrouter_payload(prompt, attempt_models, attempt_max_tokens)
            data = await _post_openrouter_chat_completion(payload, headers, timeout)

            content = _extract_openrouter_response_text(data).strip()
            used_model = _openrouter_used_model(data, attempt_models[0])
            logger.info(
                "OpenRouter summarization complete: {} chars (requested_models={}, response_model={})",
                len(content or ""),
                payload.get("model") or payload.get("models"),
                used_model,
            )

            if _openrouter_should_retry_with_more_tokens(data) and attempt_max_tokens < retry_cap:
                last_empty_detail = _openrouter_empty_response_detail(data)
                next_max_tokens = _openrouter_next_output_budget(attempt_max_tokens, retry_cap, data)
                key = (tuple(attempt_models), next_max_tokens)
                if key not in seen_attempts:
                    logger.warning(
                        "OpenRouter stopped due length from {} at max_tokens={}. Retrying with max_tokens={}. detail={}",
                        used_model,
                        attempt_max_tokens,
                        next_max_tokens,
                        last_empty_detail,
                    )
                    attempts.append(attempt_models)
                    attempt_budgets.append(next_max_tokens)
                    seen_attempts.add(key)
                    attempt_index += 1
                    continue

            if content and not _openrouter_should_retry_with_more_tokens(data):
                return content

            last_empty_detail = _openrouter_empty_response_detail(data)
            retry_models = _openrouter_retry_candidates(
                attempt_models,
                used_model=used_model,
                allow_default_fallbacks=isinstance(models, str),
            )
            retry_key = (tuple(retry_models), attempt_max_tokens)
            if retry_models and retry_key not in seen_attempts:
                logger.warning(
                    "OpenRouter returned incomplete or empty response from {}. Retrying with {}. detail={}",
                    used_model,
                    retry_models,
                    last_empty_detail,
                )
                attempts.append(retry_models)
                attempt_budgets.append(attempt_max_tokens)
                seen_attempts.add(retry_key)
                attempt_index += 1
                continue
            if _openrouter_should_retry_with_more_tokens(data):
                raise RuntimeError(
                    "OpenRouter hit max_tokens before completing the summary "
                    f"(max_tokens={attempt_max_tokens}, detail={last_empty_detail}). "
                    "The partial summary was discarded to avoid saving truncated content."
                )
            break

        raise RuntimeError(f"OpenRouter returned empty response. detail={last_empty_detail}")
    except aiohttp.ClientError as e:
        logger.exception("OpenRouter summarization HTTP error")
        raise RuntimeError(f"OpenRouter summarization failed: {e}")
    except json.JSONDecodeError as e:
        logger.exception("OpenRouter summarization parse error")
        raise RuntimeError(f"OpenRouter response parse failed: {e}")


def _build_gemini_payload(prompt: str, model: str, max_output_tokens: int) -> dict[str, Any]:
    generation_config: dict[str, Any] = {
        "maxOutputTokens": max_output_tokens,
    }
    temperature_raw = os.getenv("SCRIBER_SUMMARY_GEMINI_TEMPERATURE", "").strip()
    if temperature_raw:
        generation_config["temperature"] = min(1.0, max(0.0, float(temperature_raw)))

    thinking_level = _gemini_thinking_level_for_model(model)
    if thinking_level is not None:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}

    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


async def _post_gemini_generate_content(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    *,
    retries: int,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    last_error: RuntimeError | None = None

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
    return data


def _extract_gemini_response(data: dict[str, Any]) -> tuple[str, str | None, Any, Any]:
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
    return content, finish_reason, candidate_tokens, total_tokens


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
        http_retries = _env_int("SCRIBER_SUMMARY_GEMINI_RETRIES", 2, min_value=0)
        max_token_retries = _env_int("SCRIBER_SUMMARY_GEMINI_MAX_TOKENS_RETRIES", 2, min_value=0)
        retry_cap = _gemini_retry_output_cap(model, max_output_tokens)
        current_max_output_tokens = max_output_tokens

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for max_token_attempt in range(max_token_retries + 1):
                payload = _build_gemini_payload(prompt, model, current_max_output_tokens)
                data = await _post_gemini_generate_content(
                    session,
                    url,
                    payload,
                    retries=http_retries,
                )
                content, finish_reason, candidate_tokens, total_tokens = _extract_gemini_response(data)

                logger.info(
                    "Gemini summarization complete: {} chars (finish_reason={}, candidate_tokens={}, total_tokens={}, max_output_tokens={})",
                    len(content or ""),
                    finish_reason,
                    candidate_tokens,
                    total_tokens,
                    current_max_output_tokens,
                )

                if finish_reason == "MAX_TOKENS":
                    if max_token_attempt < max_token_retries and current_max_output_tokens < retry_cap:
                        next_max_output_tokens = _gemini_next_output_budget(current_max_output_tokens, retry_cap)
                        logger.warning(
                            "Gemini stopped due MAX_TOKENS (max_output_tokens={}, candidate_tokens={}, total_tokens={}). Retrying with max_output_tokens={} and thinkingLevel={}.",
                            current_max_output_tokens,
                            candidate_tokens,
                            total_tokens,
                            next_max_output_tokens,
                            _gemini_thinking_level_for_model(model),
                        )
                        current_max_output_tokens = next_max_output_tokens
                        continue

                    raise RuntimeError(
                        "Gemini hit MAX_TOKENS before completing the summary "
                        f"(finish_reason=MAX_TOKENS, max_output_tokens={current_max_output_tokens}, "
                        f"candidate_tokens={candidate_tokens}, total_tokens={total_tokens}). "
                        "The partial summary was discarded to avoid saving truncated content."
                    )

                if not content:
                    prompt_feedback = data.get("promptFeedback") if isinstance(data, dict) else None
                    raise RuntimeError(f"Gemini returned empty response. promptFeedback={prompt_feedback}")

                return content

        raise RuntimeError("Gemini summarization failed before returning a response.")

    except aiohttp.ClientError as e:
        logger.exception("Gemini summarization HTTP error")
        raise RuntimeError(f"Gemini summarization failed: {e}")
    except json.JSONDecodeError as e:
        logger.exception("Gemini summarization parse error")
        raise RuntimeError(f"Gemini response parse failed: {e}")
    except RuntimeError:
        raise
    except Exception as e:
        logger.exception("Gemini summarization failed")
        raise RuntimeError(f"Gemini summarization failed: {e}")

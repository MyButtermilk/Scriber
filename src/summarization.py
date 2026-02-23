"""
LLM-based transcript summarization.
Supports OpenAI (GPT-4) and Google Gemini models.
"""

from __future__ import annotations
import asyncio
import math
import os
from typing import Literal

from loguru import logger

from src.config import Config

SummarizationModel = Literal["gemini-3-flash-preview", "gemini-3-pro-preview", "gpt-5.2", "gpt-5-mini", "gpt-5-nano"]


def _summary_budget_for_text(text: str, model: str) -> tuple[int, int, int]:
    """
    Derive dynamic summary length and token budget from transcript size.

    Returns:
        tuple: (input_word_count, target_summary_words, max_output_tokens)
    """
    input_words = max(1, len((text or "").split()))

    # Higher compression for very long transcripts, lower compression for short ones.
    if input_words <= 800:
        ratio = 0.22
    elif input_words <= 2_000:
        ratio = 0.18
    elif input_words <= 5_000:
        ratio = 0.15
    elif input_words <= 10_000:
        ratio = 0.12
    else:
        ratio = 0.10

    min_words = max(80, int(os.getenv("SCRIBER_SUMMARY_MIN_WORDS", "180")))
    max_words = max(min_words, int(os.getenv("SCRIBER_SUMMARY_MAX_WORDS", "2200")))
    target_words = int(round(input_words * ratio))
    target_words = max(min_words, min(max_words, target_words))

    # Approximate model tokens needed for the requested output length.
    # Defaults are intentionally generous to avoid clipping long summaries.
    token_multiplier = max(1.0, float(os.getenv("SCRIBER_SUMMARY_TOKEN_MULTIPLIER", "1.8")))
    token_overhead = max(0, int(os.getenv("SCRIBER_SUMMARY_TOKEN_OVERHEAD", "220")))
    min_tokens = max(256, int(os.getenv("SCRIBER_SUMMARY_MIN_OUTPUT_TOKENS", "512")))
    max_tokens = max(min_tokens, int(os.getenv("SCRIBER_SUMMARY_MAX_OUTPUT_TOKENS", "8192")))

    model_caps = {
        "gpt-5-nano": 4096,
        "gpt-5-mini": 8192,
        "gpt-5.2": 8192,
        "gemini-3-flash-preview": 8192,
        "gemini-3-pro-preview": 12288,
    }
    model_cap = model_caps.get(model, max_tokens)
    budget_cap = max(min_tokens, min(max_tokens, model_cap))

    requested_tokens = int(math.ceil(target_words * token_multiplier)) + token_overhead
    output_tokens = max(min_tokens, min(budget_cap, requested_tokens))

    return input_words, target_words, output_tokens


def _dynamic_length_instruction(input_words: int, target_words: int) -> str:
    return (
        "Zusätzliche Längenregel (automatisch): "
        f"Der Input hat ungefähr {input_words} Wörter. "
        f"Erstelle eine inhaltlich vollständige Zusammenfassung mit ungefähr {target_words} Wörtern (Toleranz ±15%). "
        "Bei langen Inputs sollen alle Hauptthemen, Entscheidungen, offenen Punkte und relevanten Details enthalten sein."
    )


async def summarize_text(text: str, model: SummarizationModel | None = None) -> str:
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
    
    model = model or getattr(Config, "SUMMARIZATION_MODEL", "gemini-3-flash-preview")
    base_prompt = Config.SUMMARIZATION_PROMPT or "Summarize the following transcript:"
    input_words, target_words, output_tokens = _summary_budget_for_text(text, model)
    length_instruction = _dynamic_length_instruction(input_words, target_words)
    full_prompt = f"{base_prompt}\n\n{length_instruction}\n\n{text}"
    
    logger.info(
        "Summarizing transcript with {} ({} chars, ~{} words, target ~{} words, max_output_tokens={})",
        model,
        len(text),
        input_words,
        target_words,
        output_tokens,
    )
    
    if model.startswith("gpt-"):
        return await _summarize_openai(full_prompt, model, output_tokens)
    elif model.startswith("gemini-"):
        return await _summarize_gemini(full_prompt, model, output_tokens)
    else:
        raise ValueError(f"Unknown summarization model: {model}")


async def _summarize_openai(prompt: str, model: str, max_output_tokens: int) -> str:
    """Summarize using OpenAI API."""
    api_key = Config.OPENAI_API_KEY
    if not api_key:
        raise ValueError("OpenAI API key not configured. Please add it in Settings.")
    
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai library not installed. Run: pip install openai")
    
    client = openai.AsyncOpenAI(api_key=api_key)
    
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_output_tokens,
            temperature=0.3,
        )
        
        content = response.choices[0].message.content
        logger.info(f"OpenAI summarization complete: {len(content or '')} chars")
        return content or ""
        
    except openai.APIError as e:
        logger.error(f"OpenAI API error: {e}")
        raise RuntimeError(f"OpenAI API error: {e}")
    except Exception as e:
        logger.exception("OpenAI summarization failed")
        raise RuntimeError(f"OpenAI summarization failed: {e}")


async def _summarize_gemini(prompt: str, model: str, max_output_tokens: int) -> str:
    """Summarize using Google Gemini API."""
    api_key = Config.GOOGLE_API_KEY
    if not api_key:
        raise ValueError("Gemini API key not configured. Please add it in Settings.")
    
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai library not installed. Run: pip install google-generativeai")
    
    genai.configure(api_key=api_key)
    
    try:
        # Run in executor since genai is synchronous
        loop = asyncio.get_running_loop()
        
        def _generate():
            gemini_model = genai.GenerativeModel(model)
            response = gemini_model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.3,
                    "max_output_tokens": max_output_tokens,
                },
            )
            return response.text
        
        content = await loop.run_in_executor(None, _generate)
        logger.info(f"Gemini summarization complete: {len(content or '')} chars")
        return content or ""
        
    except Exception as e:
        logger.exception("Gemini summarization failed")
        raise RuntimeError(f"Gemini summarization failed: {e}")

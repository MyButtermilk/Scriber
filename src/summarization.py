"""
LLM-based transcript summarization.
Supports OpenAI (GPT-4) and Google Gemini models.
"""

from __future__ import annotations
import asyncio
from typing import Literal

from loguru import logger

from src.config import Config

SummarizationModel = Literal["gemini-3-pro-preview", "gemini-flash-latest", "gpt-5.2", "gpt-5-mini", "gpt-5-nano"]


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
    
    model = model or getattr(Config, "SUMMARIZATION_MODEL", "gemini-flash-latest")
    prompt = Config.SUMMARIZATION_PROMPT or "Summarize the following transcript:"
    full_prompt = f"{prompt}\n\n{text}"
    
    logger.info(f"Summarizing transcript with {model} ({len(text)} chars)")
    
    if model.startswith("gpt-"):
        return await _summarize_openai(full_prompt, model)
    elif model.startswith("gemini-"):
        return await _summarize_gemini(full_prompt, model)
    else:
        raise ValueError(f"Unknown summarization model: {model}")


async def _summarize_openai(prompt: str, model: str) -> str:
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
            max_tokens=4096,
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


async def _summarize_gemini(prompt: str, model: str) -> str:
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
            response = gemini_model.generate_content(prompt)
            return response.text
        
        content = await loop.run_in_executor(None, _generate)
        logger.info(f"Gemini summarization complete: {len(content or '')} chars")
        return content or ""
        
    except Exception as e:
        logger.exception("Gemini summarization failed")
        raise RuntimeError(f"Gemini summarization failed: {e}")

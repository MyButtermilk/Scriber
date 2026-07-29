"""Deterministic local inference with fail-closed SST and protected-span handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .protected_spans import protect_spans, restore_spans
from .sst_parser import parse_sst
from .target_renderer import render_plain_text
from .tokenize import POLISHING_INSTRUCTION


def _token_ids(tokenizer: Any, text: str) -> list[Any]:
    messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]
    token_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()
    if token_ids and isinstance(token_ids[0], list):
        token_ids = token_ids[0]
    return list(token_ids)


def polish_transcript(
    transcript: str,
    *,
    tokenizer: Any,
    model: Any,
    max_new_tokens: int = 512,
    validator: Callable[[str], bool] | None = None,
) -> str:
    """Polish only a valid SST result; any unsafe result returns the original input."""
    protected = protect_spans(transcript)
    try:
        input_ids = _token_ids(tokenizer, protected.text)
        generated = model.generate(input_ids, do_sample=False, num_beams=1, max_new_tokens=max_new_tokens)
        if hasattr(generated, "tolist"):
            generated = generated.tolist()
        if generated and isinstance(generated[0], list):
            generated = generated[0]
        completion = tokenizer.decode(generated[len(input_ids) :], skip_special_tokens=True)
        restored = restore_spans(protected, completion)
        if validator is not None and not validator(restored):
            raise ValueError("polishing output did not satisfy the validator")
        return render_plain_text(parse_sst(restored))
    except (TypeError, ValueError):
        return transcript

"""Tokenizer-native conversational encoding for Gemma SFT."""

from typing import Any

POLISHING_INSTRUCTION = (
    "Bereinige dieses Transkript konservativ. Gib ausschließlich gültiges SST v1 aus; "
    "erhalte Bedeutung, Reihenfolge und geschützte Platzhalter exakt.\n\nTranskript:\n"
)
IGNORE_INDEX = -100


def build_training_messages(transcript: str, completion: str) -> list[dict[str, str]]:
    """Build a Gemma-compatible dialogue; Gemma deliberately has no system turn."""
    return [
        {"role": "user", "content": f"{POLISHING_INSTRUCTION}{transcript}"},
        {"role": "assistant", "content": completion},
    ]


def _chat_token_ids(tokenizer: Any, messages: list[dict[str, str]], *, add_generation_prompt: bool) -> list[int]:
    token_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=add_generation_prompt)
    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()
    if token_ids and isinstance(token_ids[0], list):
        token_ids = token_ids[0]
    return list(token_ids)


def tokenize_example(
    tokenizer: Any, *, transcript: str, completion: str, max_length: int | None = None
) -> dict[str, list[int]]:
    """Encode one SFT example and mask every token before the assistant completion."""
    messages = build_training_messages(transcript, completion)
    prompt_ids = _chat_token_ids(tokenizer, messages[:1], add_generation_prompt=True)
    input_ids = _chat_token_ids(tokenizer, messages, add_generation_prompt=False)
    if input_ids[: len(prompt_ids)] != prompt_ids:
        raise ValueError("tokenizer chat template does not preserve the generation prompt prefix")
    if max_length is not None:
        input_ids = input_ids[:max_length]
    labels = [IGNORE_INDEX] * min(len(prompt_ids), len(input_ids)) + input_ids[len(prompt_ids) :]
    if not any(label != IGNORE_INDEX for label in labels):
        raise ValueError("completion was truncated completely")
    return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids), "labels": labels}

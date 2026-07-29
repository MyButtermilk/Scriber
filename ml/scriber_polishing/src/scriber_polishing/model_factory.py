"""Model-loading seams for the fixed Gemma polishing base."""

from collections.abc import Callable, Mapping
from typing import Any

BASE_MODEL_ID = "google/gemma-3-270m-it"


def assert_base_model(model_id: str) -> str:
    """Reject every base model other than the reviewed Gemma instruction model."""
    if model_id != BASE_MODEL_ID:
        raise ValueError(f"the polishing model must use {BASE_MODEL_ID}")
    return model_id


def load_model_and_tokenizer(
    model_loader: Callable[..., Any],
    tokenizer_loader: Callable[..., Any],
    *,
    model_id: str = BASE_MODEL_ID,
    **kwargs: Any,
) -> tuple[Any, Any]:
    """Load the exact base at the injectable Hugging Face boundary."""
    base_model = assert_base_model(model_id)
    return model_loader(base_model, **kwargs), tokenizer_loader(base_model)


def run_forward_smoke(model: Callable[..., Any], batch: Mapping[str, Any]) -> Any:
    """Exercise a model's public forward call with pre-tokenized supervised data."""
    if "input_ids" not in batch or "labels" not in batch:
        raise ValueError("forward smoke requires input_ids and completion-only labels")
    return model(**dict(batch))

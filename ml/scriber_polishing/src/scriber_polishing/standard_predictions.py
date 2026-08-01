"""Deterministic, non-model prediction producers for frozen evaluations."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .document_ast import Block, BlockType, Document
from .sst_renderer import render_sst

STANDARD_CANDIDATES = frozenset({"raw_transcript", "deterministic_minimal"})
_FILLER = re.compile(r"(?<![\wÄÖÜäöüß])(?:ähm|äh|hmm|hm)(?![\wÄÖÜäöüß])", re.IGNORECASE)
_RESERVED_SST_TOKEN = re.compile(r"\[/?[A-Z0-9_]+\]")
_MAX_PREDICTION_LENGTH = 30_000


def _protect_spans(text: str, values: Iterable[object]) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}
    result = text
    for index, value in enumerate(
        sorted(
            (value for value in values if isinstance(value, str) and value and value in text),
            key=len,
            reverse=True,
        )
    ):
        placeholder = f"\ufff0{index}\ufff1"
        while placeholder in result:
            placeholder = f"\ufff0{placeholder}\ufff1"
        result = result.replace(value, placeholder)
        protected[placeholder] = value
    return result, protected


def _capitalize_initial(text: str) -> str:
    for index, value in enumerate(text):
        if value.isalpha():
            return f"{text[:index]}{value.upper()}{text[index + 1:]}"
    return text


def _escape_reserved_sst_tokens(text: str) -> str:
    return _RESERVED_SST_TOKEN.sub(
        lambda match: match.group(0).replace("[", "［").replace("]", "］"),
        text,
    )


def minimal_polish(source_text: str, protected_spans: Iterable[object] = ()) -> str:
    """Apply only documented non-semantic cleanup before wrapping one paragraph SST block."""

    protected_text, placeholders = _protect_spans(source_text.replace("\r\n", "\n"), protected_spans)
    cleaned = _FILLER.sub(" ", protected_text)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = " ".join(source_text.replace("\r\n", "\n").split())
    cleaned = _capitalize_initial(cleaned)
    if cleaned and cleaned[-1] not in ".!?;:":
        cleaned += "."
    for placeholder, value in placeholders.items():
        cleaned = cleaned.replace(placeholder, value)
    return _escape_reserved_sst_tokens(cleaned)


def standard_prediction_sst(reference: Mapping[str, Any], *, candidate: str) -> str:
    """Create one baseline output without a model, network call, or mutable runtime state."""

    if candidate not in STANDARD_CANDIDATES:
        raise ValueError(f"unsupported standard candidate: {candidate}")
    source_text = reference.get("source_text")
    if not isinstance(source_text, str) or not source_text:
        raise ValueError("reference source_text must be a non-empty string")
    if candidate == "raw_transcript":
        return source_text
    prediction = render_sst(
        Document(
            blocks=(
                Block(
                    type=BlockType.PARAGRAPH,
                    text=minimal_polish(source_text, reference.get("protected_spans", ())),
                ),
            )
        )
    )
    if len(prediction) > _MAX_PREDICTION_LENGTH:
        raise ValueError("deterministic minimal prediction exceeds the candidate schema length limit")
    return prediction


def build_standard_predictions(
    references: Iterable[Mapping[str, Any]],
    *,
    candidate: str,
) -> list[dict[str, object]]:
    """Return one schema-v1 prediction row per reference in stable case-ID order."""

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for reference in references:
        case_id = reference.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("reference case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate reference case_id: {case_id}")
        seen.add(case_id)
        rows.append(
            {
                "schema_version": 1,
                "case_id": case_id,
                "prediction_sst": standard_prediction_sst(reference, candidate=candidate),
            }
        )
    if not rows:
        raise ValueError("at least one reference is required")
    return sorted(rows, key=lambda row: str(row["case_id"]))

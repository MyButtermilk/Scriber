"""Deterministic document-grouped dataset splitting and leakage checks."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class Split(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"
    CHALLENGE = "challenge"


@dataclass(frozen=True, slots=True)
class DatasetExample:
    example_id: str
    canonical_document_id: str


def _document_bucket(canonical_document_id: str, seed: int) -> float:
    digest = hashlib.sha256(f"{seed}:{canonical_document_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def assign_document_splits(
    examples: Iterable[DatasetExample],
    *,
    seed: int,
) -> dict[str, Split]:
    """Assign a split from the canonical document id before variants exist."""

    materialized = tuple(examples)
    if any(not item.example_id or not item.canonical_document_id for item in materialized):
        raise ValueError("example_id and canonical_document_id must be non-empty")
    if len({item.example_id for item in materialized}) != len(materialized):
        raise ValueError("example_id values must be unique")

    document_splits: dict[str, Split] = {}
    for document_id in sorted({item.canonical_document_id for item in materialized}):
        bucket = _document_bucket(document_id, seed)
        if bucket < 0.70:
            split = Split.TRAIN
        elif bucket < 0.80:
            split = Split.VALIDATION
        elif bucket < 0.90:
            split = Split.TEST
        else:
            split = Split.CHALLENGE
        document_splits[document_id] = split

    return {
        item.example_id: document_splits[item.canonical_document_id]
        for item in sorted(materialized, key=lambda value: value.example_id)
    }


def validate_no_split_leakage(
    examples: Iterable[DatasetExample],
    assignments: Mapping[str, Split],
) -> tuple[str, ...]:
    """Report canonical documents whose variants cross split boundaries."""

    by_document: dict[str, set[Split]] = defaultdict(set)
    for item in examples:
        try:
            split = Split(assignments[item.example_id])
        except KeyError as exc:
            raise ValueError(f"missing split assignment for {item.example_id}") from exc
        by_document[item.canonical_document_id].add(split)

    return tuple(
        f"{document_id}: {', '.join(sorted(split.value for split in splits))}"
        for document_id, splits in sorted(by_document.items())
        if len(splits) > 1
    )

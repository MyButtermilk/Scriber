"""Deterministic, text-only duplicate and template-family detection."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import Any


@dataclass(frozen=True, slots=True)
class DeduplicationReport:
    exact_groups: tuple[tuple[str, ...], ...]
    normalized_groups: tuple[tuple[str, ...], ...]
    near_duplicate_groups: tuple[tuple[str, ...], ...]
    family_leakage: tuple[tuple[str, ...], ...]


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", text).casefold()).split())


def _ngrams(text: str, size: int = 3) -> frozenset[str]:
    padded = f"  {text}  "
    return frozenset(padded[index : index + size] for index in range(max(1, len(padded) - size + 1)))


def _similar(left: str, right: str) -> float:
    left_grams, right_grams = _ngrams(left), _ngrams(right)
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _groups(values: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
    grouped: dict[str, list[str]] = {}
    for example_id, value in values.items():
        grouped.setdefault(value, []).append(example_id)
    return tuple(sorted(tuple(sorted(ids)) for ids in grouped.values() if len(ids) > 1))


def _connected_groups(ids: tuple[str, ...], pairs: Iterable[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    parents = {example_id: example_id for example_id in ids}

    def find(item: str) -> str:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    for left, right in pairs:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root
    groups: dict[str, list[str]] = {}
    for example_id in ids:
        groups.setdefault(find(example_id), []).append(example_id)
    return tuple(sorted(tuple(sorted(group)) for group in groups.values() if len(group) > 1))


def audit_deduplication(
    records: Iterable[Mapping[str, Any]], *, near_duplicate_threshold: float = 0.82
) -> DeduplicationReport:
    """Report exact, normalized and n-gram-near duplicate families deterministically."""
    if not 0 < near_duplicate_threshold <= 1:
        raise ValueError("near_duplicate_threshold must be in (0, 1]")
    materialized = tuple(records)
    examples: dict[str, tuple[str, str]] = {}
    for record in materialized:
        example_id = _required_text(record, "example_id")
        if example_id in examples:
            raise ValueError("example_id values must be unique")
        examples[example_id] = (_required_text(record, "canonical_document_id"), _required_text(record, "source_text"))

    ids = tuple(sorted(examples))
    texts = {example_id: examples[example_id][1] for example_id in ids}
    normalized = {example_id: _normalized(text) for example_id, text in texts.items()}
    exact_groups = _groups(texts)
    normalized_groups = tuple(group for group in _groups(normalized) if group not in exact_groups)
    near_pairs = (
        (left, right)
        for left, right in combinations(ids, 2)
        if _similar(normalized[left], normalized[right]) >= near_duplicate_threshold
    )
    near_groups = _connected_groups(ids, near_pairs)
    family_leakage = tuple(
        tuple(sorted({examples[example_id][0] for example_id in group}))
        for group in near_groups
        if len({examples[example_id][0] for example_id in group}) > 1
    )
    return DeduplicationReport(exact_groups, normalized_groups, near_groups, tuple(sorted(family_leakage)))

"""Small deterministic train/validation/test packet for the mandatory SFT smoke."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .corruption_engine import CorruptionProfile, corrupt_with_profile

_PROFILES = (
    CorruptionProfile.IDENTITY,
    CorruptionProfile.MINIMAL_PUNCTUATION,
    CorruptionProfile.PUNCTUATION_CASE,
    CorruptionProfile.SPELLING,
    CorruptionProfile.GRAMMAR,
    CorruptionProfile.REPETITION_SELF_CORRECTION,
    CorruptionProfile.ABANDONED_START,
    CorruptionProfile.SPOKEN_FORMATTING,
    CorruptionProfile.FILLER_STUTTER,
    CorruptionProfile.HEAVY_COMBINED,
)
_SPLIT_COUNTS = (("train", 200), ("validation", 50), ("test", 50))


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"smoke source requires non-empty {field}")
    return value


def build_smoke_pairs(
    canonical_records: Iterable[Mapping[str, Any]],
    *,
    seed: int,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Select 300 distinct canonicals and create the exact required smoke splits."""

    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    indexed: dict[str, Mapping[str, Any]] = {}
    for record in canonical_records:
        document_id = _required_text(record, "canonical_document_id")
        _required_text(record, "target_plain_text")
        _required_text(record, "target_sst")
        if document_id in indexed:
            raise ValueError(f"duplicate canonical_document_id: {document_id}")
        indexed[document_id] = record
    if len(indexed) < 300:
        raise ValueError("smoke dataset requires at least 300 canonical records")
    ordered = sorted(
        indexed.values(),
        key=lambda record: hashlib.sha256(f"{seed}:{record['canonical_document_id']}".encode()).hexdigest(),
    )[:300]
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    offset = 0
    for split, count in _SPLIT_COUNTS:
        rows: list[dict[str, Any]] = []
        for split_index, record in enumerate(ordered[offset : offset + count]):
            profile = _PROFILES[(offset + split_index) % len(_PROFILES)]
            corruption = corrupt_with_profile(
                record["target_plain_text"],
                profile,
                seed=seed + offset + split_index,
            )
            rows.append(
                {
                    "schema_version": 1,
                    "example_id": f"smoke_{split}_{split_index:04d}",
                    "canonical_document_id": record["canonical_document_id"],
                    "split": split,
                    "corruption_profile": profile.value,
                    "severity": corruption.severity.value,
                    "error_origin": corruption.error_origin.value,
                    "operations": [operation.kind for operation in corruption.operations],
                    "source_text": corruption.text,
                    "target_sst": record["target_sst"],
                }
            )
        result[split] = tuple(rows)
        offset += count
    return result

"""Deterministic training-set compaction that preserves every coverage floor."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .coverage_audit import MANDATORY_QUOTAS, quota_evidence

EvidenceFunction = Callable[[Mapping[str, Any]], frozenset[str]]


@dataclass(frozen=True, slots=True)
class QuotaSelection:
    """One exact-size, quota-preserving selection and its aggregate evidence."""

    records: tuple[Mapping[str, Any], ...]
    source_count: int
    selected_count: int
    mandatory_union_count: int
    quota_counts: dict[str, int]
    seed: int


def _rank(seed: int, example_id: str) -> tuple[bytes, str]:
    digest = hashlib.sha256(f"quota-sampler-v1:{seed}:{example_id}".encode()).digest()
    return digest, example_id


def select_quota_preserving_records(
    records: Iterable[Mapping[str, Any]],
    *,
    target_count: int,
    seed: int,
    quota_minima: Mapping[str, int] | None = None,
    evidence_fn: EvidenceFunction = quota_evidence,
) -> QuotaSelection:
    """Keep an exact deterministic subset while satisfying every declared quota.

    For each quota, the sampler first retains the lowest hash-ranked eligible
    rows up to the required floor. It then fills the remaining capacity by the
    same global rank. The union is therefore stable under input reordering and
    never relies on semantic guesses.
    """

    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count <= 0:
        raise ValueError("target_count must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    minima_source = dict(MANDATORY_QUOTAS) if quota_minima is None else dict(quota_minima)
    if not minima_source:
        raise ValueError("quota_minima must not be empty")
    for quota_id, minimum in minima_source.items():
        if (
            not isinstance(quota_id, str)
            or not quota_id
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 0
        ):
            raise ValueError("quota_minima must map non-empty strings to non-negative integers")

    by_id: dict[str, Mapping[str, Any]] = {}
    evidence_by_id: dict[str, frozenset[str]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("quota sampler records must be mappings")
        example_id = record.get("example_id")
        if not isinstance(example_id, str) or not example_id:
            raise ValueError("quota sampler record requires a non-empty example_id")
        if example_id in by_id:
            raise ValueError(f"duplicate example_id: {example_id}")
        evidence = evidence_fn(record)
        if not isinstance(evidence, frozenset) or not all(
            isinstance(quota_id, str) and quota_id for quota_id in evidence
        ):
            raise ValueError("evidence_fn must return a frozenset of non-empty quota IDs")
        by_id[example_id] = record
        evidence_by_id[example_id] = evidence

    if target_count > len(by_id):
        raise ValueError(
            f"target_count {target_count} exceeds source record count {len(by_id)}"
        )
    ordered_ids = sorted(by_id, key=lambda example_id: _rank(seed, example_id))
    eligible_by_quota = {
        quota_id: [
            example_id
            for example_id in ordered_ids
            if quota_id in evidence_by_id[example_id]
        ]
        for quota_id in minima_source
    }
    mandatory_ids: set[str] = set()
    for quota_id, minimum in sorted(minima_source.items()):
        eligible = eligible_by_quota[quota_id]
        if len(eligible) < minimum:
            raise ValueError(
                f"insufficient evidence for {quota_id}: required {minimum}, found {len(eligible)}"
            )
        mandatory_ids.update(eligible[:minimum])
    if len(mandatory_ids) > target_count:
        raise ValueError(
            "target_count cannot retain the mandatory quota union: "
            f"target={target_count}, mandatory_union={len(mandatory_ids)}"
        )

    selected_ids = set(mandatory_ids)
    for example_id in ordered_ids:
        if len(selected_ids) == target_count:
            break
        selected_ids.add(example_id)
    selected_order = tuple(example_id for example_id in ordered_ids if example_id in selected_ids)
    quota_counts = {
        quota_id: sum(
            quota_id in evidence_by_id[example_id] for example_id in selected_order
        )
        for quota_id in sorted(minima_source)
    }
    if any(quota_counts[quota_id] < minimum for quota_id, minimum in minima_source.items()):
        raise RuntimeError("quota sampler internal coverage invariant failed")
    return QuotaSelection(
        records=tuple(by_id[example_id] for example_id in selected_order),
        source_count=len(by_id),
        selected_count=len(selected_order),
        mandatory_union_count=len(mandatory_ids),
        quota_counts=quota_counts,
        seed=seed,
    )

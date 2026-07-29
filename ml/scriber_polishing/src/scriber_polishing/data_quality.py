"""Fail-closed, reproducible quality reports for generated polishing records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .deduplicate import audit_deduplication
from .split_dataset import DatasetExample, Split, validate_no_split_leakage
from .sst_parser import SSTParseError, parse_sst


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    accepted_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    rejection_reasons: dict[str, tuple[str, ...]]
    duplicate_rate: float


def assess_data_quality(records: Iterable[Mapping[str, Any]], assignments: Mapping[str, Split]) -> DataQualityReport:
    """Evaluate hard format, duplicate and split gates without accepting ambiguity."""
    materialized = tuple(records)
    audit = audit_deduplication(materialized)
    ids = tuple(sorted(str(record.get("example_id", "")) for record in materialized))
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("example_id values must be unique and non-empty")
    examples = tuple(
        DatasetExample(str(record["example_id"]), str(record["canonical_document_id"])) for record in materialized
    )
    leakage_documents = {issue.split(":", 1)[0] for issue in validate_no_split_leakage(examples, assignments)}
    duplicate_ids = {example_id for group in audit.near_duplicate_groups for example_id in group}
    reasons: dict[str, tuple[str, ...]] = {}
    for record in materialized:
        example_id = str(record["example_id"])
        item_reasons: list[str] = []
        if example_id in duplicate_ids:
            item_reasons.append("duplicate_family")
        if str(record["canonical_document_id"]) in leakage_documents:
            item_reasons.append("split_leakage")
        if not isinstance(record.get("semantic_plan"), Mapping) or not record["semantic_plan"].get("id"):
            item_reasons.append("invalid_semantic_plan")
        try:
            parse_sst(str(record.get("target_sst", "")))
        except (SSTParseError, ValueError):
            item_reasons.append("invalid_sst")
        if item_reasons:
            reasons[example_id] = tuple(item_reasons)
    rejected_ids = tuple(sorted(reasons))
    accepted_ids = tuple(example_id for example_id in ids if example_id not in reasons)
    return DataQualityReport(
        accepted_ids, rejected_ids, reasons, len(duplicate_ids) / len(materialized) if materialized else 0.0
    )

"""AI-Gold selection: all required evidence must be present and agree."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .critic_aggregation import CriticVerdict, ExampleRisk, ReleaseDecision, aggregate_critic_verdicts
from .deduplicate import audit_deduplication
from .protected_spans import protect_spans
from .split_dataset import DatasetExample, Split, validate_no_split_leakage
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst


@dataclass(frozen=True, slots=True)
class GoldBuildResult:
    accepted: tuple[Mapping[str, Any], ...]
    rejected: dict[str, tuple[str, ...]]


def _protected_values_survive(source: str, target_sst: str) -> bool:
    return all(target_sst.count(span.value) == 1 for span in protect_spans(source).spans)


def build_gold_examples(candidates: Iterable[Mapping[str, Any]], assignments: Mapping[str, Split]) -> GoldBuildResult:
    """Return only AI-Gold candidates with complete independent and deterministic proof."""
    materialized = tuple(candidates)
    audit = audit_deduplication(materialized)
    examples = tuple(
        DatasetExample(str(item.get("example_id", "")), str(item.get("canonical_document_id", "")))
        for item in materialized
    )
    leakage_documents = {issue.split(":", 1)[0] for issue in validate_no_split_leakage(examples, assignments)}
    duplicate_ids = {example_id for group in audit.near_duplicate_groups for example_id in group}
    accepted: list[Mapping[str, Any]] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for candidate in sorted(materialized, key=lambda item: str(item.get("example_id", ""))):
        example_id = str(candidate.get("example_id", ""))
        reasons: list[str] = []
        if str(candidate.get("canonical_document_id", "")) in leakage_documents:
            reasons.append("split_leakage")
        elif example_id in duplicate_ids:
            reasons.append("duplicate_family")
        semantic_plan = candidate.get("semantic_plan")
        if (
            not isinstance(semantic_plan, Mapping)
            or not isinstance(semantic_plan.get("id"), str)
            or not semantic_plan["id"]
        ):
            reasons.append("invalid_semantic_plan")
        if not isinstance(candidate.get("target_ast"), Mapping):
            reasons.append("invalid_document_ast")
        target_sst = candidate.get("target_sst")
        try:
            document = parse_sst(str(target_sst))
            if render_sst(document) != target_sst:
                reasons.append("sst_roundtrip_failed")
        except (SSTParseError, ValueError):
            reasons.append("invalid_sst")
        source = candidate.get("source_text")
        if not isinstance(source, str) or not _protected_values_survive(source, str(target_sst)):
            reasons.append("protected_span_failed")
        if not candidate.get("deterministic_valid") or not candidate.get("fact_roundtrip_valid"):
            reasons.append("deterministic_gate_failed")
        verdicts = candidate.get("verdicts")
        if not isinstance(verdicts, tuple) or not all(isinstance(item, CriticVerdict) for item in verdicts):
            reasons.append("critic_not_accepted")
        else:
            try:
                decision = aggregate_critic_verdicts(
                    generator_agent_id=str(candidate.get("generator_agent_id", "")),
                    risk=ExampleRisk(candidate.get("risk", ExampleRisk.AI_GOLD)),
                    deterministic_valid=bool(candidate.get("deterministic_valid")),
                    verdicts=verdicts,
                )
            except ValueError:
                decision = ReleaseDecision.REJECT
            if decision is not ReleaseDecision.ACCEPT:
                reasons.append("critic_not_accepted")
        if reasons:
            rejected[example_id] = tuple(dict.fromkeys(reasons))
        else:
            accepted.append(candidate)
    return GoldBuildResult(tuple(accepted), dict(sorted(rejected.items())))

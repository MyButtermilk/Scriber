"""Fail-closed aggregation of schema-valid, independent blinded critic reviews."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .schemas import validate_critic_verdict


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ReviewAggregationResult:
    decisions: dict[str, ReviewDecision]
    rejection_reasons: dict[str, tuple[str, ...]]


def aggregate_case_reviews(
    case_ids: Iterable[str],
    review_sets: Iterable[Iterable[Mapping[str, Any]]],
    *,
    require_adversarial: bool,
) -> ReviewAggregationResult:
    """Require complete role evidence and reject any disagreement or critical flag."""

    ordered_case_ids = tuple(sorted(case_ids))
    if not ordered_case_ids or any(not case_id for case_id in ordered_case_ids):
        raise ValueError("case_ids must be non-empty strings")
    if len(ordered_case_ids) != len(set(ordered_case_ids)):
        raise ValueError("case_ids must be unique")
    known_cases = set(ordered_case_ids)
    by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in ordered_case_ids}
    seen: set[tuple[str, str]] = set()
    critic_roles: dict[str, str] = {}
    for review_set in review_sets:
        for raw_verdict in review_set:
            verdict = validate_critic_verdict(raw_verdict)
            case_id = verdict["case_id"]
            if case_id not in known_cases:
                raise ValueError(f"verdict references unknown case: {case_id}")
            identity = (verdict["critic_id"], case_id)
            if identity in seen:
                raise ValueError(f"duplicate verdict for critic and case: {identity[0]} {case_id}")
            seen.add(identity)
            previous_role = critic_roles.setdefault(verdict["critic_id"], verdict["critic_role"])
            if previous_role != verdict["critic_role"]:
                raise ValueError(f"critic changed role: {verdict['critic_id']}")
            by_case[case_id].append(verdict)

    decisions: dict[str, ReviewDecision] = {}
    rejection_reasons: dict[str, tuple[str, ...]] = {}
    for case_id in ordered_case_ids:
        verdicts = by_case[case_id]
        roles = {
            role: [item for item in verdicts if item["critic_role"] == role]
            for role in ("fact", "style", "adversarial")
        }
        reasons: list[str] = []
        if not roles["fact"]:
            reasons.append("missing_fact_critic")
        if not roles["style"]:
            reasons.append("missing_style_critic")
        if require_adversarial and not roles["adversarial"]:
            reasons.append("missing_adversarial_critic")
        for verdict in verdicts:
            reasons.extend(f"critical_flag:{flag}" for flag in verdict["critical_flags"])
            if not verdict["acceptable"]:
                reasons.append(verdict["summary_code"])
        unique_reasons = tuple(dict.fromkeys(reasons))
        if unique_reasons:
            decisions[case_id] = ReviewDecision.REJECT
            rejection_reasons[case_id] = unique_reasons
        else:
            decisions[case_id] = ReviewDecision.ACCEPT
    return ReviewAggregationResult(decisions, rejection_reasons)

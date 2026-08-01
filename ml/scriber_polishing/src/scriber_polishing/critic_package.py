"""Build deterministic clean-room packages for independent AI critics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

_PLAN_FIELDS = (
    "domain",
    "document_type",
    "language",
    "address_mode",
    "risk_tags",
    "semantic_plan",
)
_TARGET_FIELDS = (
    "target_ast",
    "target_sst",
    "target_plain_text",
    "target_markdown",
    "target_html",
    "protected_spans",
)


def _required_text(record: Mapping[str, Any], field: str, label: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires non-empty {field}")
    return value


def _indexed(records: Iterable[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for record in records:
        seed_id = _required_text(record, "seed_id", label)
        if seed_id in indexed:
            raise ValueError(f"duplicate {label} seed_id: {seed_id}")
        indexed[seed_id] = record
    return indexed


def _case_id(seed_id: str, plan: Mapping[str, Any], target: Mapping[str, Any], blind_seed: int) -> str:
    evidence = json.dumps(
        {
            "blind_seed": blind_seed,
            "source_identity": seed_id,
            "semantic_plan": plan["semantic_plan"],
            "target_ast": target["target_ast"],
            "target_sst": target["target_sst"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"case_{hashlib.sha256(evidence).hexdigest()[:24]}"


def build_blinded_cases(
    plans: Iterable[Mapping[str, Any]],
    targets: Iterable[Mapping[str, Any]],
    *,
    blind_seed: int,
) -> tuple[dict[str, Any], ...]:
    """Pair plans and targets while removing all agent, seed, split, and candidate identity."""

    if not isinstance(blind_seed, int):
        raise TypeError("blind_seed must be an integer")
    plans_by_id = _indexed(plans, "plan")
    targets_by_id = _indexed(targets, "target")
    missing_targets = sorted(set(plans_by_id).difference(targets_by_id))
    if missing_targets:
        raise ValueError(f"missing target for plan: {missing_targets[0]}")
    missing_plans = sorted(set(targets_by_id).difference(plans_by_id))
    if missing_plans:
        raise ValueError(f"target without a plan: {missing_plans[0]}")

    cases: list[dict[str, Any]] = []
    for seed_id, plan in plans_by_id.items():
        target = targets_by_id[seed_id]
        for field in _PLAN_FIELDS:
            if field not in plan:
                raise ValueError(f"plan requires {field}")
        for field in _TARGET_FIELDS:
            if field not in target:
                raise ValueError(f"target requires {field}")
        case = {
            "package_schema_version": 1,
            "case_id": _case_id(seed_id, plan, target, blind_seed),
            **{field: deepcopy(plan[field]) for field in _PLAN_FIELDS},
            **{field: deepcopy(target[field]) for field in _TARGET_FIELDS},
        }
        cases.append(case)

    def blinded_order(case: Mapping[str, Any]) -> str:
        return hashlib.sha256(f"{blind_seed}:{case['case_id']}".encode()).hexdigest()

    ordered = tuple(sorted(cases, key=blinded_order))
    case_ids = [case["case_id"] for case in ordered]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("blinded case IDs must be unique")
    return ordered


def build_blinded_case_index(
    plans: Iterable[Mapping[str, Any]],
    targets: Iterable[Mapping[str, Any]],
    *,
    blind_seed: int,
) -> dict[str, str]:
    """Return the private case-to-seed join without placing it in a critic package."""

    if not isinstance(blind_seed, int):
        raise TypeError("blind_seed must be an integer")
    plans_by_id = _indexed(plans, "plan")
    targets_by_id = _indexed(targets, "target")
    missing_targets = sorted(set(plans_by_id).difference(targets_by_id))
    if missing_targets:
        raise ValueError(f"missing target for plan: {missing_targets[0]}")
    missing_plans = sorted(set(targets_by_id).difference(plans_by_id))
    if missing_plans:
        raise ValueError(f"target without a plan: {missing_plans[0]}")
    result = {
        _case_id(seed_id, plan, targets_by_id[seed_id], blind_seed): seed_id for seed_id, plan in plans_by_id.items()
    }
    if len(result) != len(plans_by_id):
        raise ValueError("blinded case IDs must be unique")
    return dict(sorted(result.items()))

"""Load and validate versioned, repository-owned polishing contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REQUIRED_RULE_FIELDS = frozenset(
    {
        "id",
        "description",
        "criticality",
        "positive_examples",
        "counterexamples",
        "automatic_checks",
        "ai_judge_criterion",
        "error_code",
    }
)
VALID_CRITICALITIES = frozenset({"critical", "high", "medium", "low"})
_CONTRACTS_ROOT = Path(__file__).parents[2] / "contracts"


def load_behavioral_contract(path: str | Path) -> dict[str, Any]:
    """Load a behavioural contract and reject malformed YAML documents."""
    with Path(path).open(encoding="utf-8") as file_obj:
        contract = yaml.safe_load(file_obj)
    if not isinstance(contract, dict):
        raise ValueError("behavioral contract must be a mapping")
    validate_behavioral_contract(contract)
    return contract


def validate_behavioral_contract(contract: Mapping[str, Any]) -> None:
    """Validate the public behavioural-contract record shape."""
    if not isinstance(contract.get("version"), str) or not contract["version"].strip():
        raise ValueError("behavioral contract requires a non-empty version")
    rules = contract.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("behavioral contract requires a non-empty rules list")

    rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise ValueError(f"behavioral contract rule {index} must be a mapping")
        missing = REQUIRED_RULE_FIELDS.difference(rule)
        if missing:
            raise ValueError(f"behavioral contract rule {index} is missing {sorted(missing)[0]}")
        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"behavioral contract rule {index} has an invalid id")
        if rule_id in rule_ids:
            raise ValueError(f"behavioral contract contains duplicate id: {rule_id}")
        rule_ids.add(rule_id)
        if rule["criticality"] not in VALID_CRITICALITIES:
            raise ValueError(f"behavioral contract rule {rule_id} has invalid criticality")
        for field in ("description", "ai_judge_criterion", "error_code"):
            if not isinstance(rule[field], str) or not rule[field].strip():
                raise ValueError(f"behavioral contract rule {rule_id} has an invalid {field}")
        for field in ("positive_examples", "counterexamples", "automatic_checks"):
            value = rule[field]
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"behavioral contract rule {rule_id} has invalid {field}")


def validate_seed_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a semantic seed plan only when it passes the closed v1 schema."""

    schema = json.loads((_CONTRACTS_ROOT / "seed_plan_schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(plan)), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"seed plan failed schema: {errors[0].message}")
    return dict(plan)

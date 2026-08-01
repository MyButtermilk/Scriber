"""Load and validate versioned, repository-owned polishing contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
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


@cache
def _contract_validator(file_name: str) -> Draft202012Validator:
    schema = json.loads((_CONTRACTS_ROOT / file_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


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

    errors = sorted(
        _contract_validator("seed_plan_schema.json").iter_errors(dict(plan)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"seed plan failed schema: {errors[0].message}")
    return dict(plan)


def validate_critic_verdict(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Return a blinded critic verdict only when it satisfies the closed v1 schema."""

    errors = sorted(
        _contract_validator("critic_verdict_schema.json").iter_errors(dict(verdict)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"critic verdict failed schema: {errors[0].message}")
    return dict(verdict)


def validate_judge_verdict(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """Return a blinded pairwise judge verdict only when its evidence is complete."""

    errors = sorted(
        _contract_validator("judge_verdict_schema.json").iter_errors(dict(verdict)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"judge verdict failed schema: {errors[0].message}")
    return dict(verdict)


def validate_judge_stage_output_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a judge-stage output binding only when every identity field is closed."""

    errors = sorted(
        _contract_validator("judge_stage_output_manifest_schema.json").iter_errors(dict(manifest)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"judge stage output manifest failed schema: {errors[0].message}")
    return dict(manifest)


def validate_judge_campaign_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a dual-comparison release report only when its shape is closed."""

    errors = sorted(
        _contract_validator("judge_campaign_report_schema.json").iter_errors(dict(report)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"judge campaign report failed schema: {errors[0].message}")
    return dict(report)


def validate_sol_followup_selection_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a diagnostic Sol-selection manifest only when its closed shape is valid."""

    errors = sorted(
        _contract_validator("sol_followup_selection_manifest_schema.json").iter_errors(dict(manifest)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"Sol follow-up selection manifest failed schema: {errors[0].message}")
    return dict(manifest)


def validate_sol_followup_diagnostic_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return the closed aggregate-only diagnostic report for Sol follow-up selection."""

    errors = sorted(
        _contract_validator("sol_followup_diagnostic_report_schema.json").iter_errors(dict(report)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"Sol follow-up diagnostic report failed schema: {errors[0].message}")
    return dict(report)


def validate_sol_followup_artifact_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a closed hash-binding manifest for diagnostic Sol follow-up artifacts."""

    errors = sorted(
        _contract_validator("sol_followup_artifact_manifest_schema.json").iter_errors(dict(manifest)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"Sol follow-up artifact manifest failed schema: {errors[0].message}")
    return dict(manifest)


def validate_semantic_plan(semantic_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the reusable semantic-plan portion of an AI seed record."""

    plan_schema = _contract_validator("seed_plan_schema.json").schema["properties"]["semantic_plan"]
    errors = sorted(
        Draft202012Validator(plan_schema).iter_errors(dict(semantic_plan)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"semantic plan failed schema: {errors[0].message}")
    return dict(semantic_plan)


def validate_ai_gold_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the aggregate-only AI-Verified Gold Set manifest."""

    materialized = dict(manifest)
    errors = sorted(
        _contract_validator("ai_gold_manifest_schema.json").iter_errors(materialized),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"AI-Gold manifest failed schema: {errors[0].message}")
    split_counts = materialized["split_counts"]
    if split_counts != materialized["split_targets"]:
        raise ValueError("AI-Gold manifest split counts differ from frozen targets")
    if any(materialized["splits"][split]["count"] != count for split, count in split_counts.items()):
        raise ValueError("AI-Gold manifest split counts differ from split evidence")
    if materialized["total_count"] != sum(split_counts.values()):
        raise ValueError("AI-Gold manifest total differs from split counts")
    if materialized["eligible_count"] != (materialized["total_count"] + materialized["eligible_unselected_count"]):
        raise ValueError("AI-Gold manifest eligible count is inconsistent")
    if materialized["rejected_count"] != sum(materialized["rejection_reason_counts"].values()):
        raise ValueError("AI-Gold manifest rejected count is inconsistent")
    if materialized["candidate_count"] != (materialized["eligible_count"] + materialized["rejected_count"]):
        raise ValueError("AI-Gold manifest candidate count is inconsistent")
    if materialized["critic_package_count"] != len(materialized["critic_packages_sha256"]):
        raise ValueError("AI-Gold manifest critic package count is inconsistent")
    return materialized


def validate_training_pair(pair: Mapping[str, Any]) -> dict[str, Any]:
    """Return a synthetic training pair only when it passes the closed v1 schema."""

    errors = sorted(
        _contract_validator("training_pair_schema.json").iter_errors(dict(pair)),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"training pair failed schema: {errors[0].message}")
    return dict(pair)

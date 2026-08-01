"""Strict config-bound release gates for deterministic evaluation reports."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .paired_significance import validate_paired_significance_report

_PROJECT_ROOT = Path(__file__).parents[2]
_CONFIG_SCHEMA_PATH = _PROJECT_ROOT / "contracts" / "evaluation_config_schema.json"
_CONTRACTS_ROOT = _PROJECT_ROOT / "contracts"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PRODUCT_STATUS_VALUES = frozenset(
    {
        "AI_DATA_FACTORY_IN_PROGRESS",
        "READY_FOR_TRAINING",
        "TRAINING_IN_PROGRESS",
        "READY_FOR_AI_JUDGING",
        "RELEASED",
        "PRIVATE_CANDIDATE",
        "BLOCKED_BY_INCLUDED_CODEX_LIMIT",
        "BLOCKED_BY_EXTERNAL_PREREQUISITE",
        "FAILED",
    }
)


@cache
def _config_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_CONFIG_SCHEMA_PATH.read_text(encoding="utf-8")))


@cache
def _contract_validator(file_name: str) -> Draft202012Validator:
    path = _CONTRACTS_ROOT / file_name
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _schema_reasons(
    report: Mapping[str, Any],
    *,
    schema_file: str,
    label: str,
) -> list[str]:
    errors = sorted(
        _contract_validator(schema_file).iter_errors(dict(report)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if not errors:
        return []
    location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
    return [f"{label}_schema_failed_at_{location}:{errors[0].message}"]


def _evidence_sha256_reason(value: object) -> str | None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return "missing_or_invalid_evidence_sha256"
    return None


def validate_evaluation_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed release policy and reject ambiguous check identities."""

    materialized = dict(config)
    errors = sorted(_config_validator().iter_errors(materialized), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"evaluation config failed schema: {errors[0].message}")
    checks = [
        *materialized["automatic_evaluation"]["hard_gates"],
        *materialized["automatic_evaluation"]["minimums"],
    ]
    check_ids = [check["id"] for check in checks]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("evaluation config check IDs must be unique")
    paired_policy = materialized.get("paired_significance")
    if paired_policy is not None:
        candidates = set(materialized["candidates"])
        if paired_policy["baseline_candidate"] not in candidates:
            raise ValueError("paired significance baseline candidate must be declared")
        if not set(paired_policy["required_for_release_candidates"]).issubset(candidates):
            raise ValueError("paired significance release candidates must be declared")
        for field in ("confidence_level", "minimum_delta"):
            if not math.isfinite(float(paired_policy[field])):
                raise ValueError(f"paired significance {field} must be finite")
    return materialized


def load_evaluation_config(path: str | Path) -> dict[str, Any]:
    """Load one repository-owned YAML gate configuration."""

    with Path(path).open(encoding="utf-8") as file_obj:
        config = yaml.safe_load(file_obj)
    if not isinstance(config, Mapping):
        raise ValueError("evaluation config must be a mapping")
    return validate_evaluation_config(config)


def _metric_value(slice_report: Mapping[str, Any], metric_path: str) -> int | float:
    current: Any = slice_report
    parts = metric_path.split(".")
    for index, part in enumerate(parts):
        if not isinstance(current, Mapping):
            raise KeyError(metric_path)
        if part not in current:
            if index == len(parts) - 1 and parts[:-1] == ["critical_errors"]:
                return 0
            raise KeyError(metric_path)
        current = current[part]
    if isinstance(current, bool) or not isinstance(current, int | float):
        raise TypeError(f"gate metric must resolve to a number: {metric_path}")
    return current


def _check(
    metrics: Mapping[str, Any],
    definition: Mapping[str, Any],
    *,
    group: str,
) -> dict[str, Any]:
    slice_name = definition["slice"]
    slice_reports = metrics.get("slice_reports")
    slice_report = slice_reports.get(slice_name) if isinstance(slice_reports, Mapping) else None
    actual: int | float | None = None
    observed_cases = 0
    reason: str | None = None
    if not isinstance(slice_report, Mapping):
        reason = "slice_not_evaluated"
    else:
        total = slice_report.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            reason = "invalid_slice_case_count"
        else:
            observed_cases = total
        if reason is None and observed_cases < definition["minimum_cases"]:
            reason = "insufficient_slice_cases"
        if reason is None:
            try:
                actual = _metric_value(slice_report, definition["metric"])
            except KeyError:
                reason = "metric_not_evaluated"
            except TypeError:
                reason = "metric_not_numeric"

    threshold = definition["threshold"]
    operator = definition["operator"]
    passed = False
    if reason is None and actual is not None:
        passed = {
            "gte": actual >= threshold,
            "lte": actual <= threshold,
            "eq": actual == threshold,
        }[operator]
        if not passed:
            reason = "threshold_not_met"
    return {
        "id": definition["id"],
        "group": group,
        "slice": slice_name,
        "metric": definition["metric"],
        "operator": operator,
        "threshold": threshold,
        "minimum_cases": definition["minimum_cases"],
        "observed_cases": observed_cases,
        "actual": actual,
        "passed": passed,
        "reason": reason,
    }


def evaluate_candidate_gate(
    metrics: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    candidate: str,
) -> dict[str, Any]:
    """Apply every configured automatic check to one named candidate."""

    validated = validate_evaluation_config(config)
    if candidate not in validated["candidates"]:
        raise ValueError(f"candidate is not declared in evaluation config: {candidate}")
    hard = {
        definition["id"]: _check(metrics, definition, group="hard_gate")
        for definition in validated["automatic_evaluation"]["hard_gates"]
    }
    minimums = {
        definition["id"]: _check(metrics, definition, group="minimum")
        for definition in validated["automatic_evaluation"]["minimums"]
    }
    ordered = [*hard.values(), *minimums.values()]
    failed = [check for check in ordered if not check["passed"]]
    return {
        "schema_version": 1,
        "candidate": candidate,
        "status": "passed" if not failed else "failed",
        "passed": not failed,
        "hard_gates": hard,
        "minimums": minimums,
        "failed_checks": failed,
    }


def _required_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _required_number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be numeric")
    return value


def _completed_training_component(
    report: Mapping[str, Any] | None,
    *,
    report_sha256: str | None,
) -> dict[str, Any]:
    if report is None:
        return {
            "status": "missing",
            "passed": False,
            "reasons": ["missing_completed_final_training_report"],
        }
    reasons = _schema_reasons(
        report,
        schema_file="completed_final_training_report_schema.json",
        label="completed_final_training_report",
    )
    hash_reason = _evidence_sha256_reason(report_sha256)
    if hash_reason is not None:
        reasons.append(hash_reason)
    if not reasons:
        if report.get("run_kind") != "final":
            reasons.append("training_report_is_not_final_run")
        if report.get("training_completed") is not True:
            reasons.append("training_not_completed")
    return {
        "status": "complete" if not reasons else "invalid",
        "passed": not reasons,
        "reasons": reasons,
        **({"report_sha256": report_sha256} if report_sha256 is not None else {}),
        **(
            {"run_fingerprint": report.get("run_fingerprint")}
            if isinstance(report.get("run_fingerprint"), str)
            else {}
        ),
    }


def _private_hub_component(
    report: Mapping[str, Any] | None,
    *,
    report_sha256: str | None,
    completed_training_report: Mapping[str, Any] | None,
    completed_training_report_sha256: str | None,
) -> dict[str, Any]:
    if report is None:
        return {
            "status": "missing",
            "passed": False,
            "reasons": ["missing_private_hub_verification_report"],
        }
    reasons = _schema_reasons(
        report,
        schema_file="hub_verification_schema.json",
        label="private_hub_verification_report",
    )
    hash_reason = _evidence_sha256_reason(report_sha256)
    if hash_reason is not None:
        reasons.append(hash_reason)
    expected_training_hash = (
        f"sha256:{completed_training_report_sha256}"
        if _evidence_sha256_reason(completed_training_report_sha256) is None
        else None
    )
    if report.get("completed_training_report_sha256") != expected_training_hash:
        reasons.append("completed_training_report_hash_mismatch")
    training_run_fingerprint = (
        completed_training_report.get("run_fingerprint")
        if isinstance(completed_training_report, Mapping)
        else None
    )
    if report.get("training_run_fingerprint") != training_run_fingerprint:
        reasons.append("training_run_fingerprint_mismatch")
    final_model = (
        completed_training_report.get("final_model")
        if isinstance(completed_training_report, Mapping)
        else None
    )
    inventory = final_model.get("inventory") if isinstance(final_model, Mapping) else None
    final_model_tree_sha256 = (
        inventory.get("tree_sha256") if isinstance(inventory, Mapping) else None
    )
    if report.get("bf16_final_model_tree_sha256") != final_model_tree_sha256:
        reasons.append("bf16_final_model_tree_hash_mismatch")
    artifacts = report.get("artifacts")
    revisions: dict[str, str] = {}
    if isinstance(artifacts, Mapping):
        expected_repositories = {
            "model": "Buttermilk03/scriber-gemma3-270m-polishing-de-v1",
            "dataset": "Buttermilk03/scriber-transcript-polishing-de-v1",
        }
        for kind, expected_repository in expected_repositories.items():
            artifact = artifacts.get(kind)
            if isinstance(artifact, Mapping):
                if (
                    artifact.get("repo_id") != expected_repository
                    or artifact.get("repo_type") != kind
                ):
                    reasons.append(f"{kind}_repository_identity_mismatch")
                if isinstance(artifact.get("revision"), str):
                    revisions[kind] = artifact["revision"]
    return {
        "status": "verified_private" if not reasons else "invalid",
        "passed": not reasons,
        "reasons": reasons,
        **({"report_sha256": report_sha256} if report_sha256 is not None else {}),
        "completed_training_report_sha256": report.get(
            "completed_training_report_sha256"
        ),
        "training_run_fingerprint": report.get("training_run_fingerprint"),
        "bf16_final_model_tree_sha256": report.get(
            "bf16_final_model_tree_sha256"
        ),
        "revisions": revisions,
    }


def _variant_release_component(report: Mapping[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {
            "status": "missing",
            "passed": False,
            "reasons": ["missing_variant_release_matrix"],
        }
    materialized = dict(report)
    if not isinstance(materialized.get("passed"), bool):
        return {
            "status": "invalid",
            "passed": False,
            "reasons": ["variant_release_component_has_no_boolean_passed_state"],
        }
    reasons = materialized.get("reasons")
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        return {
            "status": "invalid",
            "passed": False,
            "reasons": ["variant_release_component_has_invalid_reasons"],
        }
    return materialized


def _judge_component(
    report: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    split: str,
    candidate: str,
    config_sha256: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    if report.get("evaluation_config_sha256") != config_sha256:
        reasons.append("config_hash_mismatch")
    if report.get("evaluation_split") != split:
        reasons.append("split_mismatch")
    if report.get("release_candidate") != candidate:
        reasons.append("candidate_mismatch")
    status = report.get("status")
    if status not in {"complete", "pending_tiebreak"}:
        reasons.append("invalid_status")
    if status == "pending_tiebreak":
        if report.get("release_gate_passed") is not False:
            reasons.append("pending_gate_must_not_pass")
        return {
            "status": status,
            "passed": False,
            "checks": {},
            "reasons": reasons,
        }

    summaries = _required_mapping(report.get("candidate_summaries"), "candidate_summaries")
    summary = _required_mapping(summaries.get(candidate), f"candidate summary {candidate}")
    acceptable_rate = _required_number(summary.get("acceptable_rate"), "acceptable_rate")
    deterministic_count = _required_number(
        summary.get("deterministic_critical_case_count"),
        "deterministic_critical_case_count",
    )
    judge_count = _required_number(summary.get("judge_critical_case_count"), "judge_critical_case_count")
    rubric = _required_mapping(summary.get("rubric_averages"), "rubric_averages")
    judge_policy = config["judge_aggregation"]
    checks: dict[str, dict[str, Any]] = {}

    def check(check_id: str, actual: int | float, operator: str, threshold: int | float) -> None:
        passed = actual >= threshold if operator == "gte" else actual <= threshold
        checks[check_id] = {
            "actual": actual,
            "operator": operator,
            "threshold": threshold,
            "passed": passed,
        }

    check(
        "acceptable_rate",
        acceptable_rate,
        "gte",
        judge_policy["acceptable_rate_minimums"][split],
    )
    check("deterministic_critical_case_count", deterministic_count, "lte", 0)
    check("judge_critical_case_count", judge_count, "lte", 0)
    for dimension, threshold in judge_policy["rubric_minimums"].items():
        check(
            f"rubric:{dimension}",
            _required_number(rubric.get(dimension), f"rubric average {dimension}"),
            "gte",
            threshold,
        )
    recomputed_passed = status == "complete" and all(item["passed"] for item in checks.values())
    if report.get("release_gate_passed") is not recomputed_passed:
        reasons.append("declared_gate_differs_from_recomputed_gate")
    return {
        "status": status,
        "passed": recomputed_passed and not reasons,
        "checks": checks,
        "reasons": reasons,
    }


def _paired_significance_component(
    report: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
    *,
    candidate: str,
    config_sha256: str,
    reference_sha256: object,
    evaluation_report_sha256: str | None,
) -> dict[str, Any]:
    if report is None:
        return {"status": "missing", "passed": False, "reasons": ["missing_measured_evidence"]}
    try:
        validated = validate_paired_significance_report(report)
    except ValueError as error:
        return {"status": "invalid", "passed": False, "reasons": [str(error)]}

    reasons: list[str] = []
    inputs = validated["inputs"]
    metric = validated["metric"]
    bootstrap = validated["bootstrap"]
    paired_delta = validated["paired_delta"]
    if validated["candidates"]["base"] != policy["baseline_candidate"]:
        reasons.append("baseline_candidate_mismatch")
    if validated["candidates"]["fine_tuned"] != candidate:
        reasons.append("candidate_mismatch")
    if inputs["evaluation_config_sha256"] != config_sha256:
        reasons.append("config_hash_mismatch")
    if inputs["reference_sha256"] != reference_sha256:
        reasons.append("reference_hash_mismatch")
    if evaluation_report_sha256 is None:
        reasons.append("evaluation_report_hash_unavailable")
    elif inputs["fine_tuned_report_sha256"] != evaluation_report_sha256:
        reasons.append("fine_tuned_report_hash_mismatch")
    if metric["name"] != policy["metric"]:
        reasons.append("metric_mismatch")
    if bootstrap["seed"] != policy["seed"]:
        reasons.append("seed_mismatch")
    if bootstrap["resamples"] < policy["minimum_resamples"]:
        reasons.append("insufficient_resamples")
    if not math.isclose(float(bootstrap["confidence_level"]), float(policy["confidence_level"]), abs_tol=1e-12):
        reasons.append("confidence_level_mismatch")
    if not math.isclose(float(paired_delta["minimum"]), float(policy["minimum_delta"]), abs_tol=1e-12):
        reasons.append("minimum_delta_mismatch")
    if bootstrap["passed"] is not True:
        reasons.append("significance_not_passed")
    return {
        "status": validated["status"],
        "passed": not reasons,
        "reasons": reasons,
        "report": validated,
    }


def verify_release_evidence(
    evaluation_report: Mapping[str, Any],
    judge_test_report: Mapping[str, Any],
    judge_challenge_report: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    candidate: str,
    config_sha256: str,
    paired_significance_report: Mapping[str, Any] | None = None,
    evaluation_report_sha256: str | None = None,
    completed_training_report: Mapping[str, Any] | None = None,
    completed_training_report_sha256: str | None = None,
    private_hub_report: Mapping[str, Any] | None = None,
    private_hub_report_sha256: str | None = None,
    variant_release_component: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute gates and emit only evidence-supported product lifecycle states."""

    validated = validate_evaluation_config(config)
    if candidate not in validated["candidates"]:
        raise ValueError(f"candidate is not declared in evaluation config: {candidate}")
    evaluation_reasons: list[str] = []
    if evaluation_report.get("candidate") != candidate:
        evaluation_reasons.append("candidate_mismatch")
    if evaluation_report.get("evaluation_config_sha256") != config_sha256:
        evaluation_reasons.append("config_hash_mismatch")
    if evaluation_report.get("exact_case_coverage") is not True:
        evaluation_reasons.append("incomplete_case_coverage")
    metrics = _required_mapping(evaluation_report.get("metrics"), "evaluation metrics")
    recomputed_evaluation = evaluate_candidate_gate(metrics, validated, candidate=candidate)
    if evaluation_report.get("gate") != recomputed_evaluation:
        evaluation_reasons.append("declared_gate_differs_from_recomputed_gate")
    evaluation_component = {
        "passed": recomputed_evaluation["passed"] and not evaluation_reasons,
        "reasons": evaluation_reasons,
        "gate": recomputed_evaluation,
    }
    judge_test_component = _judge_component(
        judge_test_report,
        validated,
        split="test",
        candidate=candidate,
        config_sha256=config_sha256,
    )
    judge_challenge_component = _judge_component(
        judge_challenge_report,
        validated,
        split="challenge",
        candidate=candidate,
        config_sha256=config_sha256,
    )
    components = {
        "automatic_evaluation": evaluation_component,
        "judge:test": judge_test_component,
        "judge:challenge": judge_challenge_component,
        "completed_final_training": _completed_training_component(
            completed_training_report,
            report_sha256=completed_training_report_sha256,
        ),
        "private_hub_publication": _private_hub_component(
            private_hub_report,
            report_sha256=private_hub_report_sha256,
            completed_training_report=completed_training_report,
            completed_training_report_sha256=completed_training_report_sha256,
        ),
    }
    paired_policy = validated.get("paired_significance")
    if paired_policy is not None and candidate in paired_policy["required_for_release_candidates"]:
        components["paired_significance"] = _paired_significance_component(
            paired_significance_report,
            paired_policy,
            candidate=candidate,
            config_sha256=config_sha256,
            reference_sha256=evaluation_report.get("reference_sha256"),
            evaluation_report_sha256=evaluation_report_sha256,
        )
    components["variant_release_matrix"] = _variant_release_component(
        variant_release_component
    )
    pending = any(
        component.get("status") == "pending_tiebreak"
        for component in (judge_test_component, judge_challenge_component)
    )
    failed_components = [name for name, component in components.items() if not component["passed"]]
    passed = not pending and not failed_components
    training_verified = components["completed_final_training"]["passed"] is True
    private_hub_verified = components["private_hub_publication"]["passed"] is True
    if not training_verified:
        product_status = "READY_FOR_TRAINING"
    elif pending:
        product_status = "READY_FOR_AI_JUDGING"
    elif passed:
        product_status = "RELEASED"
    elif private_hub_verified:
        product_status = "PRIVATE_CANDIDATE"
    else:
        product_status = "READY_FOR_AI_JUDGING"
    if product_status == "PRIVATE_CANDIDATE" and not (
        training_verified and private_hub_verified and not pending and not passed
    ):
        raise AssertionError("PRIVATE_CANDIDATE lacks its required evidence")
    if product_status == "RELEASED" and not (
        training_verified and private_hub_verified and passed
    ):
        raise AssertionError("RELEASED lacks its required evidence")
    slice_reports = _required_mapping(metrics.get("slice_reports"), "evaluation slice reports")
    decision = {
        "schema_version": 2,
        "candidate": candidate,
        "evaluation_config_sha256": config_sha256,
        "status": product_status,
        "passed": passed,
        "components": components,
        "failed_components": failed_components,
        "evaluation_slice_reports": dict(slice_reports),
        "status_basis": {
            "completed_final_training_verified": training_verified,
            "private_hub_upload_and_reload_verified": private_hub_verified,
            "ai_judging_pending": pending,
            "all_release_components_passed": passed,
        },
    }
    errors = sorted(
        _contract_validator("release_verification_report_schema.json").iter_errors(decision),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise ValueError(
            f"release verification report schema failed at {location}: {errors[0].message}"
        )
    if decision["status"] not in PRODUCT_STATUS_VALUES:
        raise AssertionError("release verification emitted a non-product status")
    return decision

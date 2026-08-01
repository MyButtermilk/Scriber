"""Fail-closed Terra/Sol aggregation over hash-bound, materialized judge stages."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .judge_bundle import (
    JUDGE_COMPARISON_KINDS,
    JUDGE_SCORE_DIMENSIONS,
    judge_coverage_policy_sha256,
    judge_materialization_plan_sha256,
    judge_stage_assignment_sha256,
    validate_judge_bundle_case,
    validate_judge_coverage_policy,
    validate_judge_materialization_plan,
    validate_judge_stage_packet_manifest,
)
from .schemas import (
    validate_judge_campaign_report,
    validate_judge_stage_output_manifest,
    validate_judge_verdict,
)


@dataclass(frozen=True, slots=True)
class JudgeCaseDecision:
    base_case_id: str
    candidate_label: str | None
    candidates_identical: bool
    acceptable: bool
    deterministic_critical_flags: tuple[str, ...]
    judge_critical_flags: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    position_conflict: bool
    judge_disagreement: bool
    tie_break_used: bool
    rubric_scores: dict[str, float]


@dataclass(frozen=True, slots=True)
class JudgeCandidateSummary:
    candidate_label: str
    total_cases: int
    win_count: int
    equivalent_count: int
    win_rate: float
    acceptable_count: int
    acceptable_rate: float
    selected_acceptance_rate: float
    deterministic_critical_case_count: int
    judge_critical_case_count: int
    rubric_observation_count: int
    rubric_averages: dict[str, float | None]


@dataclass(frozen=True, slots=True)
class JudgeAggregationResult:
    status: str
    evaluation_split: str
    comparison_kind: str
    bundle_sha256: str
    materialization_plan_sha256: str
    coverage_policy_sha256: str
    decisions: dict[str, JudgeCaseDecision]
    required_sol_release_case_ids: tuple[str, ...]
    required_tie_break_case_ids: tuple[str, ...]
    missing_tie_break_case_ids: tuple[str, ...]
    pending_case_count: int
    accepted_count: int
    rejected_count: int
    acceptance_rate: float
    candidate_summaries: dict[str, JudgeCandidateSummary]
    stratum_candidate_summaries: dict[str, dict[str, dict[str, Any]]]
    terra_stage_candidate_summaries: dict[str, dict[str, dict[str, Any]]]
    winning_candidate_labels: tuple[str, ...]
    rubric_averages: dict[str, float | None]
    release_candidate: str | None
    release_gate: dict[str, dict[str, Any]]
    comparison_gate_passed: bool | None
    release_gate_passed: bool
    stage_coverage: dict[str, dict[str, Any]]
    judge_ids: dict[str, str]
    judge_session_ids: dict[str, str]
    prompt_hashes: dict[str, str]
    stage_assignment_sha256s: dict[str, str]
    verdict_output_sha256s: dict[str, str]
    stage_output_manifest_sha256s: dict[str, str]


@dataclass(frozen=True, slots=True)
class _StageDecision:
    candidate_label: str | None
    candidates_identical: bool
    acceptable: bool
    critical_flags: tuple[str, ...]
    position_conflict: bool
    rubric_scores: dict[str, float]


@dataclass(frozen=True, slots=True)
class _ValidatedStage:
    by_id: dict[str, dict[str, Any]]
    judge_id: str
    judge_session_id: str
    prompt_hash: str
    assignment_sha256: str
    verdict_output_sha256: str
    manifest_sha256: str
    missing_case_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JudgeStageResumeResult:
    status: str
    verdicts: tuple[dict[str, Any], ...]
    completed_count: int
    missing_case_ids: tuple[str, ...]
    output_manifest: dict[str, Any] | None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256((_canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def judge_bundle_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    """Hash the exact canonical JSONL representation used by the bundle writer."""

    if not cases:
        raise ValueError("judge bundle must contain at least one case")
    payload = ("\n".join(_canonical_json(case) for case in cases) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def judge_case_ids_sha256(case_ids: Sequence[str]) -> str:
    """Hash one unique case set in stable order."""

    ordered = tuple(sorted(case_ids))
    if not ordered:
        raise ValueError("judge case ID set must not be empty")
    if len(ordered) != len(set(ordered)):
        raise ValueError("judge case ID set contains duplicates")
    return _canonical_json_sha256({"case_ids": list(ordered)})


def judge_verdicts_sha256(verdicts: Sequence[Mapping[str, Any]]) -> str:
    """Hash the canonical JSONL bytes of a non-empty verdict output."""

    if not verdicts:
        raise ValueError("judge verdict output must not be empty")
    payload = ("\n".join(_canonical_json(verdict) for verdict in verdicts) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def judge_stage_output_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a validated stage output manifest."""

    return _canonical_json_sha256(validate_judge_stage_output_manifest(manifest))


def build_judge_stage_output_manifest(
    verdicts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind already-produced verdicts to their exact session, assignment, and bytes."""

    validated = tuple(validate_judge_verdict(verdict) for verdict in verdicts)
    if not validated:
        raise ValueError("judge verdict output must not be empty")
    binding_fields = (
        "judge_stage",
        "judge_id",
        "judge_session_id",
        "model_family",
        "reasoning_effort",
        "prompt_hash",
        "reviewed_bundle_sha256",
        "stage_assignment_sha256",
    )
    bindings: dict[str, Any] = {}
    for field in binding_fields:
        values = {verdict[field] for verdict in validated}
        if len(values) != 1:
            raise ValueError(f"judge verdict output changes {field}")
        bindings[field] = next(iter(values))
    case_ids = [verdict["case_id"] for verdict in validated]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("judge verdict output contains duplicate case IDs")
    return validate_judge_stage_output_manifest(
        {
            "schema_version": 1,
            **bindings,
            "case_ids_sha256": judge_case_ids_sha256(case_ids),
            "verdict_output_sha256": judge_verdicts_sha256(validated),
            "verdict_count": len(validated),
        }
    )


def resume_judge_stage_verdicts(
    stage_cases: Sequence[Mapping[str, Any]],
    stage_packet_manifest: Mapping[str, Any],
    verdict_batches: Sequence[Sequence[Mapping[str, Any]]],
) -> JudgeStageResumeResult:
    """Merge resumable batches for one exact stage without weakening its identity."""

    cases = tuple(validate_judge_bundle_case(case) for case in stage_cases)
    if not cases:
        raise ValueError("judge stage packet must not be empty")
    case_ids = tuple(case["case_id"] for case in cases)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("judge stage packet contains duplicate case IDs")
    manifest = validate_judge_stage_packet_manifest(stage_packet_manifest)
    if manifest["assigned_public_case_count"] != len(cases):
        raise ValueError("judge stage packet count differs from manifest")
    if manifest["stage_assignment_public_case_count"] != manifest["assigned_public_case_count"]:
        raise ValueError("judge resume requires the complete original stage packet")
    if manifest["case_ids_sha256"] != judge_case_ids_sha256(case_ids):
        raise ValueError("judge stage packet case set differs from manifest")
    if manifest["stage_assignment_case_ids_sha256"] != manifest["case_ids_sha256"]:
        raise ValueError("judge stage packet does not cover its complete assignment")
    if manifest["stage_bundle_sha256"] != judge_bundle_sha256(cases):
        raise ValueError("judge stage packet bytes differ from manifest")

    by_id: dict[str, dict[str, Any]] = {}
    for batch in verdict_batches:
        for raw_verdict in batch:
            verdict = validate_judge_verdict(raw_verdict)
            case_id = verdict["case_id"]
            if case_id not in set(case_ids):
                raise ValueError(f"judge verdict references an unassigned case: {case_id}")
            if case_id in by_id:
                raise ValueError(f"duplicate resumed judge verdict: {case_id}")
            by_id[case_id] = verdict
    if not by_id:
        raise ValueError("judge resume requires at least one verdict")

    packet_bindings = {
        "judge_stage": manifest["judge_stage"],
        "model_family": manifest["model_family"],
        "reasoning_effort": manifest["reasoning_effort"],
        "prompt_hash": manifest["prompt_hash"],
        "reviewed_bundle_sha256": manifest["reviewed_bundle_sha256"],
        "stage_assignment_sha256": manifest["stage_assignment_sha256"],
    }
    for field, expected in packet_bindings.items():
        observed = {verdict[field] for verdict in by_id.values()}
        if observed != {expected}:
            raise ValueError(f"resumed judge verdicts differ on {field}")
    for field in ("judge_id", "judge_session_id"):
        observed = {verdict[field] for verdict in by_id.values()}
        if len(observed) != 1:
            raise ValueError(f"resumed judge verdicts differ on {field}")

    ordered_verdicts = tuple(by_id[case_id] for case_id in case_ids if case_id in by_id)
    missing = tuple(case_id for case_id in case_ids if case_id not in by_id)
    output_manifest = None if missing else build_judge_stage_output_manifest(ordered_verdicts)
    return JudgeStageResumeResult(
        status="in_progress" if missing else "complete",
        verdicts=ordered_verdicts,
        completed_count=len(ordered_verdicts),
        missing_case_ids=missing,
        output_manifest=output_manifest,
    )


def _validate_bundle_and_mapping(
    cases: Sequence[Mapping[str, Any]],
    private_mapping: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[str, ...]]]:
    validated_cases = tuple(validate_judge_bundle_case(case) for case in cases)
    case_ids = tuple(case["case_id"] for case in validated_cases)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("judge bundle case IDs must be unique")
    if set(private_mapping) != set(case_ids):
        raise ValueError("private judge mapping must cover the exact public bundle")

    cases_by_id = {case["case_id"]: case for case in validated_cases}
    groups: dict[str, list[str]] = {}
    for public_case_id in case_ids:
        mapping = private_mapping[public_case_id]
        required = {
            "public_case_id",
            "base_case_id",
            "position_variant",
            "candidate_a_label",
            "candidate_b_label",
            "candidate_outputs_identical",
            "paired_public_case_id",
        }
        if set(mapping) != required:
            raise ValueError(f"private judge mapping has unexpected shape: {public_case_id}")
        if mapping["public_case_id"] != public_case_id:
            raise ValueError(f"private judge mapping public identity differs: {public_case_id}")
        base_case_id = mapping["base_case_id"]
        if not isinstance(base_case_id, str) or not base_case_id:
            raise ValueError(f"private judge mapping has invalid base_case_id: {public_case_id}")
        if mapping["position_variant"] not in {"primary", "swapped"}:
            raise ValueError(f"private judge mapping has invalid position variant: {public_case_id}")
        if (
            not isinstance(mapping["candidate_a_label"], str)
            or not isinstance(mapping["candidate_b_label"], str)
            or not mapping["candidate_a_label"]
            or not mapping["candidate_b_label"]
            or mapping["candidate_a_label"] == mapping["candidate_b_label"]
        ):
            raise ValueError(f"private judge mapping has invalid candidate labels: {public_case_id}")
        if not isinstance(mapping["candidate_outputs_identical"], bool):
            raise ValueError(f"private judge mapping has invalid candidate equivalence: {public_case_id}")
        public_case = cases_by_id[public_case_id]
        outputs_identical = public_case["candidate_a"] == public_case["candidate_b"]
        if mapping["candidate_outputs_identical"] is not outputs_identical:
            raise ValueError(f"private judge mapping candidate equivalence differs: {public_case_id}")
        if outputs_identical and (
            public_case["deterministic_critical_flags"]["A"] != public_case["deterministic_critical_flags"]["B"]
        ):
            raise ValueError(f"identical judge outputs have different deterministic flags: {public_case_id}")
        groups.setdefault(base_case_id, []).append(public_case_id)

    for base_case_id, public_ids in groups.items():
        variants = [private_mapping[case_id]["position_variant"] for case_id in public_ids]
        if variants.count("primary") != 1 or variants.count("swapped") > 1:
            raise ValueError(f"private judge mapping has invalid variants for base case: {base_case_id}")
        if len(public_ids) not in {1, 2}:
            raise ValueError(f"private judge mapping has too many variants: {base_case_id}")
        for public_case_id in public_ids:
            paired = private_mapping[public_case_id]["paired_public_case_id"]
            expected_peers = [case_id for case_id in public_ids if case_id != public_case_id]
            if paired != (expected_peers[0] if expected_peers else None):
                raise ValueError(f"private judge mapping has invalid pair link: {public_case_id}")
        if len(public_ids) == 2:
            primary_id = next(
                case_id for case_id in public_ids if private_mapping[case_id]["position_variant"] == "primary"
            )
            swapped_id = next(
                case_id for case_id in public_ids if private_mapping[case_id]["position_variant"] == "swapped"
            )
            primary_mapping = private_mapping[primary_id]
            swapped_mapping = private_mapping[swapped_id]
            primary_case = cases_by_id[primary_id]
            swapped_case = cases_by_id[swapped_id]
            if (
                primary_mapping["candidate_a_label"] != swapped_mapping["candidate_b_label"]
                or primary_mapping["candidate_b_label"] != swapped_mapping["candidate_a_label"]
                or primary_case["candidate_a"] != swapped_case["candidate_b"]
                or primary_case["candidate_b"] != swapped_case["candidate_a"]
                or primary_case["deterministic_critical_flags"]["A"]
                != swapped_case["deterministic_critical_flags"]["B"]
                or primary_case["deterministic_critical_flags"]["B"]
                != swapped_case["deterministic_critical_flags"]["A"]
            ):
                raise ValueError(f"swapped judge case must reverse A/B exactly: {base_case_id}")
        equivalence_values = {private_mapping[case_id]["candidate_outputs_identical"] for case_id in public_ids}
        if len(equivalence_values) != 1:
            raise ValueError(f"candidate equivalence changes across swapped cases: {base_case_id}")
    duplicated_base_count = sum(len(public_ids) == 2 for public_ids in groups.values())
    if duplicated_base_count * 2 < len(groups):
        raise ValueError("judge bundle must swap at least half of its base pairwise cases")
    return validated_cases, {
        base_case_id: tuple(sorted(public_ids)) for base_case_id, public_ids in sorted(groups.items())
    }


def _public_ids_for_bases(
    base_case_ids: Sequence[str],
    groups: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    return tuple(sorted(public_case_id for base_case_id in base_case_ids for public_case_id in groups[base_case_id]))


def _validate_materialization(
    plan: Mapping[str, Any],
    *,
    bundle_sha256: str,
    public_case_count: int,
    groups: Mapping[str, tuple[str, ...]],
    cases_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    validated = validate_judge_materialization_plan(plan)
    if not validated["coverage_passed"]:
        raise ValueError("judge materialization coverage is not complete")
    if validated["coverage"]["deficits"]:
        raise ValueError("complete judge materialization must not retain coverage deficits")
    if validated["bundle_sha256"] != bundle_sha256:
        raise ValueError("judge materialization plan is bound to another bundle")
    if validated["base_case_count"] != len(groups):
        raise ValueError("judge materialization base-case count differs from bundle")
    if validated["public_case_count"] != public_case_count:
        raise ValueError("judge materialization public-case count differs from bundle")
    if set(validated["base_case_metadata"]) != set(groups):
        raise ValueError("judge materialization metadata must cover every base case exactly")
    if set(validated["primary_stage_by_base_case"]) != set(groups):
        raise ValueError("judge materialization primary allocation must cover every base case")

    requirements = validated["coverage_requirements"]
    reconstructed_policy = validate_judge_coverage_policy(
        {
            "schema_version": 1,
            "selection_seed": validated["selection_seed"],
            "regular_test_minimum": requirements["regular_test_minimum"],
            "ai_gold_test_expected_count": requirements["ai_gold_test_expected_count"],
            "ai_gold_challenge_expected_count": requirements["ai_gold_challenge_expected_count"],
            "critical_regression_expected_count": requirements["critical_regression_expected_count"],
            "terra_accepted_sample_fraction": validated["sol_release_dynamic_policy"]["terra_accepted_sample_fraction"],
            "generator_family_tag_prefix": requirements["generator_family_tag_prefix"],
            "required_generator_families": requirements["required_generator_families"],
            "required_comparison_kinds": sorted(JUDGE_COMPARISON_KINDS),
            "category_quotas": requirements["category_quotas"],
        }
    )
    if judge_coverage_policy_sha256(reconstructed_policy) != validated["coverage_policy_sha256"]:
        raise ValueError("judge materialization coverage requirements differ from policy hash")

    metadata = validated["base_case_metadata"]
    for base_case_id, item in metadata.items():
        flags: set[str] = set()
        conflicting_automatic_metrics = False
        for public_case_id in groups[base_case_id]:
            critical = cases_by_id[public_case_id]["deterministic_critical_flags"]
            flags_a = set(critical["A"])
            flags_b = set(critical["B"])
            flags.update(flags_a)
            flags.update(flags_b)
            conflicting_automatic_metrics = conflicting_automatic_metrics or flags_a != flags_b
        tag_set = set(item["slice_tags"])
        expected_families = sorted(
            tag for tag in tag_set if tag.startswith(requirements["generator_family_tag_prefix"])
        )
        if item["generator_families"] != expected_families:
            raise ValueError(f"judge materialization generator families differ from tags: {base_case_id}")
        expected_categories = sorted(
            quota["id"]
            for quota in requirements["category_quotas"]
            if (not quota["any_slice_tags"] or set(quota["any_slice_tags"]).intersection(tag_set))
            and ("is_identity" not in quota or item["is_identity"] is quota["is_identity"])
        )
        if item["coverage_categories"] != expected_categories:
            raise ValueError(f"judge materialization categories differ from tags: {base_case_id}")
        expected_suspicious = bool(flags) or bool(tag_set.intersection({"automatic_suspicious", "automatic_warning"}))
        if item["deterministic_critical_flags"] != sorted(flags):
            raise ValueError(f"judge materialization deterministic flags differ from bundle: {base_case_id}")
        if item["automatic_suspicious"] is not expected_suspicious:
            raise ValueError(f"judge materialization suspicious marker differs from bundle: {base_case_id}")
        if item["deterministic_warning"] is not expected_suspicious:
            raise ValueError(f"judge materialization warning marker differs from bundle: {base_case_id}")
        if item["possible_critical_fact_error"] is not bool(flags):
            raise ValueError(f"judge materialization critical marker differs from bundle: {base_case_id}")
        expected_metric_conflict = conflicting_automatic_metrics or "automatic_metric_conflict" in tag_set
        if item["conflicting_automatic_metrics"] is not expected_metric_conflict:
            raise ValueError(f"judge materialization metric-conflict marker differs from bundle: {base_case_id}")

    expected_stratum_counts = {
        "regular_test": sum(item["split"] == "test" for item in metadata.values()),
        "ai_gold_test": sum(item["split"] == "ai_gold_test" for item in metadata.values()),
        "ai_gold_challenge": sum(item["split"] == "ai_gold_challenge" for item in metadata.values()),
        "critical_regression": sum(
            item["split"] == "regression" or "critical_regression" in item["slice_tags"] for item in metadata.values()
        ),
        "identity": sum(item["is_identity"] for item in metadata.values()),
        "automatic_suspicious": sum(item["automatic_suspicious"] for item in metadata.values()),
        "deterministic_warning": sum(item["deterministic_warning"] for item in metadata.values()),
    }
    expected_stratum_requirements = {
        "regular_test": requirements["regular_test_minimum"],
        "ai_gold_test": requirements["ai_gold_test_expected_count"],
        "ai_gold_challenge": requirements["ai_gold_challenge_expected_count"],
        "critical_regression": requirements["critical_regression_expected_count"],
        "identity": expected_stratum_counts["identity"],
        "automatic_suspicious": expected_stratum_counts["automatic_suspicious"],
        "deterministic_warning": expected_stratum_counts["deterministic_warning"],
    }
    for stratum_id, result in validated["coverage"]["strata"].items():
        if (
            result["available"] != expected_stratum_counts[stratum_id]
            or result["selected"] != expected_stratum_counts[stratum_id]
            or result["required"] != expected_stratum_requirements[stratum_id]
        ):
            raise ValueError(f"judge materialization stratum count differs from cases: {stratum_id}")
        if not result["passed"] or result["selected"] < result["required"]:
            raise ValueError("judge materialization has an unsatisfied mandatory stratum")
        if result["coverage_mode"] == "all" and result["selected"] != result["available"]:
            raise ValueError("all-mode judge stratum was not fully materialized")
        if result["coverage_mode"] == "exact" and not (result["selected"] == result["available"] == result["required"]):
            raise ValueError("exact-mode judge stratum differs from its frozen count")

    family_coverage = validated["coverage"]["generator_families"]
    if family_coverage["required"] != sorted(requirements["required_generator_families"]):
        raise ValueError("judge materialization required generator families differ from policy")
    observed_families = sorted({family for item in metadata.values() for family in item["generator_families"]})
    expected_family_counts = {
        family: sum(family in item["generator_families"] for item in metadata.values())
        for family in sorted(set(observed_families) | set(family_coverage["required"]))
    }
    expected_missing_families = sorted(set(family_coverage["required"]).difference(observed_families))
    if (
        family_coverage["observed"] != observed_families
        or family_coverage["counts"] != expected_family_counts
        or family_coverage["missing"] != expected_missing_families
    ):
        raise ValueError("judge materialization generator-family counts differ from cases")
    if not family_coverage["passed"]:
        raise ValueError("judge materialization lacks a required generator family")
    if family_coverage["missing"]:
        raise ValueError("judge materialization retains a missing generator family")
    quota_minimums = {quota["id"]: quota["minimum"] for quota in requirements["category_quotas"]}
    if set(validated["coverage"]["category_quotas"]) != set(quota_minimums):
        raise ValueError("judge materialization category quotas differ from policy")
    for category_id, result in validated["coverage"]["category_quotas"].items():
        actual_count = sum(category_id in item["coverage_categories"] for item in metadata.values())
        if (
            result["available"] != actual_count
            or result["selected"] != actual_count
            or result["required"] != quota_minimums[category_id]
        ):
            raise ValueError(f"judge materialization category count differs from cases: {category_id}")
        if not result["passed"] or result["selected"] < result["required"]:
            raise ValueError("judge materialization has an unsatisfied category quota")

    stage_names = {
        "terra_prejudge": "terra_prejudge",
        "sol_release_static": "sol_release",
        "sol_tiebreak_static": "sol_tiebreak",
    }
    stage_bases: dict[str, set[str]] = {}
    for plan_stage, judge_stage in stage_names.items():
        assignment = validated["stage_assignments"][plan_stage]
        base_case_ids = assignment["base_case_ids"]
        if len(base_case_ids) != len(set(base_case_ids)):
            raise ValueError(f"judge stage has duplicate base cases: {plan_stage}")
        if not set(base_case_ids).issubset(groups):
            raise ValueError(f"judge stage references an unknown base case: {plan_stage}")
        expected_public_ids = _public_ids_for_bases(base_case_ids, groups)
        if tuple(assignment["public_case_ids"]) != expected_public_ids:
            raise ValueError(f"judge stage public cases differ from base allocation: {plan_stage}")
        expected_hash = judge_stage_assignment_sha256(
            judge_stage,
            bundle_sha256,
            expected_public_ids,
        )
        if assignment["assignment_sha256"] != expected_hash:
            raise ValueError(f"judge stage assignment hash differs: {plan_stage}")
        stage_bases[plan_stage] = set(base_case_ids)

    terra_primary = {
        base_case_id
        for base_case_id, stage in validated["primary_stage_by_base_case"].items()
        if stage == "terra_prejudge"
    }
    sol_primary = {
        base_case_id
        for base_case_id, stage in validated["primary_stage_by_base_case"].items()
        if stage == "sol_release"
    }
    if terra_primary & sol_primary or terra_primary | sol_primary != set(groups):
        raise ValueError("judge primary allocation must be complete and disjoint")
    if not terra_primary.issubset(stage_bases["terra_prejudge"]):
        raise ValueError("Terra stage omits a primary Terra case")
    if not sol_primary.issubset(stage_bases["sol_release_static"]):
        raise ValueError("Sol stage omits a primary Sol case")

    required_generator_families = set(validated["coverage"]["generator_families"]["required"])
    required_terra = {
        base_case_id
        for base_case_id, item in metadata.items()
        if item["split"] in {"test", "ai_gold_test"}
        or item["automatic_suspicious"]
        or required_generator_families.intersection(item["generator_families"])
        or item["coverage_categories"]
    }
    if not required_terra.issubset(stage_bases["terra_prejudge"]):
        raise ValueError("Terra stage omits mandatory test, suspicious, or generator-family cases")
    required_sol = {
        base_case_id
        for base_case_id, item in metadata.items()
        if item["split"] in {"challenge", "ai_gold_challenge", "regression"} or item["deterministic_warning"]
    }
    if not required_sol.issubset(stage_bases["sol_release_static"]):
        raise ValueError("Sol stage omits mandatory challenge, warning, or regression cases")
    expected_tie_static = {
        base_case_id
        for base_case_id, item in metadata.items()
        if item["possible_critical_fact_error"] or item["conflicting_automatic_metrics"]
    }
    if stage_bases["sol_tiebreak_static"] != expected_tie_static:
        raise ValueError("Sol tie-break static assignment omits or adds mandatory critical cases")
    return validated, judge_materialization_plan_sha256(validated)


def _validate_stage(
    verdicts: Sequence[Mapping[str, Any]],
    output_manifest: Mapping[str, Any],
    *,
    expected_case_ids: set[str],
    expected_stage: str,
    expected_bundle_sha256: str,
    expected_assignment_sha256: str,
    allow_missing: bool = False,
) -> _ValidatedStage:
    validated = tuple(validate_judge_verdict(verdict) for verdict in verdicts)
    if not validated:
        raise ValueError(f"verdict file is empty: {expected_stage}")
    manifest = validate_judge_stage_output_manifest(output_manifest)
    by_id: dict[str, dict[str, Any]] = {}
    for verdict in validated:
        if verdict["case_id"] in by_id:
            raise ValueError(f"duplicate verdict for {expected_stage}: {verdict['case_id']}")
        by_id[verdict["case_id"]] = verdict
    missing = sorted(expected_case_ids.difference(by_id))
    if missing and not allow_missing:
        raise ValueError(f"missing verdict for {expected_stage}: {missing[0]}")
    extra = sorted(set(by_id).difference(expected_case_ids))
    if extra:
        raise ValueError(f"unexpected verdict for {expected_stage}: {extra[0]}")
    if manifest["judge_stage"] != expected_stage:
        raise ValueError(f"verdict manifest has the wrong judge stage: {expected_stage}")
    if manifest["reviewed_bundle_sha256"] != expected_bundle_sha256:
        raise ValueError(f"verdict manifest is bound to another judge bundle: {expected_stage}")
    if manifest["stage_assignment_sha256"] != expected_assignment_sha256:
        raise ValueError(f"verdict manifest is bound to another stage assignment: {expected_stage}")
    binding_fields = (
        "judge_stage",
        "judge_id",
        "judge_session_id",
        "model_family",
        "reasoning_effort",
        "prompt_hash",
        "reviewed_bundle_sha256",
        "stage_assignment_sha256",
    )
    for field in binding_fields:
        values = {verdict[field] for verdict in validated}
        if values != {manifest[field]}:
            raise ValueError(f"verdict output differs from manifest {field}: {expected_stage}")
    if manifest["verdict_count"] != len(validated):
        raise ValueError(f"verdict manifest count differs from output: {expected_stage}")
    if manifest["case_ids_sha256"] != judge_case_ids_sha256(tuple(by_id)):
        raise ValueError(f"verdict manifest case set differs from output: {expected_stage}")
    verdict_hash = judge_verdicts_sha256(validated)
    if manifest["verdict_output_sha256"] != verdict_hash:
        raise ValueError(f"verdict manifest output hash differs: {expected_stage}")
    return _ValidatedStage(
        by_id=by_id,
        judge_id=manifest["judge_id"],
        judge_session_id=manifest["judge_session_id"],
        prompt_hash=manifest["prompt_hash"],
        assignment_sha256=manifest["stage_assignment_sha256"],
        verdict_output_sha256=verdict_hash,
        manifest_sha256=judge_stage_output_manifest_sha256(manifest),
        missing_case_ids=tuple(missing),
    )


def _stage_decisions(
    by_public_id: Mapping[str, Mapping[str, Any]],
    groups: Mapping[str, tuple[str, ...]],
    private_mapping: Mapping[str, Mapping[str, Any]],
) -> dict[str, _StageDecision]:
    decisions: dict[str, _StageDecision] = {}
    for base_case_id, public_ids in groups.items():
        candidates_identical = bool(private_mapping[public_ids[0]]["candidate_outputs_identical"])
        labels: list[str] = []
        acceptable_observations: list[bool] = []
        flag_observations: list[tuple[str, ...]] = []
        flags: set[str] = set()
        score_values: dict[str, list[int]] = {dimension: [] for dimension in JUDGE_SCORE_DIMENSIONS}
        for public_case_id in public_ids:
            verdict = by_public_id[public_case_id]
            mapping = private_mapping[public_case_id]
            label = mapping["candidate_a_label"] if verdict["candidate"] == "A" else mapping["candidate_b_label"]
            labels.append(label)
            acceptable_observations.append(verdict["acceptable"] and not verdict["critical_error"])
            flag_observations.append(tuple(sorted(verdict["flags"])))
            flags.update(verdict["flags"])
            for dimension in JUDGE_SCORE_DIMENSIONS:
                score_values[dimension].append(verdict["scores"][dimension])
        distinct_labels = set(labels)
        position_conflict = (
            (not candidates_identical and len(distinct_labels) != 1)
            or len(set(acceptable_observations)) != 1
            or len(set(flag_observations)) != 1
        )
        decisions[base_case_id] = _StageDecision(
            candidate_label=(None if candidates_identical else labels[0] if len(distinct_labels) == 1 else None),
            candidates_identical=candidates_identical,
            acceptable=all(acceptable_observations),
            critical_flags=tuple(sorted(flags)),
            position_conflict=position_conflict,
            rubric_scores={dimension: sum(values) / len(values) for dimension, values in score_values.items()},
        )
    return decisions


def _deterministic_flags_by_label(
    cases_by_id: Mapping[str, Mapping[str, Any]],
    groups: Mapping[str, tuple[str, ...]],
    private_mapping: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for base_case_id, public_ids in groups.items():
        flags_by_label: dict[str, tuple[str, ...]] = {}
        for public_case_id in public_ids:
            case = cases_by_id[public_case_id]
            mapping = private_mapping[public_case_id]
            for position in ("A", "B"):
                label = mapping[f"candidate_{position.lower()}_label"]
                flags = tuple(sorted(case["deterministic_critical_flags"][position]))
                previous = flags_by_label.setdefault(label, flags)
                if previous != flags:
                    raise ValueError(f"deterministic flags change across swapped cases: {base_case_id} {label}")
        result[base_case_id] = flags_by_label
    return result


def _average_score_maps(*score_maps: Mapping[str, float]) -> dict[str, float]:
    return {
        dimension: sum(scores[dimension] for scores in score_maps) / len(score_maps)
        for dimension in JUDGE_SCORE_DIMENSIONS
    }


def _selection_order(
    base_case_ids: Sequence[str],
    *,
    selection_seed: int,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            base_case_ids,
            key=lambda case_id: (
                hashlib.sha256(f"{selection_seed}:{namespace}:{case_id}".encode()).hexdigest(),
                case_id,
            ),
        )
    )


def _derive_sol_release_base_ids(
    plan: Mapping[str, Any],
    terra: Mapping[str, _StageDecision],
) -> tuple[set[str], set[str], tuple[str, ...]]:
    static = set(plan["stage_assignments"]["sol_release_static"]["base_case_ids"])
    objections = {
        base_case_id
        for base_case_id, decision in terra.items()
        if decision.position_conflict or not decision.acceptable or bool(decision.critical_flags)
    }
    accepted = tuple(
        base_case_id
        for base_case_id, decision in terra.items()
        if base_case_id not in objections
        and not decision.position_conflict
        and decision.acceptable
        and not decision.critical_flags
    )
    fraction = plan["sol_release_dynamic_policy"]["terra_accepted_sample_fraction"]
    sample_count = math.ceil(len(accepted) * fraction)
    ordered_accepted = _selection_order(
        accepted,
        selection_seed=plan["selection_seed"],
        namespace=plan["sol_release_dynamic_policy"]["selection_namespace"],
    )
    sampled = ordered_accepted[:sample_count]
    return static | objections | set(sampled), objections, sampled


def materialize_sol_release_assignment(
    cases: Sequence[Mapping[str, Any]],
    private_mapping: Mapping[str, Mapping[str, Any]],
    materialization_plan: Mapping[str, Any],
    terra_verdicts: Sequence[Mapping[str, Any]],
    terra_output_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the exact Sol-Max case set after a complete Terra-Max stage."""

    validated_cases, groups = _validate_bundle_and_mapping(cases, private_mapping)
    bundle_hash = judge_bundle_sha256(validated_cases)
    plan, plan_hash = _validate_materialization(
        materialization_plan,
        bundle_sha256=bundle_hash,
        public_case_count=len(validated_cases),
        groups=groups,
        cases_by_id={case["case_id"]: case for case in validated_cases},
    )
    terra_public_ids = tuple(plan["stage_assignments"]["terra_prejudge"]["public_case_ids"])
    terra_stage = _validate_stage(
        terra_verdicts,
        terra_output_manifest,
        expected_case_ids=set(terra_public_ids),
        expected_stage="terra_prejudge",
        expected_bundle_sha256=bundle_hash,
        expected_assignment_sha256=plan["stage_assignments"]["terra_prejudge"]["assignment_sha256"],
    )
    terra_base_ids = plan["stage_assignments"]["terra_prejudge"]["base_case_ids"]
    terra_groups = {base_case_id: groups[base_case_id] for base_case_id in terra_base_ids}
    terra = _stage_decisions(terra_stage.by_id, terra_groups, private_mapping)
    sol_base_ids, objections, sampled = _derive_sol_release_base_ids(plan, terra)
    public_case_ids = _public_ids_for_bases(sorted(sol_base_ids), groups)
    return {
        "schema_version": 1,
        "judge_stage": "sol_release",
        "bundle_sha256": bundle_hash,
        "materialization_plan_sha256": plan_hash,
        "terra_output_manifest_sha256": terra_stage.manifest_sha256,
        "terra_verdict_output_sha256": terra_stage.verdict_output_sha256,
        "base_case_ids": sorted(sol_base_ids),
        "public_case_ids": list(public_case_ids),
        "assignment_sha256": judge_stage_assignment_sha256(
            "sol_release",
            bundle_hash,
            public_case_ids,
        ),
        "terra_objection_base_case_ids": sorted(objections),
        "terra_accepted_sample_base_case_ids": list(sampled),
    }


def _build_decisions(
    base_case_ids: Sequence[str],
    *,
    terra: Mapping[str, _StageDecision],
    sol: Mapping[str, _StageDecision],
    tie: Mapping[str, _StageDecision],
    tie_required_bases: set[str],
    judge_disagreement_bases: set[str],
    deterministic: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> dict[str, JudgeCaseDecision]:
    decisions: dict[str, JudgeCaseDecision] = {}
    for base_case_id in base_case_ids:
        terra_stage = terra.get(base_case_id)
        sol_stage = sol.get(base_case_id)
        if base_case_id in tie_required_bases:
            final_stage = tie[base_case_id]
            score_maps = (final_stage.rubric_scores,)
            position_conflict = bool(
                (terra_stage and terra_stage.position_conflict) or (sol_stage and sol_stage.position_conflict)
            )
        else:
            available = tuple(stage for stage in (terra_stage, sol_stage) if stage is not None)
            if not available:
                raise AssertionError("every materialized base case requires at least one judge stage")
            final_stage = available[0]
            score_maps = tuple(stage.rubric_scores for stage in available)
            position_conflict = any(stage.position_conflict for stage in available)
        if final_stage.candidate_label is None and not final_stage.candidates_identical:
            raise AssertionError("a final judge stage must select one internal candidate label")
        label = final_stage.candidate_label
        if final_stage.candidates_identical:
            shared_deterministic_flags = set(deterministic[base_case_id].values())
            if len(shared_deterministic_flags) != 1:
                raise AssertionError("identical candidate outputs must have identical deterministic flags")
            deterministic_flags = next(iter(shared_deterministic_flags))
        else:
            if label is None:
                raise AssertionError("a non-equivalent final judge stage requires one candidate label")
            deterministic_flags = deterministic[base_case_id][label]
        if base_case_id in tie_required_bases:
            judge_acceptable = final_stage.acceptable
            judge_flags = final_stage.critical_flags
        else:
            relevant = tuple(
                stage
                for stage in (terra_stage, sol_stage)
                if stage is not None and (final_stage.candidates_identical or stage.candidate_label == label)
            )
            if len(relevant) != len(tuple(stage for stage in (terra_stage, sol_stage) if stage)):
                raise AssertionError("unresolved judge disagreement escaped the tie-break set")
            judge_acceptable = all(stage.acceptable for stage in relevant)
            judge_flags = tuple(sorted({flag for stage in relevant for flag in stage.critical_flags}))
        rubric_scores = _average_score_maps(*score_maps)
        reasons = [f"deterministic:{flag}" for flag in deterministic_flags]
        reasons.extend(f"judge:{flag}" for flag in judge_flags)
        if not judge_acceptable and not judge_flags:
            reasons.append("judge_marked_unacceptable")
        decisions[base_case_id] = JudgeCaseDecision(
            base_case_id=base_case_id,
            candidate_label=label,
            candidates_identical=final_stage.candidates_identical,
            acceptable=judge_acceptable and not deterministic_flags and not judge_flags,
            deterministic_critical_flags=deterministic_flags,
            judge_critical_flags=judge_flags,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            position_conflict=position_conflict,
            judge_disagreement=base_case_id in judge_disagreement_bases,
            tie_break_used=base_case_id in tie_required_bases,
            rubric_scores=rubric_scores,
        )
    return dict(sorted(decisions.items()))


def _candidate_summaries(
    labels: tuple[str, ...],
    decisions: Mapping[str, JudgeCaseDecision],
    deterministic: Mapping[str, Mapping[str, tuple[str, ...]]],
    *,
    total_cases: int,
) -> dict[str, JudgeCandidateSummary]:
    summaries: dict[str, JudgeCandidateSummary] = {}
    for label in labels:
        wins = tuple(decision for decision in decisions.values() if decision.candidate_label == label)
        equivalents = tuple(decision for decision in decisions.values() if decision.candidates_identical)
        observations = (*wins, *equivalents)
        accepted = tuple(decision for decision in observations if decision.acceptable)
        rubric_averages: dict[str, float | None] = {}
        for dimension in JUDGE_SCORE_DIMENSIONS:
            values = [decision.rubric_scores[dimension] for decision in observations]
            rubric_averages[dimension] = sum(values) / len(values) if values else None
        summaries[label] = JudgeCandidateSummary(
            candidate_label=label,
            total_cases=total_cases,
            win_count=len(wins),
            equivalent_count=len(equivalents),
            win_rate=len(wins) / total_cases,
            acceptable_count=len(accepted),
            acceptable_rate=len(accepted) / total_cases,
            selected_acceptance_rate=(len(accepted) / len(observations) if observations else 0.0),
            deterministic_critical_case_count=sum(
                bool(flags_by_label[label]) for flags_by_label in deterministic.values()
            ),
            judge_critical_case_count=sum(bool(decision.judge_critical_flags) for decision in observations),
            rubric_observation_count=len(observations),
            rubric_averages=rubric_averages,
        )
    return summaries


def _stratum_summaries(
    labels: tuple[str, ...],
    decisions: Mapping[str, JudgeCaseDecision],
    metadata: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for split in sorted({item["split"] for item in metadata.values()}):
        base_ids = {base_case_id for base_case_id, item in metadata.items() if item["split"] == split}
        per_candidate: dict[str, dict[str, Any]] = {}
        for label in labels:
            wins = [
                decision
                for base_case_id, decision in decisions.items()
                if base_case_id in base_ids and (decision.candidate_label == label or decision.candidates_identical)
            ]
            acceptable_count = sum(decision.acceptable for decision in wins)
            per_candidate[label] = {
                "total_cases": len(base_ids),
                "decided_cases": sum(base_case_id in decisions for base_case_id in base_ids),
                "win_count": sum(decision.candidate_label == label for decision in wins),
                "equivalent_count": sum(decision.candidates_identical for decision in wins),
                "acceptable_count": acceptable_count,
                "acceptable_rate": (acceptable_count / len(base_ids) if base_ids else None),
            }
        result[split] = per_candidate
    return result


def _terra_stage_summaries(
    labels: tuple[str, ...],
    terra: Mapping[str, _StageDecision],
    metadata: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    strata = {
        "regular": {base_case_id for base_case_id in terra if metadata[base_case_id]["split"] == "test"},
        "challenge": {base_case_id for base_case_id in terra if metadata[base_case_id]["split"] == "challenge"},
    }
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for stratum_id, base_case_ids in strata.items():
        per_candidate: dict[str, dict[str, Any]] = {}
        for label in labels:
            selected = [
                terra[base_case_id]
                for base_case_id in base_case_ids
                if terra[base_case_id].candidate_label == label or terra[base_case_id].candidates_identical
            ]
            acceptable_count = sum(decision.acceptable and not decision.critical_flags for decision in selected)
            per_candidate[label] = {
                "total_cases": len(base_case_ids),
                "selected_count": sum(decision.candidate_label == label for decision in selected),
                "equivalent_count": sum(decision.candidates_identical for decision in selected),
                "acceptable_count": acceptable_count,
                "acceptable_rate": (acceptable_count / len(base_case_ids) if base_case_ids else None),
            }
        result[stratum_id] = per_candidate
    return result


def _release_gate(
    summary: JudgeCandidateSummary,
    stratum_summaries: Mapping[str, Mapping[str, Mapping[str, Any]]],
    terra_stage_summaries: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    acceptable_rate_minimums: Mapping[str, float],
    rubric_minimums: Mapping[str, float],
) -> tuple[dict[str, dict[str, Any]], bool]:
    checks: dict[str, dict[str, Any]] = {}

    def add_check(
        check_id: str,
        actual: float | int | None,
        threshold: float | int,
        operator: str,
    ) -> None:
        if actual is None:
            passed = False
            reason = "not_evaluated"
        else:
            passed = actual >= threshold if operator == "gte" else actual <= threshold
            reason = None if passed else "threshold_not_met"
        checks[check_id] = {
            "id": check_id,
            "actual": actual,
            "operator": operator,
            "threshold": threshold,
            "passed": passed,
            "reason": reason,
        }

    threshold_by_split = {
        "test": acceptable_rate_minimums["test"],
        "ai_gold_test": acceptable_rate_minimums["test"],
        "challenge": acceptable_rate_minimums["challenge"],
        "ai_gold_challenge": acceptable_rate_minimums["challenge"],
    }
    for split, threshold in threshold_by_split.items():
        split_summary = stratum_summaries.get(split, {}).get(summary.candidate_label)
        add_check(
            f"acceptable_rate:{split}",
            split_summary["acceptable_rate"] if split_summary else None,
            threshold,
            "gte",
        )
    for stratum_id, threshold_key in (
        ("regular", "test"),
        ("challenge", "challenge"),
    ):
        terra_summary = terra_stage_summaries.get(stratum_id, {}).get(summary.candidate_label)
        add_check(
            f"terra_acceptable_rate:{stratum_id}",
            terra_summary["acceptable_rate"] if terra_summary else None,
            acceptable_rate_minimums[threshold_key],
            "gte",
        )
    add_check(
        "deterministic_critical_case_count",
        summary.deterministic_critical_case_count,
        0,
        "lte",
    )
    add_check("judge_critical_case_count", summary.judge_critical_case_count, 0, "lte")
    for dimension, threshold in rubric_minimums.items():
        add_check(
            f"rubric:{dimension}",
            summary.rubric_averages.get(dimension),
            threshold,
            "gte",
        )
    return checks, all(check["passed"] for check in checks.values())


def _candidate_labels(private_mapping: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    labels = {
        str(mapping[key]) for mapping in private_mapping.values() for key in ("candidate_a_label", "candidate_b_label")
    }
    if len(labels) != 2:
        raise ValueError("private judge mapping must contain exactly two candidate labels")
    return tuple(sorted(labels))


def aggregate_judge_verdicts(
    cases: Sequence[Mapping[str, Any]],
    private_mapping: Mapping[str, Mapping[str, Any]],
    terra_verdicts: Sequence[Mapping[str, Any]],
    sol_verdicts: Sequence[Mapping[str, Any]],
    *,
    materialization_plan: Mapping[str, Any],
    terra_output_manifest: Mapping[str, Any],
    sol_output_manifest: Mapping[str, Any],
    comparison_kind: str,
    tie_break_verdicts: Sequence[Mapping[str, Any]] | None = None,
    tie_break_output_manifest: Mapping[str, Any] | None = None,
    release_candidate: str | None = None,
    acceptable_rate_minimums: Mapping[str, float] | None = None,
    rubric_minimums: Mapping[str, float] | None = None,
) -> JudgeAggregationResult:
    """Require exact materialized stages and never override deterministic flags."""

    if comparison_kind not in JUDGE_COMPARISON_KINDS:
        raise ValueError(f"unsupported pairwise comparison kind: {comparison_kind}")
    if (acceptable_rate_minimums is None) != (rubric_minimums is None):
        raise ValueError("judge release thresholds must be supplied together")
    if acceptable_rate_minimums is not None:
        if set(acceptable_rate_minimums) != {"test", "challenge"}:
            raise ValueError("judge acceptable-rate minimums require test and challenge")
        if any(
            isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1
            for value in acceptable_rate_minimums.values()
        ):
            raise ValueError("judge acceptable-rate minimums must be between zero and one")
    if rubric_minimums is not None:
        unknown_dimensions = sorted(set(rubric_minimums).difference(JUDGE_SCORE_DIMENSIONS))
        if unknown_dimensions:
            raise ValueError(f"unknown judge rubric threshold: {unknown_dimensions[0]}")

    validated_cases, groups = _validate_bundle_and_mapping(cases, private_mapping)
    cases_by_id = {case["case_id"]: case for case in validated_cases}
    bundle_hash = judge_bundle_sha256(validated_cases)
    plan, plan_hash = _validate_materialization(
        materialization_plan,
        bundle_sha256=bundle_hash,
        public_case_count=len(validated_cases),
        groups=groups,
        cases_by_id=cases_by_id,
    )
    labels = _candidate_labels(private_mapping)

    terra_assignment = plan["stage_assignments"]["terra_prejudge"]
    terra_stage = _validate_stage(
        terra_verdicts,
        terra_output_manifest,
        expected_case_ids=set(terra_assignment["public_case_ids"]),
        expected_stage="terra_prejudge",
        expected_bundle_sha256=bundle_hash,
        expected_assignment_sha256=terra_assignment["assignment_sha256"],
    )
    terra_groups = {base_case_id: groups[base_case_id] for base_case_id in terra_assignment["base_case_ids"]}
    terra = _stage_decisions(terra_stage.by_id, terra_groups, private_mapping)

    sol_base_ids, terra_objections, terra_accepted_sample = _derive_sol_release_base_ids(
        plan,
        terra,
    )
    sol_public_ids = _public_ids_for_bases(sorted(sol_base_ids), groups)
    sol_assignment_hash = judge_stage_assignment_sha256(
        "sol_release",
        bundle_hash,
        sol_public_ids,
    )
    sol_stage = _validate_stage(
        sol_verdicts,
        sol_output_manifest,
        expected_case_ids=set(sol_public_ids),
        expected_stage="sol_release",
        expected_bundle_sha256=bundle_hash,
        expected_assignment_sha256=sol_assignment_hash,
    )
    sol_groups = {base_case_id: groups[base_case_id] for base_case_id in sorted(sol_base_ids)}
    sol = _stage_decisions(sol_stage.by_id, sol_groups, private_mapping)

    if terra_stage.judge_id == sol_stage.judge_id:
        raise ValueError("Terra and Sol stages require independent judge identities")
    if terra_stage.judge_session_id == sol_stage.judge_session_id:
        raise ValueError("Terra and Sol stages require independent judge sessions")
    if {terra_stage.judge_id, sol_stage.judge_id}.intersection(labels):
        raise ValueError("a candidate identity cannot decide its own judge stage")

    overlapping_bases = set(terra).intersection(sol)
    judge_disagreement_bases = {
        base_case_id
        for base_case_id in overlapping_bases
        if terra[base_case_id].candidate_label != sol[base_case_id].candidate_label
        or terra[base_case_id].acceptable != sol[base_case_id].acceptable
        or terra[base_case_id].critical_flags != sol[base_case_id].critical_flags
    }
    position_conflict_bases = {
        base_case_id
        for base_case_id in set(terra) | set(sol)
        if bool(
            (terra.get(base_case_id) and terra[base_case_id].position_conflict)
            or (sol.get(base_case_id) and sol[base_case_id].position_conflict)
        )
    }
    judge_critical_bases = {
        base_case_id
        for base_case_id in set(terra) | set(sol)
        if bool(
            (terra.get(base_case_id) and terra[base_case_id].critical_flags)
            or (sol.get(base_case_id) and sol[base_case_id].critical_flags)
        )
    }
    tie_static_bases = set(plan["stage_assignments"]["sol_tiebreak_static"]["base_case_ids"])
    tie_required_bases = tie_static_bases | judge_disagreement_bases | position_conflict_bases | judge_critical_bases
    required_tie_ids = _public_ids_for_bases(sorted(tie_required_bases), groups)

    tie: dict[str, _StageDecision] = {}
    tie_stage: _ValidatedStage | None = None
    missing_tie_ids = required_tie_ids
    if required_tie_ids:
        if (tie_break_verdicts is None) != (tie_break_output_manifest is None):
            raise ValueError("tie-break verdicts and output manifest must be supplied together")
        if tie_break_verdicts is not None and tie_break_output_manifest is not None:
            tie_assignment_hash = judge_stage_assignment_sha256(
                "sol_tiebreak",
                bundle_hash,
                required_tie_ids,
            )
            tie_stage = _validate_stage(
                tie_break_verdicts,
                tie_break_output_manifest,
                expected_case_ids=set(required_tie_ids),
                expected_stage="sol_tiebreak",
                expected_bundle_sha256=bundle_hash,
                expected_assignment_sha256=tie_assignment_hash,
                allow_missing=True,
            )
            if tie_stage.judge_id in {terra_stage.judge_id, sol_stage.judge_id}:
                raise ValueError("tie-break requires an independent judge identity")
            if tie_stage.judge_session_id in {
                terra_stage.judge_session_id,
                sol_stage.judge_session_id,
            }:
                raise ValueError("tie-break requires an independent judge session")
            if tie_stage.judge_id in labels:
                raise ValueError("a candidate identity cannot decide its own tie-break")
            missing_tie_ids = tie_stage.missing_case_ids
            if not missing_tie_ids:
                tie_groups = {base_case_id: groups[base_case_id] for base_case_id in tie_required_bases}
                tie = _stage_decisions(tie_stage.by_id, tie_groups, private_mapping)
                unresolved = sorted(
                    base_case_id for base_case_id, decision in tie.items() if decision.position_conflict
                )
                if unresolved:
                    raise ValueError(f"tie-break has an unresolved A/B position conflict: {unresolved[0]}")
    elif tie_break_verdicts is not None or tie_break_output_manifest is not None:
        raise ValueError("tie-break evidence was supplied without a required tie-break")

    deterministic = _deterministic_flags_by_label(
        cases_by_id,
        groups,
        private_mapping,
    )
    status = "pending_tiebreak" if missing_tie_ids else "complete"
    decidable_base_ids = (
        tuple(sorted(set(groups).difference(tie_required_bases)))
        if status == "pending_tiebreak"
        else tuple(sorted(groups))
    )
    decisions = _build_decisions(
        decidable_base_ids,
        terra=terra,
        sol=sol,
        tie=tie,
        tie_required_bases=tie_required_bases,
        judge_disagreement_bases=judge_disagreement_bases,
        deterministic=deterministic,
    )
    summaries = _candidate_summaries(
        labels,
        decisions,
        deterministic,
        total_cases=len(groups),
    )
    stratum_summaries = _stratum_summaries(
        labels,
        decisions,
        plan["base_case_metadata"],
    )
    terra_stage_summaries = _terra_stage_summaries(
        labels,
        terra,
        plan["base_case_metadata"],
    )
    if status == "pending_tiebreak":
        winning_labels: tuple[str, ...] = ()
    else:
        maximum_wins = max(summary.win_count for summary in summaries.values())
        winning_labels = tuple(label for label, summary in summaries.items() if summary.win_count == maximum_wins)
    rubric_averages = {
        dimension: (
            sum(decision.rubric_scores[dimension] for decision in decisions.values()) / len(decisions)
            if decisions
            else None
        )
        for dimension in JUDGE_SCORE_DIMENSIONS
    }
    if release_candidate is not None and release_candidate not in summaries:
        raise ValueError(f"release candidate is absent from private mapping: {release_candidate}")
    release_gate: dict[str, dict[str, Any]] = {}
    comparison_gate_passed: bool | None = None
    if release_candidate is not None:
        if acceptable_rate_minimums is None or rubric_minimums is None:
            raise ValueError("release candidate requires judge release thresholds")
        release_gate, gate_checks_passed = _release_gate(
            summaries[release_candidate],
            stratum_summaries,
            terra_stage_summaries,
            acceptable_rate_minimums=acceptable_rate_minimums,
            rubric_minimums=rubric_minimums,
        )
        comparison_gate_passed = status == "complete" and gate_checks_passed

    accepted_count = sum(decision.acceptable for decision in decisions.values())
    provided_tie_count = len(tie_stage.by_id) if tie_stage is not None else 0
    terra_accepted_bases = set(terra).difference(terra_objections)
    terra_accepted_reviewed_bases = terra_accepted_bases.intersection(sol_base_ids)
    terra_accepted_review_fraction = (
        len(terra_accepted_reviewed_bases) / len(terra_accepted_bases) if terra_accepted_bases else 1.0
    )
    if (
        terra_accepted_bases
        and terra_accepted_review_fraction < plan["sol_release_dynamic_policy"]["terra_accepted_sample_fraction"]
    ):
        raise AssertionError("Sol release assignment reviewed less than 25% of Terra-accepted cases")
    stage_coverage = {
        "terra_prejudge": {
            "required_public_case_count": len(terra_assignment["public_case_ids"]),
            "provided_public_case_count": len(terra_stage.by_id),
            "exact": True,
        },
        "sol_release": {
            "static_base_case_count": len(plan["stage_assignments"]["sol_release_static"]["base_case_ids"]),
            "terra_objection_base_case_count": len(terra_objections),
            "terra_accepted_base_case_count": len(terra) - len(terra_objections),
            "terra_accepted_sample_base_case_count": len(terra_accepted_sample),
            "terra_accepted_reviewed_base_case_count": len(terra_accepted_reviewed_bases),
            "terra_accepted_review_fraction": terra_accepted_review_fraction,
            "terra_accepted_sample_fraction_minimum": plan["sol_release_dynamic_policy"][
                "terra_accepted_sample_fraction"
            ],
            "required_public_case_count": len(sol_public_ids),
            "provided_public_case_count": len(sol_stage.by_id),
            "exact": True,
        },
        "sol_tiebreak": {
            "static_base_case_count": len(tie_static_bases),
            "judge_disagreement_base_case_count": len(judge_disagreement_bases),
            "position_conflict_base_case_count": len(position_conflict_bases),
            "judge_critical_base_case_count": len(judge_critical_bases),
            "required_public_case_count": len(required_tie_ids),
            "provided_public_case_count": provided_tie_count,
            "exact": not missing_tie_ids,
        },
    }

    judge_ids = {
        "terra_prejudge": terra_stage.judge_id,
        "sol_release": sol_stage.judge_id,
    }
    judge_session_ids = {
        "terra_prejudge": terra_stage.judge_session_id,
        "sol_release": sol_stage.judge_session_id,
    }
    prompt_hashes = {
        "terra_prejudge": terra_stage.prompt_hash,
        "sol_release": sol_stage.prompt_hash,
    }
    assignment_hashes = {
        "terra_prejudge": terra_stage.assignment_sha256,
        "sol_release": sol_stage.assignment_sha256,
    }
    if required_tie_ids:
        assignment_hashes["sol_tiebreak"] = judge_stage_assignment_sha256(
            "sol_tiebreak",
            bundle_hash,
            required_tie_ids,
        )
    output_hashes = {
        "terra_prejudge": terra_stage.verdict_output_sha256,
        "sol_release": sol_stage.verdict_output_sha256,
    }
    manifest_hashes = {
        "terra_prejudge": terra_stage.manifest_sha256,
        "sol_release": sol_stage.manifest_sha256,
    }
    if tie_stage is not None:
        judge_ids["sol_tiebreak"] = tie_stage.judge_id
        judge_session_ids["sol_tiebreak"] = tie_stage.judge_session_id
        prompt_hashes["sol_tiebreak"] = tie_stage.prompt_hash
        if assignment_hashes["sol_tiebreak"] != tie_stage.assignment_sha256:
            raise AssertionError("validated tie-break assignment hash changed")
        output_hashes["sol_tiebreak"] = tie_stage.verdict_output_sha256
        manifest_hashes["sol_tiebreak"] = tie_stage.manifest_sha256

    return JudgeAggregationResult(
        status=status,
        evaluation_split="combined",
        comparison_kind=comparison_kind,
        bundle_sha256=bundle_hash,
        materialization_plan_sha256=plan_hash,
        coverage_policy_sha256=plan["coverage_policy_sha256"],
        decisions=decisions,
        required_sol_release_case_ids=sol_public_ids,
        required_tie_break_case_ids=required_tie_ids,
        missing_tie_break_case_ids=missing_tie_ids,
        pending_case_count=len(groups) - len(decisions),
        accepted_count=accepted_count,
        rejected_count=len(decisions) - accepted_count,
        acceptance_rate=accepted_count / len(decisions) if decisions else 0.0,
        candidate_summaries=summaries,
        stratum_candidate_summaries=stratum_summaries,
        terra_stage_candidate_summaries=terra_stage_summaries,
        winning_candidate_labels=winning_labels,
        rubric_averages=rubric_averages,
        release_candidate=release_candidate,
        release_gate=release_gate,
        comparison_gate_passed=comparison_gate_passed,
        release_gate_passed=False,
        stage_coverage=stage_coverage,
        judge_ids=judge_ids,
        judge_session_ids=judge_session_ids,
        prompt_hashes=prompt_hashes,
        stage_assignment_sha256s=assignment_hashes,
        verdict_output_sha256s=output_hashes,
        stage_output_manifest_sha256s=manifest_hashes,
    )


def aggregate_judge_campaign_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    aggregation_sha256s: Mapping[str, str],
) -> dict[str, Any]:
    """Require both pairwise comparisons before emitting an overall release decision."""

    if len(reports) != len(JUDGE_COMPARISON_KINDS):
        raise ValueError("judge campaign requires exactly two comparison reports")
    reports_by_kind: dict[str, Mapping[str, Any]] = {}
    for report in reports:
        comparison_kind = report.get("comparison_kind")
        if comparison_kind not in JUDGE_COMPARISON_KINDS:
            raise ValueError(f"judge campaign has an unknown comparison kind: {comparison_kind}")
        if comparison_kind in reports_by_kind:
            raise ValueError(f"judge campaign has a duplicate comparison: {comparison_kind}")
        reports_by_kind[comparison_kind] = report
    if set(reports_by_kind) != JUDGE_COMPARISON_KINDS:
        missing = sorted(JUDGE_COMPARISON_KINDS.difference(reports_by_kind))
        raise ValueError(f"judge campaign is missing comparison: {missing[0]}")
    if set(aggregation_sha256s) != JUDGE_COMPARISON_KINDS:
        raise ValueError("judge campaign hashes must cover both comparison reports")
    for comparison_kind, digest in aggregation_sha256s.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"judge campaign has an invalid aggregation SHA-256: {comparison_kind}")

    common_fields = (
        "release_candidate",
        "release_candidate_prediction_sha256",
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "evaluation_config_sha256",
    )
    common: dict[str, Any] = {}
    for field in common_fields:
        values = {report.get(field) for report in reports}
        if len(values) != 1:
            raise ValueError(f"judge campaign comparison reports differ on {field}")
        common[field] = next(iter(values))
    if not isinstance(common["release_candidate"], str) or not common["release_candidate"]:
        raise ValueError("judge campaign requires one non-empty release candidate")
    for field in common_fields[1:]:
        digest = common[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"judge campaign has an invalid common hash: {field}")

    comparison_evidence: dict[str, dict[str, Any]] = {}
    all_sessions: list[str] = []
    comparison_gates_passed = True
    zero_critical = True
    for comparison_kind in sorted(JUDGE_COMPARISON_KINDS):
        report = reports_by_kind[comparison_kind]
        if report.get("status") != "complete":
            raise ValueError(f"judge campaign comparison is incomplete: {comparison_kind}")
        if report.get("release_gate_passed") is not False:
            raise ValueError(f"single comparison must not self-promote to release: {comparison_kind}")
        if report.get("materialized_mandatory_coverage") is not True:
            raise ValueError(f"judge campaign comparison lacks materialized coverage: {comparison_kind}")
        if report.get("deterministic_hard_errors_overridable") is not False:
            raise ValueError(f"judge campaign comparison weakens deterministic errors: {comparison_kind}")
        stage_coverage = report.get("stage_coverage")
        if (
            report.get("exact_required_stage_coverage") is not True
            or not isinstance(stage_coverage, Mapping)
            or not stage_coverage
            or not all(isinstance(stage, Mapping) and stage.get("exact") is True for stage in stage_coverage.values())
        ):
            raise ValueError(f"judge campaign comparison lacks exact stage coverage: {comparison_kind}")
        for binding_field in (
            "stage_output_manifest_sha256s",
            "verdict_output_sha256s",
            "stage_assignment_sha256s",
        ):
            bindings = report.get(binding_field)
            if (
                not isinstance(bindings, Mapping)
                or not {"terra_prejudge", "sol_release"}.issubset(bindings)
                or any(
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    for digest in bindings.values()
                )
            ):
                raise ValueError(f"judge campaign comparison lacks exact {binding_field}: {comparison_kind}")
        sessions = report.get("judge_session_ids")
        if (
            not isinstance(sessions, Mapping)
            or not {"terra_prejudge", "sol_release"}.issubset(sessions)
            or not all(isinstance(session, str) and session for session in sessions.values())
        ):
            raise ValueError(f"judge campaign comparison lacks judge sessions: {comparison_kind}")
        all_sessions.extend(sessions.values())

        release_candidate = common["release_candidate"]
        candidate_summaries = report.get("candidate_summaries")
        if not isinstance(candidate_summaries, Mapping) or not isinstance(
            candidate_summaries.get(release_candidate), Mapping
        ):
            raise ValueError(f"judge campaign lacks release-candidate summary: {comparison_kind}")
        candidate_summary = candidate_summaries[release_candidate]
        deterministic_critical_count = candidate_summary.get("deterministic_critical_case_count")
        judge_critical_count = candidate_summary.get("judge_critical_case_count")
        if (
            not isinstance(deterministic_critical_count, int)
            or isinstance(deterministic_critical_count, bool)
            or deterministic_critical_count < 0
            or not isinstance(judge_critical_count, int)
            or isinstance(judge_critical_count, bool)
            or judge_critical_count < 0
        ):
            raise ValueError(f"judge campaign has invalid critical counts: {comparison_kind}")
        zero_critical = zero_critical and deterministic_critical_count == 0 and judge_critical_count == 0
        comparison_gate_passed = report.get("comparison_gate_passed") is True
        comparison_gates_passed = comparison_gates_passed and comparison_gate_passed
        expected_opponent_role = {
            "fine_tune_vs_base": "base_model",
            "fine_tune_vs_rule_baseline": "rule_baseline",
        }[comparison_kind]
        if report.get("opponent_role") != expected_opponent_role:
            raise ValueError(f"judge campaign comparison has the wrong opponent role: {comparison_kind}")
        opponent_prediction_sha256 = report.get("opponent_prediction_sha256")
        if (
            not isinstance(opponent_prediction_sha256, str)
            or len(opponent_prediction_sha256) != 64
            or any(character not in "0123456789abcdef" for character in opponent_prediction_sha256)
        ):
            raise ValueError(f"judge campaign comparison has an invalid opponent prediction hash: {comparison_kind}")
        bundle_hash = report.get("bundle_sha256")
        plan_hash = report.get("materialization_plan_sha256")
        for field, digest in (
            ("bundle_sha256", bundle_hash),
            ("materialization_plan_sha256", plan_hash),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"judge campaign comparison has an invalid {field}: {comparison_kind}")
        comparison_evidence[comparison_kind] = {
            "aggregation_sha256": aggregation_sha256s[comparison_kind],
            "bundle_sha256": bundle_hash,
            "materialization_plan_sha256": plan_hash,
            "status": "complete",
            "exact_required_stage_coverage": True,
            "comparison_gate_passed": comparison_gate_passed,
            "release_gate_passed": False,
            "deterministic_critical_case_count": deterministic_critical_count,
            "judge_critical_case_count": judge_critical_count,
            "opponent_role": expected_opponent_role,
            "opponent_prediction_sha256": opponent_prediction_sha256,
            "judge_session_ids": dict(sorted(sessions.items())),
        }

    independent_sessions = len(all_sessions) == len(set(all_sessions))
    release_gate_passed = comparison_gates_passed and zero_critical and independent_sessions
    result = {
        "schema_version": 1,
        "status": "complete" if release_gate_passed else "blocked",
        **common,
        "required_comparison_kinds": sorted(JUDGE_COMPARISON_KINDS),
        "observed_comparison_kinds": sorted(reports_by_kind),
        "missing_comparison_kinds": [],
        "comparisons": comparison_evidence,
        "independent_sessions": independent_sessions,
        "zero_critical_requirements_passed": zero_critical,
        "release_gate_passed": release_gate_passed,
    }
    return validate_judge_campaign_report(result)

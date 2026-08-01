"""Deterministic pairwise blinding with private candidate-label recovery."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_from_dict
from .evaluation_io import (
    join_evaluation_cases,
    validate_candidate_prediction,
    validate_evaluation_reference,
)
from .metrics import evaluate_predictions
from .schemas import validate_semantic_plan
from .target_renderer import render_plain_text

_PROJECT_ROOT = Path(__file__).parents[2]
_BUNDLE_CASE_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_bundle_case_schema.json"
_BUNDLE_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_bundle_manifest_schema.json"
_STAGE_PACKET_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_stage_packet_manifest_schema.json"
_STAGE_RESUME_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_stage_resume_manifest_schema.json"
_COVERAGE_POLICY_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_coverage_policy_schema.json"
_MATERIALIZATION_PLAN_SCHEMA = _PROJECT_ROOT / "contracts" / "judge_materialization_plan_schema.json"
_MANDATORY_COVERAGE_POLICY = _PROJECT_ROOT / "contracts" / "mandatory_coverage_quotas_v1.json"

JUDGE_COMPARISON_KINDS = frozenset(
    {
        "fine_tune_vs_base",
        "fine_tune_vs_rule_baseline",
    }
)

JUDGE_SCORE_DIMENSIONS = (
    "semantic_fidelity",
    "no_additions",
    "no_omissions",
    "spelling",
    "grammar",
    "punctuation",
    "paragraph_structure",
    "heading_structure",
    "numbered_lists",
    "disfluency_cleanup",
    "unit_normalization",
    "legal_citations",
    "address_pronouns",
    "formatting",
    "style_preservation",
)

JUDGE_CRITICAL_FLAGS = frozenset(
    {
        "changed_fact",
        "changed_negation",
        "changed_condition",
        "changed_modality",
        "changed_number",
        "changed_amount",
        "changed_percentage",
        "changed_date_or_time",
        "changed_unit_dimension",
        "changed_name",
        "changed_company_name",
        "changed_technical_term",
        "changed_case_identifier",
        "changed_contact_detail",
        "changed_language",
        "changed_speaker_label",
        "changed_timestamp",
        "changed_legal_reference",
        "changed_legal_hierarchy",
        "invented_law_abbreviation",
        "omitted_content",
        "added_content",
        "answered_question",
        "summarized_content",
        "reordered_content",
        "damaged_protected_span",
        "invalid_sst",
        "invented_heading",
        "missed_heading",
        "wrong_heading_level",
        "wrong_heading_style",
        "false_subject",
        "missed_subject",
        "paragraph_oversegmentation",
        "paragraph_undersegmentation",
        "missing_blank_line",
        "excessive_blank_lines",
        "salutation_layout_error",
        "closing_layout_error",
        "signature_layout_error",
        "wrong_list_type",
        "malformed_numbered_list",
        "wrong_list_nesting",
        "formatting_command_leaked",
        "formatting_command_misinterpreted",
        "false_unit_normalization",
        "wrong_address_pronoun",
        "false_variant_correction",
        "output_not_clean_text",
        "unsafe_renderer_output",
    }
)

_RUBRIC = {
    "dimensions": list(JUDGE_SCORE_DIMENSIONS),
    "score_minimum": 1,
    "score_maximum": 5,
    "hard_deterministic_errors_are_binding": True,
}


@dataclass(frozen=True, slots=True)
class BlindedJudgeBundle:
    cases: tuple[dict[str, Any], ...]
    private_mapping: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BlindedJudgeStagePacket:
    cases: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]


@cache
def _bundle_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_BUNDLE_CASE_SCHEMA.read_text(encoding="utf-8")))


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate_closed_record(
    record: Mapping[str, Any],
    *,
    schema_path: Path,
    label: str,
) -> dict[str, Any]:
    value = dict(record)
    errors = sorted(_validator(schema_path).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"{label} schema failed: {errors[0].message}")
    return value


def validate_judge_bundle_case(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the anonymous, closed public transport record."""

    value = dict(record)
    errors = sorted(_bundle_validator().iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"judge bundle case schema failed: {errors[0].message}")
    if tuple(value["rubric"]["dimensions"]) != JUDGE_SCORE_DIMENSIONS:
        raise ValueError("judge bundle rubric dimensions must use the canonical order")
    try:
        reference_document = document_from_dict(value["reference_ast"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"judge bundle reference AST is invalid: {error}") from error
    if render_plain_text(reference_document) != value["reference_text"]:
        raise ValueError("judge bundle reference AST differs from reference text")
    try:
        validate_semantic_plan(value["semantic_plan"])
    except ValueError as error:
        raise ValueError(f"judge bundle semantic plan is invalid: {error}") from error
    for protected in value["protected_spans"]:
        if protected not in value["reference_text"]:
            raise ValueError(f"judge bundle protected span is absent from reference text: {protected!r}")
    return value


def validate_judge_bundle_manifest(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed public manifest, which deliberately contains no labels."""

    return _validate_closed_record(
        record,
        schema_path=_BUNDLE_MANIFEST_SCHEMA,
        label="judge bundle manifest",
    )


def validate_judge_stage_packet_manifest(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a candidate-anonymous, exact stage packet binding."""

    return _validate_closed_record(
        record,
        schema_path=_STAGE_PACKET_MANIFEST_SCHEMA,
        label="judge stage packet manifest",
    )


def validate_judge_stage_resume_manifest(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one closed, hash-bound judge resume journal."""

    return _validate_closed_record(
        record,
        schema_path=_STAGE_RESUME_MANIFEST_SCHEMA,
        label="judge stage resume manifest",
    )


def validate_judge_coverage_policy(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a deterministic coverage policy before selecting any judge cases."""

    value = _validate_closed_record(
        record,
        schema_path=_COVERAGE_POLICY_SCHEMA,
        label="judge coverage policy",
    )
    if set(value["required_comparison_kinds"]) != JUDGE_COMPARISON_KINDS:
        raise ValueError("judge coverage policy must require both canonical pairwise comparisons")
    quota_ids = [quota["id"] for quota in value["category_quotas"]]
    if len(quota_ids) != len(set(quota_ids)):
        raise ValueError("judge coverage policy has duplicate category quota IDs")
    return value


def load_mandatory_judge_coverage_policy(
    path: Path = _MANDATORY_COVERAGE_POLICY,
) -> dict[str, Any]:
    """Load the repository-owned judge policy from the combined quota contract."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("mandatory coverage contract must be a JSON object")
    policy = payload.get("judge_evaluation")
    if not isinstance(policy, Mapping):
        raise ValueError("mandatory coverage contract lacks judge_evaluation")
    validated = validate_judge_coverage_policy(policy)
    if validated["regular_test_minimum"] < 1_000:
        raise ValueError("mandatory judge policy must require at least 1,000 regular test cases")
    return validated


def validate_judge_materialization_plan(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed, candidate-anonymous stage materialization plan."""

    return _validate_closed_record(
        record,
        schema_path=_MATERIALIZATION_PLAN_SCHEMA,
        label="judge materialization plan",
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256((_canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def judge_coverage_policy_sha256(policy: Mapping[str, Any]) -> str:
    """Hash the validated semantic policy rather than an ambient path."""

    return _canonical_json_sha256(validate_judge_coverage_policy(policy))


def judge_materialization_plan_sha256(plan: Mapping[str, Any]) -> str:
    """Hash the exact validated stage plan."""

    return _canonical_json_sha256(validate_judge_materialization_plan(plan))


def judge_stage_assignment_sha256(
    judge_stage: str,
    reviewed_bundle_sha256: str,
    public_case_ids: Sequence[str],
) -> str:
    """Bind a stage to the full bundle and one exact, ordered-independent case set."""

    if judge_stage not in {"terra_prejudge", "sol_release", "sol_tiebreak"}:
        raise ValueError(f"unsupported judge stage: {judge_stage}")
    if (
        not isinstance(reviewed_bundle_sha256, str)
        or len(reviewed_bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in reviewed_bundle_sha256)
    ):
        raise ValueError("reviewed bundle SHA-256 must be a lowercase hexadecimal digest")
    ordered = tuple(sorted(public_case_ids))
    if len(ordered) != len(set(ordered)):
        raise ValueError("judge stage assignment contains duplicate public case IDs")
    if any(not isinstance(case_id, str) or not case_id for case_id in ordered):
        raise ValueError("judge stage assignment contains an invalid public case ID")
    return _canonical_json_sha256(
        {
            "schema_version": 1,
            "judge_stage": judge_stage,
            "reviewed_bundle_sha256": reviewed_bundle_sha256,
            "public_case_ids": list(ordered),
        }
    )


def _bundle_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    if not cases:
        raise ValueError("judge bundle must contain at least one case")
    payload = ("\n".join(_canonical_json(case) for case in cases) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _case_ids_sha256(case_ids: Sequence[str]) -> str:
    ordered = tuple(sorted(case_ids))
    if not ordered:
        raise ValueError("judge stage case ID set must not be empty")
    if len(ordered) != len(set(ordered)):
        raise ValueError("judge stage case ID set contains duplicates")
    return _canonical_json_sha256({"case_ids": list(ordered)})


def materialize_judge_stage_packet(
    cases: Sequence[Mapping[str, Any]],
    *,
    judge_stage: str,
    public_case_ids: Sequence[str],
    assignment_public_case_ids: Sequence[str] | None = None,
    assignment_sha256: str,
    prompt_hash: str,
) -> BlindedJudgeStagePacket:
    """Subset one anonymous full bundle into an exact, hash-bound judge stage."""

    validated_cases = tuple(validate_judge_bundle_case(case) for case in cases)
    cases_by_id = {case["case_id"]: case for case in validated_cases}
    if len(cases_by_id) != len(validated_cases):
        raise ValueError("judge source bundle case IDs must be unique")
    packet_ids = tuple(sorted(public_case_ids))
    if not packet_ids:
        raise ValueError("judge stage assignment must not be empty")
    if len(packet_ids) != len(set(packet_ids)):
        raise ValueError("judge stage packet contains duplicate public case IDs")
    assignment_ids = tuple(sorted(packet_ids if assignment_public_case_ids is None else assignment_public_case_ids))
    if not assignment_ids:
        raise ValueError("judge stage assignment must not be empty")
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ValueError("judge stage assignment contains duplicate public case IDs")
    unknown = sorted(set(assignment_ids).difference(cases_by_id))
    if unknown:
        raise ValueError(f"judge stage assignment references an unknown case: {unknown[0]}")
    outside_assignment = sorted(set(packet_ids).difference(assignment_ids))
    if outside_assignment:
        raise ValueError(f"judge stage packet contains a case outside its assignment: {outside_assignment[0]}")

    reviewed_bundle_hash = _bundle_sha256(validated_cases)
    expected_assignment_hash = judge_stage_assignment_sha256(
        judge_stage,
        reviewed_bundle_hash,
        assignment_ids,
    )
    if assignment_sha256 != expected_assignment_hash:
        raise ValueError("judge stage assignment hash differs from exact public case set")
    selected_ids = set(packet_ids)
    selected = tuple(case for case in validated_cases if case["case_id"] in selected_ids)
    if len(selected) != len(packet_ids):
        raise AssertionError("judge stage packet selection lost an assigned case")
    model_family, reasoning_effort = {
        "terra_prejudge": ("gpt-5.6-terra", "max"),
        "sol_release": ("gpt-5.6-sol", "max"),
        "sol_tiebreak": ("gpt-5.6-sol", "ultra"),
    }[judge_stage]
    manifest = validate_judge_stage_packet_manifest(
        {
            "schema_version": 1,
            "judge_stage": judge_stage,
            "model_family": model_family,
            "reasoning_effort": reasoning_effort,
            "prompt_hash": prompt_hash,
            "reviewed_bundle_sha256": reviewed_bundle_hash,
            "stage_assignment_sha256": assignment_sha256,
            "stage_assignment_case_ids_sha256": _case_ids_sha256(assignment_ids),
            "case_ids_sha256": _case_ids_sha256(packet_ids),
            "stage_bundle_sha256": _bundle_sha256(selected),
            "source_public_case_count": len(validated_cases),
            "stage_assignment_public_case_count": len(assignment_ids),
            "assigned_public_case_count": len(selected),
            "contains_candidate_identity": False,
            "contains_model_or_checkpoint_identity": False,
            "contains_training_history": False,
            "contains_automatic_aggregate_scores": False,
            "hard_deterministic_errors_are_binding": True,
        }
    )
    return BlindedJudgeStagePacket(cases=selected, manifest=manifest)


def _blind_bit(blind_seed: int, namespace: str, case_id: str) -> int:
    digest = hashlib.sha256(f"{blind_seed}:{namespace}:{case_id}".encode()).digest()
    return digest[0] & 1


def _public_case_id(
    blind_seed: int,
    base_case_id: str,
    variant: str,
    candidate_a: str,
    candidate_b: str,
) -> str:
    evidence = _canonical_json(
        {
            "blind_seed": blind_seed,
            "base_case_id": base_case_id,
            "variant": variant,
            "candidate_a_sha256": hashlib.sha256(candidate_a.encode()).hexdigest(),
            "candidate_b_sha256": hashlib.sha256(candidate_b.encode()).hexdigest(),
        }
    )
    return "judge_" + hashlib.sha256(evidence.encode()).hexdigest()[:24]


def _critical_by_case(cases) -> dict[str, tuple[str, ...]]:
    report = evaluate_predictions(cases)
    return {
        result.case_id: tuple(flag for flag in result.critical_errors if flag in JUDGE_CRITICAL_FLAGS)
        for result in report.case_results
    }


def build_blinded_judge_bundle(
    references: Sequence[Mapping[str, Any]],
    candidate_x: Sequence[Mapping[str, Any]],
    candidate_y: Sequence[Mapping[str, Any]],
    *,
    candidate_x_label: str,
    candidate_y_label: str,
    blind_seed: int,
    swapped_fraction: float = 0.5,
) -> BlindedJudgeBundle:
    """Build randomized A/B cases and keep every internal label in a separate mapping."""

    if not isinstance(blind_seed, int):
        raise TypeError("blind_seed must be an integer")
    if not candidate_x_label or not candidate_y_label:
        raise ValueError("candidate labels must be non-empty")
    if candidate_x_label == candidate_y_label:
        raise ValueError("candidate labels must be distinct")
    if not 0.5 <= swapped_fraction <= 1.0:
        raise ValueError("swapped_fraction must be at least 0.5 and at most 1.0")

    frozen_references = tuple(validate_evaluation_reference(record) for record in references)
    x_predictions = tuple(validate_candidate_prediction(record) for record in candidate_x)
    y_predictions = tuple(validate_candidate_prediction(record) for record in candidate_y)
    x_cases = join_evaluation_cases(frozen_references, x_predictions)
    y_cases = join_evaluation_cases(frozen_references, y_predictions)
    x_by_id = {case.case_id: case for case in x_cases}
    y_by_id = {case.case_id: case for case in y_cases}
    x_critical = _critical_by_case(x_cases)
    y_critical = _critical_by_case(y_cases)

    duplicate_count = math.ceil(len(frozen_references) * swapped_fraction)
    duplicate_ids = {
        reference["case_id"]
        for reference in sorted(
            frozen_references,
            key=lambda item: hashlib.sha256(f"{blind_seed}:duplicate:{item['case_id']}".encode()).hexdigest(),
        )[:duplicate_count]
    }

    public_cases: list[dict[str, Any]] = []
    private_mapping: dict[str, dict[str, Any]] = {}
    for reference in frozen_references:
        base_case_id = reference["case_id"]
        x_case = x_by_id[base_case_id]
        y_case = y_by_id[base_case_id]
        primary_is_x_first = _blind_bit(blind_seed, "orientation", base_case_id) == 0
        orientations = [("primary", primary_is_x_first)]
        if base_case_id in duplicate_ids:
            orientations.append(("swapped", not primary_is_x_first))

        pair_case_ids: list[str] = []
        pending_mappings: list[dict[str, Any]] = []
        for variant, x_first in orientations:
            if x_first:
                candidate_a, candidate_b = x_case.prediction_sst, y_case.prediction_sst
                flags_a, flags_b = x_critical[base_case_id], y_critical[base_case_id]
                label_a, label_b = candidate_x_label, candidate_y_label
            else:
                candidate_a, candidate_b = y_case.prediction_sst, x_case.prediction_sst
                flags_a, flags_b = y_critical[base_case_id], x_critical[base_case_id]
                label_a, label_b = candidate_y_label, candidate_x_label
            public_case_id = _public_case_id(
                blind_seed,
                base_case_id,
                variant,
                candidate_a,
                candidate_b,
            )
            pair_case_ids.append(public_case_id)
            public_case = validate_judge_bundle_case(
                {
                    "schema_version": 1,
                    "case_id": public_case_id,
                    "source_text": reference["source_text"],
                    "semantic_plan": deepcopy(reference["semantic_plan"]),
                    "protected_spans": list(reference["protected_spans"]),
                    "reference_ast": deepcopy(reference["target_ast"]),
                    "reference_text": reference["target_plain_text"],
                    "candidate_a": candidate_a,
                    "candidate_b": candidate_b,
                    "deterministic_critical_flags": {
                        "A": list(flags_a),
                        "B": list(flags_b),
                    },
                    "rubric": deepcopy(_RUBRIC),
                }
            )
            public_cases.append(public_case)
            pending_mappings.append(
                {
                    "public_case_id": public_case_id,
                    "base_case_id": base_case_id,
                    "position_variant": variant,
                    "candidate_a_label": label_a,
                    "candidate_b_label": label_b,
                    "candidate_outputs_identical": candidate_a == candidate_b,
                }
            )
        for mapping in pending_mappings:
            peers = [case_id for case_id in pair_case_ids if case_id != mapping["public_case_id"]]
            mapping["paired_public_case_id"] = peers[0] if peers else None
            private_mapping[mapping["public_case_id"]] = mapping

    ordered_cases = tuple(
        sorted(
            public_cases,
            key=lambda case: hashlib.sha256(f"{blind_seed}:order:{case['case_id']}".encode()).hexdigest(),
        )
    )
    if len({case["case_id"] for case in ordered_cases}) != len(ordered_cases):
        raise ValueError("blinded judge case IDs must be unique")
    if set(private_mapping) != {case["case_id"] for case in ordered_cases}:
        raise AssertionError("private judge mapping must cover the exact public package")
    return BlindedJudgeBundle(
        cases=ordered_cases,
        private_mapping=dict(sorted(private_mapping.items())),
    )


def _coverage_result(
    *,
    available: int,
    required: int,
    coverage_mode: str,
) -> dict[str, Any]:
    selected = available
    passed = selected == required if coverage_mode == "exact" else selected >= required
    return {
        "available": available,
        "required": required,
        "selected": selected,
        "coverage_mode": coverage_mode,
        "passed": passed,
    }


def _stage_assignment(
    *,
    judge_stage: str,
    bundle_sha256: str,
    base_case_ids: set[str],
    public_ids_by_base: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    ordered_base_ids = sorted(base_case_ids)
    public_case_ids = sorted(
        public_case_id for base_case_id in ordered_base_ids for public_case_id in public_ids_by_base[base_case_id]
    )
    return {
        "base_case_ids": ordered_base_ids,
        "public_case_ids": public_case_ids,
        "assignment_sha256": judge_stage_assignment_sha256(
            judge_stage,
            bundle_sha256,
            public_case_ids,
        ),
    }


def materialize_judge_coverage(
    references: Sequence[Mapping[str, Any]],
    bundle: BlindedJudgeBundle,
    coverage_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize exact strata, quotas, and stage assignments without judge verdicts."""

    policy = validate_judge_coverage_policy(coverage_policy)
    frozen_references = tuple(validate_evaluation_reference(record) for record in references)
    references_by_id = {reference["case_id"]: reference for reference in frozen_references}
    if len(references_by_id) != len(frozen_references):
        raise ValueError("judge materialization references must have unique case IDs")

    cases_by_id = {case["case_id"]: validate_judge_bundle_case(case) for case in bundle.cases}
    if len(cases_by_id) != len(bundle.cases):
        raise ValueError("judge materialization bundle must have unique public case IDs")
    if set(bundle.private_mapping) != set(cases_by_id):
        raise ValueError("judge materialization mapping must cover the exact public bundle")

    public_ids_by_base: dict[str, list[str]] = {}
    for public_case_id, mapping in bundle.private_mapping.items():
        if set(mapping) != {
            "public_case_id",
            "base_case_id",
            "position_variant",
            "candidate_a_label",
            "candidate_b_label",
            "candidate_outputs_identical",
            "paired_public_case_id",
        }:
            raise ValueError(f"private judge mapping has unexpected shape: {public_case_id}")
        if not isinstance(mapping["candidate_outputs_identical"], bool):
            raise ValueError(f"private judge mapping has invalid candidate equivalence: {public_case_id}")
        public_case = cases_by_id[public_case_id]
        if mapping["candidate_outputs_identical"] is not (public_case["candidate_a"] == public_case["candidate_b"]):
            raise ValueError(f"private judge mapping candidate equivalence differs: {public_case_id}")
        if mapping["candidate_outputs_identical"] and (
            public_case["deterministic_critical_flags"]["A"] != public_case["deterministic_critical_flags"]["B"]
        ):
            raise ValueError(f"identical judge outputs have different deterministic flags: {public_case_id}")
        base_case_id = mapping["base_case_id"]
        if base_case_id not in references_by_id:
            raise ValueError(f"private judge mapping references an unknown base case: {base_case_id}")
        public_ids_by_base.setdefault(base_case_id, []).append(public_case_id)
    if set(public_ids_by_base) != set(references_by_id):
        raise ValueError("judge references do not cover the exact private base case set")
    frozen_public_ids_by_base = {
        base_case_id: tuple(sorted(public_case_ids))
        for base_case_id, public_case_ids in sorted(public_ids_by_base.items())
    }

    family_prefix = policy["generator_family_tag_prefix"]
    base_metadata: dict[str, dict[str, Any]] = {}
    for base_case_id, reference in sorted(references_by_id.items()):
        flags: set[str] = set()
        conflicting_automatic_metrics = False
        for public_case_id in frozen_public_ids_by_base[base_case_id]:
            critical = cases_by_id[public_case_id]["deterministic_critical_flags"]
            flags_a = set(critical["A"])
            flags_b = set(critical["B"])
            flags.update(flags_a)
            flags.update(flags_b)
            conflicting_automatic_metrics = conflicting_automatic_metrics or flags_a != flags_b
        tags = tuple(sorted(reference["slice_tags"]))
        tag_set = set(tags)
        automatic_suspicious = bool(flags) or bool(tag_set.intersection({"automatic_suspicious", "automatic_warning"}))
        generator_families = tuple(sorted(tag for tag in tags if tag.startswith(family_prefix)))
        base_metadata[base_case_id] = {
            "split": reference["split"],
            "slice_tags": list(tags),
            "is_identity": reference["is_identity"],
            "deterministic_critical_flags": sorted(flags),
            "automatic_suspicious": automatic_suspicious,
            "deterministic_warning": automatic_suspicious,
            "possible_critical_fact_error": bool(flags),
            "conflicting_automatic_metrics": conflicting_automatic_metrics or "automatic_metric_conflict" in tag_set,
            "generator_families": list(generator_families),
            "coverage_categories": [],
        }

    regular_test = {case_id for case_id, metadata in base_metadata.items() if metadata["split"] == "test"}
    ai_gold_test = {case_id for case_id, metadata in base_metadata.items() if metadata["split"] == "ai_gold_test"}
    ai_gold_challenge = {
        case_id for case_id, metadata in base_metadata.items() if metadata["split"] == "ai_gold_challenge"
    }
    critical_regression = {
        case_id
        for case_id, metadata in base_metadata.items()
        if metadata["split"] == "regression" or "critical_regression" in metadata["slice_tags"]
    }
    identity_cases = {case_id for case_id, metadata in base_metadata.items() if metadata["is_identity"]}
    suspicious = {case_id for case_id, metadata in base_metadata.items() if metadata["automatic_suspicious"]}
    deterministic_warnings = {
        case_id for case_id, metadata in base_metadata.items() if metadata["deterministic_warning"]
    }

    strata = {
        "regular_test": _coverage_result(
            available=len(regular_test),
            required=policy["regular_test_minimum"],
            coverage_mode="minimum",
        ),
        "ai_gold_test": _coverage_result(
            available=len(ai_gold_test),
            required=policy["ai_gold_test_expected_count"],
            coverage_mode="exact",
        ),
        "ai_gold_challenge": _coverage_result(
            available=len(ai_gold_challenge),
            required=policy["ai_gold_challenge_expected_count"],
            coverage_mode="exact",
        ),
        "critical_regression": _coverage_result(
            available=len(critical_regression),
            required=policy["critical_regression_expected_count"],
            coverage_mode="exact",
        ),
        "identity": _coverage_result(
            available=len(identity_cases),
            required=len(identity_cases),
            coverage_mode="all",
        ),
        "automatic_suspicious": _coverage_result(
            available=len(suspicious),
            required=len(suspicious),
            coverage_mode="all",
        ),
        "deterministic_warning": _coverage_result(
            available=len(deterministic_warnings),
            required=len(deterministic_warnings),
            coverage_mode="all",
        ),
    }

    observed_families = sorted(
        {family for metadata in base_metadata.values() for family in metadata["generator_families"]}
    )
    required_families = sorted(policy["required_generator_families"])
    missing_families = sorted(set(required_families).difference(observed_families))
    family_counts = {
        family: sum(family in metadata["generator_families"] for metadata in base_metadata.values())
        for family in sorted(set(observed_families) | set(required_families))
    }

    category_reports: dict[str, dict[str, Any]] = {}
    category_base_ids: set[str] = set()
    for quota in policy["category_quotas"]:
        quota_tags = set(quota["any_slice_tags"])
        matches = {
            case_id
            for case_id, metadata in base_metadata.items()
            if (not quota_tags or quota_tags.intersection(metadata["slice_tags"]))
            and ("is_identity" not in quota or metadata["is_identity"] is quota["is_identity"])
        }
        category_base_ids.update(matches)
        for case_id in matches:
            base_metadata[case_id]["coverage_categories"].append(quota["id"])
        category_reports[quota["id"]] = _coverage_result(
            available=len(matches),
            required=quota["minimum"],
            coverage_mode="minimum",
        )
    for metadata in base_metadata.values():
        metadata["coverage_categories"].sort()

    deficits: list[str] = []
    for stratum_id, result in strata.items():
        if not result["passed"]:
            deficits.append(f"stratum:{stratum_id}:required={result['required']}:available={result['available']}")
    if missing_families:
        deficits.extend(f"generator_family_missing:{family}" for family in missing_families)
    for quota_id, result in category_reports.items():
        if not result["passed"]:
            deficits.append(f"category:{quota_id}:required={result['required']}:available={result['available']}")

    terra_base_ids = {
        case_id for case_id, metadata in base_metadata.items() if metadata["split"] in {"test", "ai_gold_test"}
    }
    terra_base_ids.update(suspicious)
    terra_base_ids.update(category_base_ids)
    terra_base_ids.update(
        case_id
        for case_id, metadata in base_metadata.items()
        if set(metadata["generator_families"]).intersection(required_families)
    )

    sol_static_base_ids = {
        case_id
        for case_id, metadata in base_metadata.items()
        if metadata["split"] in {"challenge", "ai_gold_challenge", "regression"}
    }
    sol_static_base_ids.update(deterministic_warnings)
    sol_static_base_ids.update(critical_regression)

    all_base_ids = set(base_metadata)
    primary_stage_by_base_case = {
        case_id: ("terra_prejudge" if metadata["split"] in {"test", "ai_gold_test"} else "sol_release")
        for case_id, metadata in sorted(base_metadata.items())
    }
    if set(primary_stage_by_base_case) != all_base_ids:
        raise AssertionError("primary judge-stage allocation must cover every base case")

    tie_static_base_ids = {
        case_id
        for case_id, metadata in base_metadata.items()
        if metadata["possible_critical_fact_error"] or metadata["conflicting_automatic_metrics"]
    }
    bundle_hash = _bundle_sha256(bundle.cases)
    plan = {
        "schema_version": 1,
        "bundle_sha256": bundle_hash,
        "coverage_policy_sha256": judge_coverage_policy_sha256(policy),
        "coverage_requirements": {
            "regular_test_minimum": policy["regular_test_minimum"],
            "ai_gold_test_expected_count": policy["ai_gold_test_expected_count"],
            "ai_gold_challenge_expected_count": policy["ai_gold_challenge_expected_count"],
            "critical_regression_expected_count": policy["critical_regression_expected_count"],
            "generator_family_tag_prefix": policy["generator_family_tag_prefix"],
            "required_generator_families": list(policy["required_generator_families"]),
            "category_quotas": deepcopy(policy["category_quotas"]),
        },
        "selection_seed": policy["selection_seed"],
        "base_case_count": len(base_metadata),
        "public_case_count": len(bundle.cases),
        "coverage_passed": not deficits,
        "coverage": {
            "strata": strata,
            "generator_families": {
                "required": required_families,
                "observed": observed_families,
                "missing": missing_families,
                "counts": family_counts,
                "passed": not missing_families,
            },
            "category_quotas": dict(sorted(category_reports.items())),
            "deficits": sorted(deficits),
        },
        "base_case_metadata": base_metadata,
        "primary_stage_by_base_case": primary_stage_by_base_case,
        "stage_assignments": {
            "terra_prejudge": _stage_assignment(
                judge_stage="terra_prejudge",
                bundle_sha256=bundle_hash,
                base_case_ids=terra_base_ids,
                public_ids_by_base=frozen_public_ids_by_base,
            ),
            "sol_release_static": _stage_assignment(
                judge_stage="sol_release",
                bundle_sha256=bundle_hash,
                base_case_ids=sol_static_base_ids,
                public_ids_by_base=frozen_public_ids_by_base,
            ),
            "sol_tiebreak_static": _stage_assignment(
                judge_stage="sol_tiebreak",
                bundle_sha256=bundle_hash,
                base_case_ids=tie_static_base_ids,
                public_ids_by_base=frozen_public_ids_by_base,
            ),
        },
        "sol_release_dynamic_policy": {
            "terra_accepted_sample_fraction": policy["terra_accepted_sample_fraction"],
            "selection_namespace": "sol_release_terra_accepted_sample_v1",
            "include_all_terra_objections": True,
        },
    }
    return validate_judge_materialization_plan(plan)

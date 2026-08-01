"""Fail-closed, diagnostic-only Sol follow-up selection for partial Terra evidence.

This module deliberately materializes only blinded Sol packets.  It does not
create Sol verdicts, aggregate a campaign, evaluate a release gate, or create
training data.  Every selection is bound to canonical source bytes and records
only IDs, tiers, and hashes in its private metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .judge_aggregation import judge_bundle_sha256, judge_verdicts_sha256
from .judge_bundle import (
    JUDGE_SCORE_DIMENSIONS,
    BlindedJudgeStagePacket,
    judge_stage_assignment_sha256,
    materialize_judge_stage_packet,
    validate_judge_bundle_case,
    validate_judge_bundle_manifest,
    validate_judge_materialization_plan,
    validate_judge_stage_resume_manifest,
)
from .schemas import (
    validate_judge_verdict,
    validate_sol_followup_artifact_manifest,
    validate_sol_followup_diagnostic_report,
    validate_sol_followup_selection_manifest,
)

SELECTION_NAMESPACE = "sol_followup_v1"
SELECTION_SEED = 270036
_COMPARISON_KINDS = frozenset({"fine_tune_vs_base", "fine_tune_vs_rule_baseline"})
_OPPONENT_ROLES = {
    "fine_tune_vs_base": "base_model",
    "fine_tune_vs_rule_baseline": "rule_baseline",
}
_PRIVATE_MAPPING_FIELDS = frozenset(
    {
        "schema_version",
        "bundle_sha256",
        "blind_seed",
        "comparison_kind",
        "candidate_labels",
        "candidate_roles",
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "materialization_plan_sha256",
        "candidate_prediction_sha256",
        "cases",
    }
)
_PRIVATE_CASE_FIELDS = frozenset(
    {
        "public_case_id",
        "base_case_id",
        "position_variant",
        "candidate_a_label",
        "candidate_b_label",
        "candidate_outputs_identical",
        "paired_public_case_id",
    }
)
_TIER_IDS = (
    "critical_flag_coverage",
    "extra_critical",
    "noncritical_rejection",
    "position_conflict",
    "clean_low_score",
    "cross_comparison_divergence",
    "clean_control",
)
_MAX_EXTRA_CRITICAL = 48
_MAX_NONCRITICAL_REJECTIONS = 48
_MAX_POSITION_CONFLICTS = 32
_MAX_CROSS_COMPARISON_DIVERGENCES = 32
_MAX_CONTROLS = 64
_MIN_CONTROLS_PER_AVAILABLE_SPLIT = 8


@dataclass(frozen=True, slots=True)
class SolFollowupPackagePaths:
    """Canonical inputs for one incomplete Terra package."""

    comparison_kind: str
    verdicts: Path
    resume_manifest: Path
    bundle: Path
    private_mapping: Path
    bundle_manifest: Path
    materialization_plan: Path


@dataclass(frozen=True, slots=True)
class SolFollowupBaseOutcome:
    """Internal-only, text-free summary of one fully completed base-case pair."""

    base_case_id: str
    public_case_ids: tuple[str, ...]
    selected_fine_tune: tuple[bool, ...]
    acceptable: tuple[bool, ...]
    critical: tuple[bool, ...]
    flags: tuple[tuple[str, ...], ...]
    minimum_average_score: float
    split: str
    is_identity: bool
    coverage_categories: tuple[str, ...]

    @property
    def has_critical_error(self) -> bool:
        return any(self.critical)

    @property
    def is_accepted(self) -> bool:
        return all(self.acceptable) and not self.has_critical_error

    @property
    def is_noncritical_rejection(self) -> bool:
        return not self.is_accepted and not self.has_critical_error

    @property
    def has_position_conflict(self) -> bool:
        return len(set(self.selected_fine_tune)) > 1 or len(set(self.acceptable)) > 1 or len(set(self.flags)) > 1

    @property
    def is_clean_low_score(self) -> bool:
        return self.is_accepted and not self.has_position_conflict and self.minimum_average_score <= 4

    @property
    def is_clean_control(self) -> bool:
        return self.is_accepted and not self.has_position_conflict and self.minimum_average_score > 4

    @property
    def decision_signature(self) -> tuple[object, ...]:
        """Comparable orientation-aware decision state without any label or text."""

        return (
            self.selected_fine_tune,
            self.acceptable,
            self.critical,
            self.flags,
        )


@dataclass(frozen=True, slots=True)
class SolFollowupPackageAudit:
    """Validated source package with private data retained only in memory."""

    comparison_kind: str
    bundle_cases: tuple[dict[str, Any], ...]
    bundle_sha256: str
    base_outcomes: dict[str, SolFollowupBaseOutcome]
    completed_public_case_count: int
    partial_base_case_count: int
    source_hashes: dict[str, str]
    terra_stage_assignment_sha256: str
    terra_stage_packet_sha256: str
    terra_stage_packet_manifest_sha256: str
    terra_prompt_hash: str


@dataclass(frozen=True, slots=True)
class SolFollowupSelection:
    """One deterministic selection and its private, text-free manifest."""

    comparison_kind: str
    public_case_ids: tuple[str, ...]
    selected_base_case_ids: tuple[str, ...]
    selection_manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SolFollowupArtifacts:
    """Canonical artifact payloads ready for an atomic write."""

    selections: tuple[SolFollowupSelection, ...]
    packets: tuple[BlindedJudgeStagePacket, ...]
    diagnostic_report: dict[str, Any]
    artifact_manifest: dict[str, Any]


def canonical_json(value: object) -> str:
    """Serialize diagnostic records in the repository's canonical JSON form."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _canonical_jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        raise ValueError("canonical JSONL output must not be empty")
    return ("\n".join(canonical_json(row) for row in rows) + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prefixed_sha256(payload: bytes) -> str:
    return "sha256:" + _sha256_bytes(payload)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _load_canonical_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if raw != _canonical_json_bytes(value):
        raise ValueError(f"{label} must use canonical JSON bytes")
    return value, raw


def _load_canonical_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not valid UTF-8 JSONL") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise ValueError(f"{label} contains a blank JSONL row at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label} row {line_number} is not valid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} row {line_number} must be a JSON object")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    if raw != _canonical_jsonl_bytes(rows):
        raise ValueError(f"{label} must use canonical JSONL bytes")
    return rows, raw


def _case_ids_sha256(case_ids: Sequence[str]) -> str:
    ordered = tuple(sorted(case_ids))
    if not ordered:
        raise ValueError("case ID set must not be empty")
    if len(ordered) != len(set(ordered)):
        raise ValueError("case ID set contains duplicates")
    return _sha256_bytes(_canonical_json_bytes({"case_ids": list(ordered)}))


def _base_case_ids_sha256(base_case_ids: Sequence[str]) -> str:
    ordered = tuple(sorted(base_case_ids))
    if not ordered:
        raise ValueError("base case ID set must not be empty")
    if len(ordered) != len(set(ordered)):
        raise ValueError("base case ID set contains duplicates")
    return _sha256_bytes(_canonical_json_bytes({"base_case_ids": list(ordered)}))


def _rank(*parts: str) -> str:
    return hashlib.sha256(":".join((str(SELECTION_SEED), SELECTION_NAMESPACE, *parts)).encode("utf-8")).hexdigest()


def _validate_private_mapping(
    payload: Mapping[str, Any],
    *,
    cases_by_id: Mapping[str, Mapping[str, Any]],
    bundle_manifest: Mapping[str, Any],
    comparison_kind: str,
    bundle_sha256: str,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Validate private recovery data but return no labels to any emitted artifact."""

    if set(payload) != _PRIVATE_MAPPING_FIELDS or payload.get("schema_version") != 2:
        raise ValueError("private judge mapping has an unexpected shape or schema version")
    if payload["comparison_kind"] != comparison_kind:
        raise ValueError("private judge mapping comparison differs from the package")
    if payload["bundle_sha256"] != bundle_sha256:
        raise ValueError("private judge mapping is bound to another bundle")
    if not isinstance(payload["blind_seed"], int) or isinstance(payload["blind_seed"], bool):
        raise ValueError("private judge mapping blind seed is invalid")
    for field in (
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "materialization_plan_sha256",
    ):
        if not _is_sha256(payload[field]) or payload[field] != bundle_manifest[field]:
            raise ValueError("private judge mapping provenance differs from the bundle manifest")

    labels = payload["candidate_labels"]
    roles = payload["candidate_roles"]
    if (
        not isinstance(labels, list)
        or len(labels) != 2
        or len(set(labels)) != 2
        or not all(isinstance(label, str) and label for label in labels)
        or not isinstance(roles, dict)
        or set(roles) != set(labels)
        or set(roles.values()) != {"fine_tune", _OPPONENT_ROLES[comparison_kind]}
    ):
        raise ValueError("private judge mapping candidate roles are invalid")
    fine_tune_labels = [label for label, role in roles.items() if role == "fine_tune"]
    if len(fine_tune_labels) != 1:
        raise ValueError("private judge mapping must have one fine-tune label")

    prediction_hashes = payload["candidate_prediction_sha256"]
    if not isinstance(prediction_hashes, dict) or set(prediction_hashes) != set(labels):
        raise ValueError("private judge mapping prediction hashes are invalid")
    if not all(_is_sha256(value) for value in prediction_hashes.values()):
        raise ValueError("private judge mapping prediction hash is invalid")

    raw_cases = payload["cases"]
    if not isinstance(raw_cases, dict) or set(raw_cases) != set(cases_by_id):
        raise ValueError("private judge mapping does not cover the exact bundle")
    private_cases: dict[str, dict[str, Any]] = {}
    for public_case_id, raw_mapping in raw_cases.items():
        if not isinstance(raw_mapping, dict) or set(raw_mapping) != _PRIVATE_CASE_FIELDS:
            raise ValueError("private judge mapping case has an unexpected shape")
        if raw_mapping["public_case_id"] != public_case_id:
            raise ValueError("private judge mapping public case ID differs from its key")
        if not isinstance(raw_mapping["base_case_id"], str) or not raw_mapping["base_case_id"]:
            raise ValueError("private judge mapping base case ID is invalid")
        if raw_mapping["position_variant"] not in {"primary", "swapped"}:
            raise ValueError("private judge mapping position variant is invalid")
        if (
            raw_mapping["candidate_a_label"] not in labels
            or raw_mapping["candidate_b_label"] not in labels
            or raw_mapping["candidate_a_label"] == raw_mapping["candidate_b_label"]
            or not isinstance(raw_mapping["candidate_outputs_identical"], bool)
        ):
            raise ValueError("private judge mapping candidate labels are invalid")
        paired_public_case_id = raw_mapping["paired_public_case_id"]
        if paired_public_case_id is not None and paired_public_case_id not in raw_cases:
            raise ValueError("private judge mapping paired case is absent")
        public_case = cases_by_id[public_case_id]
        if raw_mapping["candidate_outputs_identical"] is not (public_case["candidate_a"] == public_case["candidate_b"]):
            raise ValueError("private judge mapping candidate equivalence differs from the bundle")
        if raw_mapping["candidate_outputs_identical"] and (
            public_case["deterministic_critical_flags"]["A"] != public_case["deterministic_critical_flags"]["B"]
        ):
            raise ValueError("identical public candidates have differing deterministic flags")
        private_cases[public_case_id] = dict(raw_mapping)

    for public_case_id, mapping in private_cases.items():
        paired_public_case_id = mapping["paired_public_case_id"]
        if paired_public_case_id is None:
            continue
        paired = private_cases[paired_public_case_id]
        if paired["paired_public_case_id"] != public_case_id:
            raise ValueError("private judge mapping paired case relation is not symmetric")
        if paired["base_case_id"] != mapping["base_case_id"]:
            raise ValueError("private judge mapping paired case changes its base case")
    return private_cases, fine_tune_labels[0]


def _validate_terra_assignment(
    plan: Mapping[str, Any],
    *,
    bundle_sha256: str,
    private_cases: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    assignment = plan["stage_assignments"]["terra_prejudge"]
    public_case_ids = tuple(assignment["public_case_ids"])
    base_case_ids = tuple(assignment["base_case_ids"])
    if len(public_case_ids) != len(set(public_case_ids)) or len(base_case_ids) != len(set(base_case_ids)):
        raise ValueError("Terra assignment contains duplicate IDs")
    if not public_case_ids or not base_case_ids:
        raise ValueError("Terra assignment must not be empty")
    expected_assignment_sha256 = judge_stage_assignment_sha256(
        "terra_prejudge",
        bundle_sha256,
        public_case_ids,
    )
    if assignment["assignment_sha256"] != expected_assignment_sha256:
        raise ValueError("Terra assignment hash differs from its exact public case set")
    if not set(public_case_ids).issubset(private_cases):
        raise ValueError("Terra assignment references an unknown public case")
    if not set(base_case_ids).issubset(set(plan["base_case_metadata"])):
        raise ValueError("Terra assignment references an unknown base case")
    expected_public_case_ids = {
        public_case_id
        for public_case_id, mapping in private_cases.items()
        if mapping["base_case_id"] in set(base_case_ids)
    }
    if set(public_case_ids) != expected_public_case_ids:
        raise ValueError("Terra assignment does not contain every public variant for its base cases")
    return public_case_ids, base_case_ids, expected_assignment_sha256


def audit_sol_followup_package(paths: SolFollowupPackagePaths) -> SolFollowupPackageAudit:
    """Validate one partial Terra source package before any diagnostic selection."""

    if paths.comparison_kind not in _COMPARISON_KINDS:
        raise ValueError("unknown Sol follow-up comparison kind")

    bundle_rows, bundle_bytes = _load_canonical_jsonl(paths.bundle, "judge bundle")
    bundle_cases = tuple(validate_judge_bundle_case(row) for row in bundle_rows)
    bundle_sha256 = _sha256_bytes(bundle_bytes)
    if judge_bundle_sha256(bundle_cases) != bundle_sha256:
        raise ValueError("judge bundle canonical hash differs from its bytes")
    cases_by_id = {case["case_id"]: case for case in bundle_cases}
    if len(cases_by_id) != len(bundle_cases):
        raise ValueError("judge bundle contains duplicate case IDs")

    bundle_manifest, bundle_manifest_bytes = _load_canonical_json(paths.bundle_manifest, "judge bundle manifest")
    bundle_manifest = validate_judge_bundle_manifest(bundle_manifest)
    if bundle_manifest["bundle_sha256"] != bundle_sha256:
        raise ValueError("judge bundle manifest differs from bundle bytes")
    if bundle_manifest["public_case_count"] != len(bundle_cases):
        raise ValueError("judge bundle manifest public count differs from the bundle")

    plan, plan_bytes = _load_canonical_json(paths.materialization_plan, "judge materialization plan")
    plan = validate_judge_materialization_plan(plan)
    plan_sha256 = _sha256_bytes(plan_bytes)
    if plan["bundle_sha256"] != bundle_sha256:
        raise ValueError("judge materialization plan is bound to another bundle")
    if bundle_manifest["materialization_plan_sha256"] != plan_sha256:
        raise ValueError("judge bundle manifest differs from materialization plan bytes")
    if bundle_manifest["coverage_policy_sha256"] != plan["coverage_policy_sha256"]:
        raise ValueError("judge materialization plan policy differs from the bundle manifest")

    private_payload, private_bytes = _load_canonical_json(paths.private_mapping, "private judge mapping")
    private_mapping_sha256 = _sha256_bytes(private_bytes)
    if bundle_manifest["private_mapping_sha256"] != private_mapping_sha256:
        raise ValueError("judge bundle manifest differs from private mapping bytes")
    private_cases, fine_tune_label = _validate_private_mapping(
        private_payload,
        cases_by_id=cases_by_id,
        bundle_manifest=bundle_manifest,
        comparison_kind=paths.comparison_kind,
        bundle_sha256=bundle_sha256,
    )
    if private_payload["materialization_plan_sha256"] != plan_sha256:
        raise ValueError("private judge mapping differs from materialization plan bytes")

    terra_public_case_ids, terra_base_case_ids, terra_assignment_sha256 = _validate_terra_assignment(
        plan,
        bundle_sha256=bundle_sha256,
        private_cases=private_cases,
    )

    verdict_rows, verdict_bytes = _load_canonical_jsonl(paths.verdicts, "Terra verdicts")
    verdicts = tuple(validate_judge_verdict(row) for row in verdict_rows)
    verdict_sha256 = _sha256_bytes(verdict_bytes)
    if judge_verdicts_sha256(verdicts) != verdict_sha256:
        raise ValueError("Terra verdict output hash is not canonical")
    verdict_by_id = {verdict["case_id"]: verdict for verdict in verdicts}
    if len(verdict_by_id) != len(verdicts):
        raise ValueError("Terra verdict output contains duplicate case IDs")
    if not set(verdict_by_id).issubset(set(terra_public_case_ids)):
        raise ValueError("Terra verdict output contains a case outside its assignment")
    expected_stage_order = tuple(
        case["case_id"] for case in bundle_cases if case["case_id"] in set(terra_public_case_ids)
    )
    expected_verdict_order = tuple(case_id for case_id in expected_stage_order if case_id in verdict_by_id)
    if tuple(verdict["case_id"] for verdict in verdicts) != expected_verdict_order:
        raise ValueError("Terra verdicts are not in canonical stage order")

    resume_manifest, resume_bytes = _load_canonical_json(paths.resume_manifest, "Terra resume manifest")
    resume_manifest = validate_judge_stage_resume_manifest(resume_manifest)
    if resume_manifest["status"] != "in_progress":
        raise ValueError("Sol follow-up accepts only incomplete Terra evidence")
    if resume_manifest["judge_stage"] != "terra_prejudge":
        raise ValueError("Sol follow-up accepts only Terra prejudge evidence")
    if resume_manifest["model_family"] != "gpt-5.6-terra" or resume_manifest["reasoning_effort"] != "max":
        raise ValueError("Terra resume manifest has an unexpected judge identity")
    if resume_manifest["reviewed_bundle_sha256"] != bundle_sha256:
        raise ValueError("Terra resume manifest is bound to another bundle")
    if resume_manifest["stage_assignment_sha256"] != terra_assignment_sha256:
        raise ValueError("Terra resume manifest assignment differs from materialization plan")
    if resume_manifest["expected_count"] != len(terra_public_case_ids):
        raise ValueError("Terra resume manifest expected count differs from assignment")
    if resume_manifest["completed_count"] != len(verdicts):
        raise ValueError("Terra resume manifest completed count differs from verdict output")
    missing_case_ids = tuple(resume_manifest["missing_case_ids"])
    expected_missing_case_ids = set(terra_public_case_ids).difference(verdict_by_id)
    if set(missing_case_ids) != expected_missing_case_ids:
        raise ValueError("Terra resume manifest missing cases differ from verdict output")
    if resume_manifest["missing_count"] != len(expected_missing_case_ids):
        raise ValueError("Terra resume manifest missing count differs from verdict output")
    if resume_manifest["merged_verdict_output_sha256"] != verdict_sha256:
        raise ValueError("Terra resume manifest verdict hash differs from verdict bytes")

    required_header_fields = (
        "judge_stage",
        "judge_id",
        "judge_session_id",
        "model_family",
        "reasoning_effort",
        "prompt_hash",
        "reviewed_bundle_sha256",
        "stage_assignment_sha256",
    )
    for verdict in verdicts:
        for field in required_header_fields:
            if verdict[field] != resume_manifest[field]:
                raise ValueError(f"Terra verdict {field} differs from the resume manifest")

    public_ids_by_base: dict[str, tuple[str, ...]] = {}
    for base_case_id in terra_base_case_ids:
        ids = tuple(
            public_case_id
            for public_case_id in expected_stage_order
            if private_cases[public_case_id]["base_case_id"] == base_case_id
        )
        if not ids:
            raise ValueError("Terra base case lacks an assigned public variant")
        public_ids_by_base[base_case_id] = ids

    base_outcomes: dict[str, SolFollowupBaseOutcome] = {}
    partial_base_case_count = 0
    for base_case_id in sorted(terra_base_case_ids):
        public_case_ids = public_ids_by_base[base_case_id]
        completed_ids = tuple(case_id for case_id in public_case_ids if case_id in verdict_by_id)
        if not completed_ids:
            partial_base_case_count += 1
            continue
        if len(completed_ids) != len(public_case_ids):
            partial_base_case_count += 1
            continue
        ordered_ids = tuple(
            sorted(
                completed_ids,
                key=lambda case_id: (
                    0 if private_cases[case_id]["position_variant"] == "primary" else 1,
                    case_id,
                ),
            )
        )
        selected_fine_tune: list[bool] = []
        acceptable: list[bool] = []
        critical: list[bool] = []
        flags: list[tuple[str, ...]] = []
        per_dimension_totals = dict.fromkeys(JUDGE_SCORE_DIMENSIONS, 0)
        for public_case_id in ordered_ids:
            verdict = verdict_by_id[public_case_id]
            mapping = private_cases[public_case_id]
            selected_label = (
                mapping["candidate_a_label"] if verdict["candidate"] == "A" else mapping["candidate_b_label"]
            )
            selected_fine_tune.append(selected_label == fine_tune_label)
            acceptable.append(verdict["acceptable"])
            critical.append(verdict["critical_error"])
            flags.append(tuple(sorted(verdict["flags"])))
            for dimension in JUDGE_SCORE_DIMENSIONS:
                per_dimension_totals[dimension] += verdict["scores"][dimension]
        metadata = plan["base_case_metadata"][base_case_id]
        base_outcomes[base_case_id] = SolFollowupBaseOutcome(
            base_case_id=base_case_id,
            public_case_ids=ordered_ids,
            selected_fine_tune=tuple(selected_fine_tune),
            acceptable=tuple(acceptable),
            critical=tuple(critical),
            flags=tuple(flags),
            minimum_average_score=min(total / len(ordered_ids) for total in per_dimension_totals.values()),
            split=metadata["split"],
            is_identity=metadata["is_identity"],
            coverage_categories=tuple(sorted(metadata["coverage_categories"])),
        )
    if not base_outcomes:
        raise ValueError("Terra source contains no fully completed base-case group")

    return SolFollowupPackageAudit(
        comparison_kind=paths.comparison_kind,
        bundle_cases=bundle_cases,
        bundle_sha256=bundle_sha256,
        base_outcomes=base_outcomes,
        completed_public_case_count=len(verdicts),
        partial_base_case_count=partial_base_case_count,
        source_hashes={
            "verdicts_sha256": verdict_sha256,
            "resume_manifest_sha256": _sha256_bytes(resume_bytes),
            "bundle_sha256": bundle_sha256,
            "private_mapping_sha256": private_mapping_sha256,
            "bundle_manifest_sha256": _sha256_bytes(bundle_manifest_bytes),
            "materialization_plan_sha256": plan_sha256,
        },
        terra_stage_assignment_sha256=terra_assignment_sha256,
        terra_stage_packet_sha256=resume_manifest["stage_packet_sha256"],
        terra_stage_packet_manifest_sha256=resume_manifest["stage_packet_manifest_sha256"],
        terra_prompt_hash=resume_manifest["prompt_hash"],
    )


def _ordered_by_rank(
    comparison_kind: str,
    tier: str,
    candidates: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(candidates, key=lambda base_case_id: (_rank(comparison_kind, tier, base_case_id), base_case_id))
    )


def _add_ranked(
    *,
    comparison_kind: str,
    tier: str,
    candidates: Sequence[str],
    chosen_tiers: dict[str, str],
    maximum: int | None,
) -> None:
    added = 0
    for base_case_id in _ordered_by_rank(comparison_kind, tier, candidates):
        if base_case_id in chosen_tiers:
            continue
        chosen_tiers[base_case_id] = tier
        added += 1
        if maximum is not None and added >= maximum:
            return


def _select_critical_flag_coverage(
    package: SolFollowupPackageAudit,
    chosen_tiers: dict[str, str],
) -> None:
    flag_to_base_ids: dict[str, set[str]] = {}
    for base_case_id, outcome in package.base_outcomes.items():
        if not outcome.has_critical_error:
            continue
        for flag in {flag for verdict_flags in outcome.flags for flag in verdict_flags}:
            flag_to_base_ids.setdefault(flag, set()).add(base_case_id)
    for flag in sorted(flag_to_base_ids):
        ranked = _ordered_by_rank(
            package.comparison_kind,
            f"critical_flag_coverage:{flag}",
            tuple(flag_to_base_ids[flag]),
        )
        selected = next((base_case_id for base_case_id in ranked if base_case_id not in chosen_tiers), None)
        if selected is not None:
            chosen_tiers[selected] = "critical_flag_coverage"


def _select_controls(
    package: SolFollowupPackageAudit,
    chosen_tiers: dict[str, str],
) -> None:
    """Select a bounded clean control sample using only available evidence."""

    candidates = {
        base_case_id
        for base_case_id, outcome in package.base_outcomes.items()
        if outcome.is_clean_control and base_case_id not in chosen_tiers
    }

    def add_one(tier_suffix: str, options: Sequence[str]) -> None:
        if len([tier for tier in chosen_tiers.values() if tier == "clean_control"]) >= _MAX_CONTROLS:
            return
        for base_case_id in _ordered_by_rank(package.comparison_kind, f"clean_control:{tier_suffix}", options):
            if base_case_id in candidates and base_case_id not in chosen_tiers:
                chosen_tiers[base_case_id] = "clean_control"
                return

    available_categories = sorted(
        {
            category
            for base_case_id in candidates
            for category in package.base_outcomes[base_case_id].coverage_categories
        }
    )
    for category in available_categories:
        add_one(
            f"category:{category}",
            tuple(
                base_case_id
                for base_case_id in candidates
                if category in package.base_outcomes[base_case_id].coverage_categories
            ),
        )

    available_splits = sorted({package.base_outcomes[base_case_id].split for base_case_id in candidates})
    for split in available_splits:
        for ordinal in range(_MIN_CONTROLS_PER_AVAILABLE_SPLIT):
            add_one(
                f"split:{split}:{ordinal}",
                tuple(
                    base_case_id for base_case_id in candidates if package.base_outcomes[base_case_id].split == split
                ),
            )

    if any(package.base_outcomes[base_case_id].is_identity for base_case_id in candidates):
        add_one(
            "identity",
            tuple(base_case_id for base_case_id in candidates if package.base_outcomes[base_case_id].is_identity),
        )

    _add_ranked(
        comparison_kind=package.comparison_kind,
        tier="clean_control",
        candidates=tuple(candidates),
        chosen_tiers=chosen_tiers,
        maximum=_MAX_CONTROLS - len([tier for tier in chosen_tiers.values() if tier == "clean_control"]),
    )


def _selection_manifest(
    package: SolFollowupPackageAudit,
    chosen_tiers: Mapping[str, str],
) -> SolFollowupSelection:
    selected_base_case_ids = tuple(sorted(chosen_tiers))
    if not selected_base_case_ids:
        raise ValueError("Sol follow-up selection is empty")
    selected_public_case_ids = tuple(
        case["case_id"]
        for case in package.bundle_cases
        if case["case_id"]
        in {
            public_case_id
            for base_case_id in selected_base_case_ids
            for public_case_id in package.base_outcomes[base_case_id].public_case_ids
        }
    )
    if not selected_public_case_ids:
        raise AssertionError("Sol follow-up selection lost every public case")
    assignment_sha256 = judge_stage_assignment_sha256(
        "sol_release",
        package.bundle_sha256,
        selected_public_case_ids,
    )
    tier_counts = Counter(chosen_tiers.values())
    selection_manifest = validate_sol_followup_selection_manifest(
        {
            "schema_version": 1,
            "kind": "scriber_sol_followup_selection_manifest",
            "diagnostic_only": True,
            "selection_namespace": SELECTION_NAMESPACE,
            "selection_seed": SELECTION_SEED,
            "comparison_kind": package.comparison_kind,
            "source": {
                "terra_judge_stage": "terra_prejudge",
                "terra_status": "in_progress",
                "terra_stage_assignment_sha256": package.terra_stage_assignment_sha256,
                "terra_stage_packet_sha256": package.terra_stage_packet_sha256,
                "terra_stage_packet_manifest_sha256": package.terra_stage_packet_manifest_sha256,
                "terra_prompt_hash": package.terra_prompt_hash,
                "source_hashes": dict(package.source_hashes),
                "completed_base_case_count": len(package.base_outcomes),
                "completed_public_case_count": package.completed_public_case_count,
                "partial_base_case_count": package.partial_base_case_count,
            },
            "selection": {
                "selected_base_case_count": len(selected_base_case_ids),
                "selected_public_case_count": len(selected_public_case_ids),
                "base_case_ids_sha256": _base_case_ids_sha256(selected_base_case_ids),
                "public_case_ids_sha256": _case_ids_sha256(selected_public_case_ids),
                "tier_counts": {tier: tier_counts[tier] for tier in _TIER_IDS},
                "selected_cases": [
                    {
                        "base_case_id": base_case_id,
                        "public_case_ids": list(package.base_outcomes[base_case_id].public_case_ids),
                        "tiers": [chosen_tiers[base_case_id]],
                    }
                    for base_case_id in selected_base_case_ids
                ],
            },
            "sol_release": {
                "stage_assignment_sha256": assignment_sha256,
                "stage_assignment_case_ids_sha256": _case_ids_sha256(selected_public_case_ids),
                "packet_case_ids_sha256": _case_ids_sha256(selected_public_case_ids),
            },
            "privacy": {
                "source_payload_present": False,
                "reference_payload_present": False,
                "output_payload_present": False,
                "judge_reason_present": False,
                "contains_model_identity": False,
                "training_examples_present": False,
            },
        }
    )
    return SolFollowupSelection(
        comparison_kind=package.comparison_kind,
        public_case_ids=selected_public_case_ids,
        selected_base_case_ids=selected_base_case_ids,
        selection_manifest=selection_manifest,
    )


def select_sol_followup_cases(
    packages: Sequence[SolFollowupPackageAudit],
) -> tuple[SolFollowupSelection, ...]:
    """Pure deterministic selector over already-audited, text-free package state.

    Tiers are deduplicated in their declared order: a base case enters at its
    first qualifying tier only.  Cross-comparison candidates are selected as a
    shared base-ID set before per-comparison de-duplication.
    """

    if len(packages) != 2 or {package.comparison_kind for package in packages} != _COMPARISON_KINDS:
        raise ValueError("Sol follow-up requires exactly the two canonical comparison packages")
    ordered_packages = tuple(sorted(packages, key=lambda package: package.comparison_kind))
    chosen_by_comparison: dict[str, dict[str, str]] = {package.comparison_kind: {} for package in ordered_packages}

    for package in ordered_packages:
        chosen = chosen_by_comparison[package.comparison_kind]
        _select_critical_flag_coverage(package, chosen)
        _add_ranked(
            comparison_kind=package.comparison_kind,
            tier="extra_critical",
            candidates=tuple(
                base_case_id for base_case_id, outcome in package.base_outcomes.items() if outcome.has_critical_error
            ),
            chosen_tiers=chosen,
            maximum=_MAX_EXTRA_CRITICAL,
        )
        _add_ranked(
            comparison_kind=package.comparison_kind,
            tier="noncritical_rejection",
            candidates=tuple(
                base_case_id
                for base_case_id, outcome in package.base_outcomes.items()
                if outcome.is_noncritical_rejection
            ),
            chosen_tiers=chosen,
            maximum=_MAX_NONCRITICAL_REJECTIONS,
        )
        _add_ranked(
            comparison_kind=package.comparison_kind,
            tier="position_conflict",
            candidates=tuple(
                base_case_id for base_case_id, outcome in package.base_outcomes.items() if outcome.has_position_conflict
            ),
            chosen_tiers=chosen,
            maximum=_MAX_POSITION_CONFLICTS,
        )
        _add_ranked(
            comparison_kind=package.comparison_kind,
            tier="clean_low_score",
            candidates=tuple(
                base_case_id for base_case_id, outcome in package.base_outcomes.items() if outcome.is_clean_low_score
            ),
            chosen_tiers=chosen,
            maximum=None,
        )

    left, right = ordered_packages
    # Anchor the shared 32-ID divergence draw after the primary comparison's
    # earlier tiers; the same draw is then de-duplicated independently below.
    anchor_preselected = chosen_by_comparison["fine_tune_vs_base"]
    divergence_base_ids = tuple(
        sorted(
            base_case_id
            for base_case_id in set(left.base_outcomes).intersection(right.base_outcomes)
            if left.base_outcomes[base_case_id].decision_signature
            != right.base_outcomes[base_case_id].decision_signature
            and base_case_id not in anchor_preselected
        )
    )
    selected_cross_base_ids = _ordered_by_rank(
        "cross_comparison",
        "cross_comparison_divergence",
        divergence_base_ids,
    )[:_MAX_CROSS_COMPARISON_DIVERGENCES]
    for package in ordered_packages:
        chosen = chosen_by_comparison[package.comparison_kind]
        for base_case_id in selected_cross_base_ids:
            if base_case_id in package.base_outcomes and base_case_id not in chosen:
                chosen[base_case_id] = "cross_comparison_divergence"

    for package in ordered_packages:
        _select_controls(package, chosen_by_comparison[package.comparison_kind])

    return tuple(
        _selection_manifest(package, chosen_by_comparison[package.comparison_kind]) for package in ordered_packages
    )


def build_sol_followup_artifacts(
    packages: Sequence[SolFollowupPackageAudit],
    *,
    prompt_hash: str,
) -> SolFollowupArtifacts:
    """Build two blind packets and aggregate-only diagnostic metadata in memory."""

    if not isinstance(prompt_hash, str) or not prompt_hash.startswith("sha256:") or not _is_sha256(prompt_hash[7:]):
        raise ValueError("Sol follow-up prompt hash must be a sha256: prefixed digest")
    ordered_packages = tuple(sorted(packages, key=lambda package: package.comparison_kind))
    selections = select_sol_followup_cases(ordered_packages)
    packets: list[BlindedJudgeStagePacket] = []
    package_summaries: list[dict[str, Any]] = []
    manifest_packages: list[dict[str, Any]] = []
    for package, selection in zip(ordered_packages, selections, strict=True):
        packet = materialize_judge_stage_packet(
            package.bundle_cases,
            judge_stage="sol_release",
            public_case_ids=selection.public_case_ids,
            assignment_sha256=selection.selection_manifest["sol_release"]["stage_assignment_sha256"],
            prompt_hash=prompt_hash,
        )
        if (
            packet.manifest["stage_assignment_sha256"]
            != selection.selection_manifest["sol_release"]["stage_assignment_sha256"]
        ):
            raise AssertionError("Sol packet assignment hash differs from selection manifest")
        if packet.manifest["case_ids_sha256"] != selection.selection_manifest["sol_release"]["packet_case_ids_sha256"]:
            raise AssertionError("Sol packet IDs differ from selection manifest")
        packet_bytes = _canonical_jsonl_bytes(packet.cases)
        packet_manifest_bytes = _canonical_json_bytes(packet.manifest)
        selection_manifest_bytes = _canonical_json_bytes(selection.selection_manifest)
        tier_counts = dict(selection.selection_manifest["selection"]["tier_counts"])
        package_summaries.append(
            {
                "comparison_kind": package.comparison_kind,
                "completed_base_case_count": len(package.base_outcomes),
                "completed_public_case_count": package.completed_public_case_count,
                "partial_base_case_count": package.partial_base_case_count,
                "selected_base_case_count": len(selection.selected_base_case_ids),
                "selected_public_case_count": len(selection.public_case_ids),
                "tier_counts": tier_counts,
                "source_hashes": dict(package.source_hashes),
                "sol_stage_assignment_sha256": packet.manifest["stage_assignment_sha256"],
                "selection_manifest_sha256": _sha256_bytes(selection_manifest_bytes),
                "packet_sha256": _sha256_bytes(packet_bytes),
                "packet_manifest_sha256": _sha256_bytes(packet_manifest_bytes),
            }
        )
        manifest_packages.append(
            {
                "comparison_kind": package.comparison_kind,
                "selection_manifest_sha256": _sha256_bytes(selection_manifest_bytes),
                "packet_sha256": _sha256_bytes(packet_bytes),
                "packet_manifest_sha256": _sha256_bytes(packet_manifest_bytes),
            }
        )
        packets.append(packet)

    diagnostic_report = validate_sol_followup_diagnostic_report(
        {
            "schema_version": 1,
            "kind": "scriber_sol_followup_diagnostic_report",
            "status": "diagnostic_only",
            "diagnostic_only": True,
            "selection_namespace": SELECTION_NAMESPACE,
            "selection_seed": SELECTION_SEED,
            "packages": package_summaries,
            "release_aggregation": {
                "full_campaign_aggregation_performed": False,
                "release_gate_evaluated": False,
                "release_gate_passed": False,
                "verdicts_created": False,
                "blocked_reason": "Diagnostic Sol follow-up packets are selected from incomplete Terra evidence and are not release evidence.",
            },
            "training_policy": {
                "new_training_examples_created": 0,
                "direct_test_or_challenge_reuse": "prohibited",
                "allowed_followup": "none_until_independent_post_evaluation_work_is_authorized",
            },
            "privacy": {
                "source_payload_present": False,
                "reference_payload_present": False,
                "output_payload_present": False,
                "judge_reason_present": False,
                "contains_model_identity": False,
                "training_examples_present": False,
            },
        }
    )
    artifact_manifest = validate_sol_followup_artifact_manifest(
        {
            "schema_version": 1,
            "kind": "scriber_sol_followup_artifact_manifest",
            "diagnostic_only": True,
            "selection_namespace": SELECTION_NAMESPACE,
            "selection_seed": SELECTION_SEED,
            "diagnostic_report_sha256": _prefixed_sha256(_canonical_json_bytes(diagnostic_report)),
            "packages": manifest_packages,
            "contains_verdicts": False,
        }
    )
    return SolFollowupArtifacts(
        selections=selections,
        packets=tuple(packets),
        diagnostic_report=diagnostic_report,
        artifact_manifest=artifact_manifest,
    )


def write_sol_followup_artifacts(output_dir: Path, artifacts: SolFollowupArtifacts) -> dict[str, Path]:
    """Atomically commit a complete diagnostic artifact root or leave no output root."""

    if output_dir.exists():
        raise ValueError("Sol follow-up output directory already exists")
    if len(artifacts.selections) != 2 or len(artifacts.packets) != 2:
        raise ValueError("Sol follow-up artifacts must contain exactly two selections and packets")
    output_parent = output_dir.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_parent))
    try:
        paths: dict[str, Path] = {}
        for selection, packet in zip(artifacts.selections, artifacts.packets, strict=True):
            if selection.comparison_kind not in _COMPARISON_KINDS:
                raise ValueError("Sol follow-up artifact has an unknown comparison")
            public_dir = temporary_dir / selection.comparison_kind
            private_dir = temporary_dir / "private" / selection.comparison_kind
            public_dir.mkdir(parents=True)
            private_dir.mkdir(parents=True)
            packet_path = public_dir / "sol-release.packet.jsonl"
            packet_manifest_path = public_dir / "sol-release.packet.manifest.json"
            selection_path = private_dir / "sol-followup.selection.manifest.json"
            packet_path.write_bytes(_canonical_jsonl_bytes(packet.cases))
            packet_manifest_path.write_bytes(_canonical_json_bytes(packet.manifest))
            selection_path.write_bytes(_canonical_json_bytes(selection.selection_manifest))
            paths[f"{selection.comparison_kind}:packet"] = output_dir / selection.comparison_kind / packet_path.name
            paths[f"{selection.comparison_kind}:packet_manifest"] = (
                output_dir / selection.comparison_kind / packet_manifest_path.name
            )
            paths[f"{selection.comparison_kind}:selection_manifest"] = (
                output_dir / "private" / selection.comparison_kind / selection_path.name
            )
        report_path = temporary_dir / "sol-followup-diagnostic-report.json"
        artifact_manifest_path = temporary_dir / "sol-followup-artifacts.manifest.json"
        report_path.write_bytes(_canonical_json_bytes(artifacts.diagnostic_report))
        artifact_manifest_path.write_bytes(_canonical_json_bytes(artifacts.artifact_manifest))
        paths["diagnostic_report"] = output_dir / report_path.name
        paths["artifact_manifest"] = output_dir / artifact_manifest_path.name
        os.replace(temporary_dir, output_dir)
        return paths
    except Exception:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
        raise

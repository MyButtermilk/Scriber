import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.judge_aggregation import (
    aggregate_judge_campaign_reports,
    aggregate_judge_verdicts,
    build_judge_stage_output_manifest,
    judge_bundle_sha256,
    materialize_sol_release_assignment,
    resume_judge_stage_verdicts,
)
from scriber_polishing.judge_bundle import (
    JUDGE_SCORE_DIMENSIONS,
    build_blinded_judge_bundle,
    judge_materialization_plan_sha256,
    judge_stage_assignment_sha256,
    materialize_judge_coverage,
    materialize_judge_stage_packet,
)
from scriber_polishing.schemas import validate_judge_verdict
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reference(index: int, split: str) -> dict[str, object]:
    target_sst = f"[DOC]\n[P]Bitte bis {index:02d}.08.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    tags = ["dates"]
    if split in {"test", "challenge"}:
        tags.append("generator_family:test")
    if split == "test":
        tags.append("quota_case")
    if split == "regression":
        tags.append("critical_regression")
    return {
        "schema_version": 1,
        "case_id": f"eval_case_{index:03d}",
        "split": split,
        "source_text": f"bitte bis {index:02d}.08.2026 antworten",
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Eine Antwort ist erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": f"Die Frist endet am {index:02d}.08.2026.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [f"{index:02d}.08.2026"],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Antwortfrist"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [f"{index:02d}.08.2026"],
        "slice_tags": tags,
        "is_identity": False,
    }


def _references() -> list[dict[str, object]]:
    return [
        _reference(1, "test"),
        _reference(2, "ai_gold_test"),
        _reference(3, "challenge"),
        _reference(4, "ai_gold_challenge"),
        _reference(5, "regression"),
    ]


def _prediction(reference: dict[str, object], sst: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": reference["case_id"],
        "prediction_sst": sst or reference["target_sst"],
    }


def _coverage_policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "selection_seed": 270036,
        "regular_test_minimum": 1,
        "ai_gold_test_expected_count": 1,
        "ai_gold_challenge_expected_count": 1,
        "critical_regression_expected_count": 1,
        "terra_accepted_sample_fraction": 0.25,
        "generator_family_tag_prefix": "generator_family:",
        "required_generator_families": ["generator_family:test"],
        "required_comparison_kinds": [
            "fine_tune_vs_base",
            "fine_tune_vs_rule_baseline",
        ],
        "category_quotas": [
            {
                "id": "quota_case",
                "minimum": 1,
                "any_slice_tags": ["quota_case"],
            }
        ],
    }


def _bundle(*, unsafe_y: bool = False):
    references = _references()
    candidate_x = [_prediction(reference) for reference in references]
    candidate_y = [
        _prediction(
            reference,
            str(reference["target_sst"]).replace("antworten.", "antworten!"),
        )
        for reference in references
    ]
    if unsafe_y:
        candidate_y[0] = _prediction(
            references[0],
            "[DOC]\n[P]Bitte bis 09.08.2026 antworten.[/P]\n[/DOC]",
        )
    bundle = build_blinded_judge_bundle(
        references,
        candidate_x,
        candidate_y,
        candidate_x_label="candidate_x",
        candidate_y_label="candidate_y",
        blind_seed=270031,
    )
    plan = materialize_judge_coverage(references, bundle, _coverage_policy())
    assert plan["coverage_passed"] is True
    return bundle, references, plan


def _verdict(
    bundle,
    public_case_id: str,
    *,
    stage: str,
    label: str,
    assignment_sha256: str,
    acceptable: bool = True,
    score: int = 5,
) -> dict[str, object]:
    model_family = "gpt-5.6-terra" if stage == "terra_prejudge" else "gpt-5.6-sol"
    reasoning_effort = "ultra" if stage == "sol_tiebreak" else "max"
    mapping = bundle.private_mapping[public_case_id]
    candidate = "A" if mapping["candidate_a_label"] == label else "B"
    return {
        "schema_version": 2,
        "case_id": public_case_id,
        "judge_id": {
            "terra_prejudge": "judge_terra_max_01",
            "sol_release": "judge_sol_max_01",
            "sol_tiebreak": "judge_sol_ultra_01",
        }[stage],
        "judge_session_id": {
            "terra_prejudge": "session_terra_max_run_01",
            "sol_release": "session_sol_max_run_01",
            "sol_tiebreak": "session_sol_ultra_run_01",
        }[stage],
        "judge_stage": stage,
        "model_family": model_family,
        "reasoning_effort": reasoning_effort,
        "prompt_hash": "sha256:" + "d" * 64,
        "reviewed_bundle_sha256": judge_bundle_sha256(bundle.cases),
        "stage_assignment_sha256": assignment_sha256,
        "candidate": candidate,
        "acceptable": acceptable,
        "critical_error": False,
        "scores": dict.fromkeys(JUDGE_SCORE_DIMENSIONS, score),
        "flags": [],
        "brief_reason": "Die gewählte Ausgabe ist semantisch treu und formal korrekt.",
    }


def _stage_for_label(
    bundle,
    *,
    stage: str,
    label: str,
    public_case_ids: list[str] | tuple[str, ...],
    assignment_sha256: str,
    unacceptable_base_ids: frozenset[str] = frozenset(),
    score: int = 5,
) -> list[dict[str, object]]:
    return [
        _verdict(
            bundle,
            public_case_id,
            stage=stage,
            label=label,
            assignment_sha256=assignment_sha256,
            acceptable=(bundle.private_mapping[public_case_id]["base_case_id"] not in unacceptable_base_ids),
            score=score,
        )
        for public_case_id in public_case_ids
    ]


def _initial_evidence(
    bundle,
    plan,
    *,
    terra_label: str = "candidate_x",
    sol_label: str = "candidate_x",
    terra_unacceptable: frozenset[str] = frozenset(),
    score: int = 5,
):
    terra_assignment = plan["stage_assignments"]["terra_prejudge"]
    terra = _stage_for_label(
        bundle,
        stage="terra_prejudge",
        label=terra_label,
        public_case_ids=terra_assignment["public_case_ids"],
        assignment_sha256=terra_assignment["assignment_sha256"],
        unacceptable_base_ids=terra_unacceptable,
        score=score,
    )
    terra_manifest = build_judge_stage_output_manifest(terra)
    sol_assignment = materialize_sol_release_assignment(
        bundle.cases,
        bundle.private_mapping,
        plan,
        terra,
        terra_manifest,
    )
    sol = _stage_for_label(
        bundle,
        stage="sol_release",
        label=sol_label,
        public_case_ids=sol_assignment["public_case_ids"],
        assignment_sha256=sol_assignment["assignment_sha256"],
        score=score,
    )
    sol_manifest = build_judge_stage_output_manifest(sol)
    return terra, terra_manifest, sol, sol_manifest, sol_assignment


def _aggregate(
    bundle,
    plan,
    terra,
    terra_manifest,
    sol,
    sol_manifest,
    **kwargs,
):
    return aggregate_judge_verdicts(
        bundle.cases,
        bundle.private_mapping,
        terra,
        sol,
        materialization_plan=plan,
        terra_output_manifest=terra_manifest,
        sol_output_manifest=sol_manifest,
        comparison_kind="fine_tune_vs_base",
        **kwargs,
    )


def _tie_evidence(bundle, result, *, label: str = "candidate_x", score: int = 5):
    assignment_hash = judge_stage_assignment_sha256(
        "sol_tiebreak",
        result.bundle_sha256,
        result.required_tie_break_case_ids,
    )
    verdicts = _stage_for_label(
        bundle,
        stage="sol_tiebreak",
        label=label,
        public_case_ids=result.required_tie_break_case_ids,
        assignment_sha256=assignment_hash,
        score=score,
    )
    return verdicts, build_judge_stage_output_manifest(verdicts)


def test_judge_verdict_schema_is_closed_and_binds_session_and_assignment() -> None:
    bundle, _, plan = _bundle()
    assignment = plan["stage_assignments"]["terra_prejudge"]
    verdict = _verdict(
        bundle,
        assignment["public_case_ids"][0],
        stage="terra_prejudge",
        label="candidate_x",
        assignment_sha256=assignment["assignment_sha256"],
    )

    assert validate_judge_verdict(verdict) == verdict

    invalid = deepcopy(verdict)
    invalid["flags"] = ["changed_number"]
    invalid["critical_error"] = True
    with pytest.raises(ValueError, match="judge verdict failed"):
        validate_judge_verdict(invalid)

    unexpected = deepcopy(verdict)
    unexpected["model_name"] = "identity_leak"
    with pytest.raises(ValueError, match="judge verdict failed"):
        validate_judge_verdict(unexpected)

    missing_session = deepcopy(verdict)
    del missing_session["judge_session_id"]
    with pytest.raises(ValueError, match="judge verdict failed"):
        validate_judge_verdict(missing_session)

    wrong_model = deepcopy(verdict)
    wrong_model["model_family"] = "gpt-5.6-sol"
    with pytest.raises(ValueError, match="judge verdict failed"):
        validate_judge_verdict(wrong_model)


def test_stage_verdict_resume_merges_batches_and_fails_closed_on_session_drift() -> None:
    bundle, _, plan = _bundle()
    terra, terra_manifest, _, _, _ = _initial_evidence(bundle, plan)
    assignment = plan["stage_assignments"]["terra_prejudge"]
    packet = materialize_judge_stage_packet(
        bundle.cases,
        judge_stage="terra_prejudge",
        public_case_ids=assignment["public_case_ids"],
        assignment_sha256=assignment["assignment_sha256"],
        prompt_hash=terra_manifest["prompt_hash"],
    )
    split_at = max(1, len(terra) // 2)

    partial = resume_judge_stage_verdicts(
        packet.cases,
        packet.manifest,
        [terra[:split_at]],
    )

    assert partial.status == "in_progress"
    assert partial.output_manifest is None
    assert partial.completed_count == split_at
    assert set(partial.missing_case_ids) == {verdict["case_id"] for verdict in terra[split_at:]}

    complete = resume_judge_stage_verdicts(
        packet.cases,
        packet.manifest,
        [terra[split_at:], terra[:split_at]],
    )

    assert complete.status == "complete"
    assert complete.output_manifest == build_judge_stage_output_manifest(complete.verdicts)
    assert complete.missing_case_ids == ()
    assert [verdict["case_id"] for verdict in complete.verdicts] == [case["case_id"] for case in packet.cases]

    drifted = deepcopy(terra[split_at:])
    drifted[0]["judge_session_id"] = "session_terra_max_drifted"
    with pytest.raises(ValueError, match="judge_session_id"):
        resume_judge_stage_verdicts(
            packet.cases,
            packet.manifest,
            [terra[:split_at], drifted],
        )


def test_aggregation_requires_exact_materialized_stages_and_is_position_invariant() -> None:
    bundle, _, plan = _bundle()
    terra, terra_manifest, sol, sol_manifest, sol_assignment = _initial_evidence(
        bundle,
        plan,
    )

    result = _aggregate(bundle, plan, terra, terra_manifest, sol, sol_manifest)

    assert result.status == "complete"
    assert set(result.decisions) == set(plan["base_case_metadata"])
    assert all(decision.candidate_label == "candidate_x" for decision in result.decisions.values())
    assert all(decision.acceptable for decision in result.decisions.values())
    assert result.required_tie_break_case_ids == ()
    assert result.required_sol_release_case_ids == tuple(sol_assignment["public_case_ids"])
    assert result.stage_coverage["terra_prejudge"]["exact"] is True
    assert result.stage_coverage["sol_release"]["exact"] is True
    assert result.candidate_summaries["candidate_x"].win_rate == 1.0
    assert result.candidate_summaries["candidate_y"].win_rate == 0.0


def test_identical_candidate_outputs_are_neutral_equivalences_not_position_wins() -> None:
    references = _references()
    predictions = [_prediction(reference) for reference in references]
    bundle = build_blinded_judge_bundle(
        references,
        predictions,
        predictions,
        candidate_x_label="candidate_x",
        candidate_y_label="candidate_y",
        blind_seed=270031,
    )
    plan = materialize_judge_coverage(references, bundle, _coverage_policy())
    terra_assignment = plan["stage_assignments"]["terra_prejudge"]
    terra = _stage_for_label(
        bundle,
        stage="terra_prejudge",
        label="candidate_x",
        public_case_ids=terra_assignment["public_case_ids"],
        assignment_sha256=terra_assignment["assignment_sha256"],
    )
    for verdict in terra:
        verdict["candidate"] = "A"
    terra_manifest = build_judge_stage_output_manifest(terra)
    sol_assignment = materialize_sol_release_assignment(
        bundle.cases,
        bundle.private_mapping,
        plan,
        terra,
        terra_manifest,
    )
    sol = _stage_for_label(
        bundle,
        stage="sol_release",
        label="candidate_x",
        public_case_ids=sol_assignment["public_case_ids"],
        assignment_sha256=sol_assignment["assignment_sha256"],
    )
    for verdict in sol:
        verdict["candidate"] = "A"
    sol_manifest = build_judge_stage_output_manifest(sol)

    result = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
        release_candidate="candidate_x",
        acceptable_rate_minimums={"test": 0.0, "challenge": 0.0},
        rubric_minimums={},
    )

    assert result.status == "complete"
    assert result.required_tie_break_case_ids == ()
    assert result.winning_candidate_labels == ("candidate_x", "candidate_y")
    assert all(decision.candidate_label is None for decision in result.decisions.values())
    assert all(decision.candidates_identical for decision in result.decisions.values())
    for summary in result.candidate_summaries.values():
        assert summary.win_count == 0
        assert summary.equivalent_count == len(references)
        assert summary.acceptable_count == len(references)
        assert summary.rubric_observation_count == len(references)
    assert result.comparison_gate_passed is True

    with pytest.raises(ValueError, match="missing verdict"):
        _aggregate(
            bundle,
            plan,
            terra,
            terra_manifest,
            sol[:-1],
            build_judge_stage_output_manifest(sol[:-1]),
        )

    altered_session = deepcopy(sol)
    for verdict in altered_session:
        verdict["judge_session_id"] = terra_manifest["judge_session_id"]
    with pytest.raises(ValueError, match="independent judge sessions"):
        _aggregate(
            bundle,
            plan,
            terra,
            terra_manifest,
            altered_session,
            build_judge_stage_output_manifest(altered_session),
        )

    tampered_manifest = deepcopy(sol_manifest)
    tampered_manifest["prompt_hash"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="differs from manifest prompt_hash"):
        _aggregate(bundle, plan, terra, terra_manifest, sol, tampered_manifest)

    tampered_plan = deepcopy(plan)
    quota = tampered_plan["coverage"]["category_quotas"]["quota_case"]
    quota["available"] += 1
    quota["selected"] += 1
    with pytest.raises(ValueError, match="category count differs from cases"):
        _aggregate(
            bundle,
            tampered_plan,
            terra,
            terra_manifest,
            sol,
            sol_manifest,
        )

    tampered_metadata = deepcopy(plan)
    metadata = tampered_metadata["base_case_metadata"]["eval_case_002"]
    metadata["automatic_suspicious"] = not metadata["automatic_suspicious"]
    with pytest.raises(ValueError, match="suspicious marker differs from bundle"):
        _aggregate(
            bundle,
            tampered_metadata,
            terra,
            terra_manifest,
            sol,
            sol_manifest,
        )


def test_acceptability_flip_across_swapped_positions_requires_tiebreak() -> None:
    bundle, _, plan = _bundle()
    terra_assignment = plan["stage_assignments"]["terra_prejudge"]
    public_ids_by_base: dict[str, list[str]] = {}
    for public_case_id in terra_assignment["public_case_ids"]:
        base_case_id = bundle.private_mapping[public_case_id]["base_case_id"]
        public_ids_by_base.setdefault(base_case_id, []).append(public_case_id)
    conflicted_base_id, conflicted_public_ids = next(
        (base_case_id, public_case_ids)
        for base_case_id, public_case_ids in public_ids_by_base.items()
        if len(public_case_ids) == 2
    )

    terra = _stage_for_label(
        bundle,
        stage="terra_prejudge",
        label="candidate_x",
        public_case_ids=terra_assignment["public_case_ids"],
        assignment_sha256=terra_assignment["assignment_sha256"],
    )
    next(verdict for verdict in terra if verdict["case_id"] == conflicted_public_ids[0])["acceptable"] = False
    terra_manifest = build_judge_stage_output_manifest(terra)
    sol_assignment = materialize_sol_release_assignment(
        bundle.cases,
        bundle.private_mapping,
        plan,
        terra,
        terra_manifest,
    )
    sol = _stage_for_label(
        bundle,
        stage="sol_release",
        label="candidate_x",
        public_case_ids=sol_assignment["public_case_ids"],
        assignment_sha256=sol_assignment["assignment_sha256"],
    )
    next(verdict for verdict in sol if verdict["case_id"] == conflicted_public_ids[0])["acceptable"] = False
    sol_manifest = build_judge_stage_output_manifest(sol)

    pending = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
    )

    assert pending.status == "pending_tiebreak"
    assert pending.stage_coverage["sol_tiebreak"]["position_conflict_base_case_count"] == 1
    assert set(conflicted_public_ids).issubset(pending.required_tie_break_case_ids)
    assert conflicted_base_id not in pending.decisions


def test_sol_assignment_includes_all_objections_and_seeded_quarter_sample() -> None:
    bundle, _, plan = _bundle()
    terra, terra_manifest, sol, sol_manifest, sol_assignment = _initial_evidence(
        bundle,
        plan,
        terra_unacceptable=frozenset({"eval_case_001"}),
    )

    assert sol_assignment["terra_objection_base_case_ids"] == ["eval_case_001"]
    assert len(sol_assignment["terra_accepted_sample_base_case_ids"]) == 1
    assert set(sol_assignment["terra_accepted_sample_base_case_ids"]).issubset({"eval_case_002", "eval_case_003"})
    repeated = materialize_sol_release_assignment(
        bundle.cases,
        bundle.private_mapping,
        plan,
        terra,
        terra_manifest,
    )
    assert repeated == sol_assignment

    result = _aggregate(bundle, plan, terra, terra_manifest, sol, sol_manifest)
    assert result.stage_coverage["sol_release"]["terra_objection_base_case_count"] == 1
    assert result.stage_coverage["sol_release"]["terra_accepted_sample_base_case_count"] == 1
    assert result.stage_coverage["sol_release"]["terra_accepted_review_fraction"] >= 0.25


def test_disagreements_require_complete_independent_sol_ultra_tie_break() -> None:
    bundle, _, plan = _bundle()
    terra, terra_manifest, sol, sol_manifest, _ = _initial_evidence(
        bundle,
        plan,
        terra_label="candidate_x",
        sol_label="candidate_y",
    )

    pending = _aggregate(bundle, plan, terra, terra_manifest, sol, sol_manifest)
    assert pending.status == "pending_tiebreak"
    assert pending.required_tie_break_case_ids
    assert pending.missing_tie_break_case_ids == pending.required_tie_break_case_ids
    assert pending.comparison_gate_passed is None
    assert pending.release_gate_passed is False

    tie_break, tie_manifest = _tie_evidence(bundle, pending)
    partial = tie_break[:-1]
    partially_pending = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
        tie_break_verdicts=partial,
        tie_break_output_manifest=build_judge_stage_output_manifest(partial),
    )
    assert partially_pending.status == "pending_tiebreak"
    assert len(partially_pending.missing_tie_break_case_ids) == 1

    result = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
        tie_break_verdicts=tie_break,
        tie_break_output_manifest=tie_manifest,
    )
    assert result.status == "complete"
    assert all(
        result.decisions[base_case_id].candidate_label == "candidate_x"
        for base_case_id in {
            bundle.private_mapping[case_id]["base_case_id"] for case_id in pending.required_tie_break_case_ids
        }
    )

    reused_session = deepcopy(tie_break)
    for verdict in reused_session:
        verdict["judge_session_id"] = sol_manifest["judge_session_id"]
    with pytest.raises(ValueError, match="independent judge session"):
        _aggregate(
            bundle,
            plan,
            terra,
            terra_manifest,
            sol,
            sol_manifest,
            tie_break_verdicts=reused_session,
            tie_break_output_manifest=build_judge_stage_output_manifest(reused_session),
        )


def test_deterministic_hard_errors_force_tie_break_and_remain_binding() -> None:
    bundle, _, plan = _bundle(unsafe_y=True)
    terra, terra_manifest, sol, sol_manifest, _ = _initial_evidence(
        bundle,
        plan,
        terra_label="candidate_y",
        sol_label="candidate_y",
    )

    pending = _aggregate(bundle, plan, terra, terra_manifest, sol, sol_manifest)
    assert pending.status == "pending_tiebreak"
    affected_public_ids = {
        case_id for case_id, mapping in bundle.private_mapping.items() if mapping["base_case_id"] == "eval_case_001"
    }
    assert affected_public_ids.issubset(pending.required_tie_break_case_ids)

    tie_break, tie_manifest = _tie_evidence(bundle, pending, label="candidate_y")
    result = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
        tie_break_verdicts=tie_break,
        tie_break_output_manifest=tie_manifest,
    )
    decision = result.decisions["eval_case_001"]
    assert decision.candidate_label == "candidate_y"
    assert decision.acceptable is False
    assert "deterministic:changed_date_or_time" in decision.rejection_reasons
    assert "deterministic:damaged_protected_span" in decision.rejection_reasons


def test_release_candidate_thresholds_cover_test_challenge_and_ai_gold_strata() -> None:
    bundle, _, plan = _bundle()
    terra, terra_manifest, sol, sol_manifest, _ = _initial_evidence(
        bundle,
        plan,
        score=5,
    )
    for verdict in [*terra, *sol]:
        verdict["scores"]["grammar"] = 4
    terra_manifest = build_judge_stage_output_manifest(terra)
    sol_manifest = build_judge_stage_output_manifest(sol)

    result = _aggregate(
        bundle,
        plan,
        terra,
        terra_manifest,
        sol,
        sol_manifest,
        release_candidate="candidate_x",
        acceptable_rate_minimums={"test": 0.97, "challenge": 0.95},
        rubric_minimums={
            "semantic_fidelity": 4.5,
            "no_additions": 4.5,
            "no_omissions": 4.5,
            "spelling": 4.2,
            "grammar": 4.2,
            "punctuation": 4.2,
            "paragraph_structure": 4.2,
            "heading_structure": 4.2,
            "numbered_lists": 4.2,
            "unit_normalization": 4.2,
            "legal_citations": 4.2,
            "address_pronouns": 4.2,
        },
    )

    assert result.evaluation_split == "combined"
    assert result.release_gate_passed is False
    assert result.comparison_gate_passed is False
    for split in ("test", "ai_gold_test", "challenge", "ai_gold_challenge"):
        assert result.release_gate[f"acceptable_rate:{split}"]["passed"] is True
    assert result.release_gate["terra_acceptable_rate:regular"]["passed"] is True
    assert result.release_gate["terra_acceptable_rate:challenge"]["passed"] is True
    assert result.release_gate["rubric:grammar"]["actual"] == 4.0
    assert result.release_gate["rubric:grammar"]["passed"] is False
    assert result.release_gate["deterministic_critical_case_count"]["actual"] == 0
    assert result.release_gate["judge_critical_case_count"]["actual"] == 0


def _evaluation_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "seed": 270020,
                "candidates": ["candidate_x", "candidate_y"],
                "automatic_evaluation": {
                    "hard_gates": [
                        {
                            "id": "output_only",
                            "slice": "all",
                            "metric": "output_only_compliance_rate",
                            "operator": "gte",
                            "threshold": 1.0,
                            "minimum_cases": 1,
                        }
                    ],
                    "minimums": [
                        {
                            "id": "parse",
                            "slice": "all",
                            "metric": "sst_parse_rate",
                            "operator": "gte",
                            "threshold": 1.0,
                            "minimum_cases": 1,
                        }
                    ],
                },
                "judge_aggregation": {
                    "acceptable_rate_minimums": {"test": 0.97, "challenge": 0.95},
                    "rubric_minimums": {
                        "semantic_fidelity": 4.5,
                        "no_additions": 4.5,
                        "no_omissions": 4.5,
                        "spelling": 4.2,
                        "grammar": 4.2,
                        "punctuation": 4.2,
                        "paragraph_structure": 4.2,
                        "heading_structure": 4.2,
                        "numbered_lists": 4.2,
                        "unit_normalization": 4.2,
                        "legal_citations": 4.2,
                        "address_pronouns": 4.2,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_cli_evidence(
    tmp_path: Path,
    *,
    sol_label: str,
) -> dict[str, Path]:
    bundle, references, plan = _bundle()
    terra, terra_manifest, sol, sol_manifest, _ = _initial_evidence(
        bundle,
        plan,
        sol_label=sol_label,
    )
    paths = {
        name: tmp_path / filename
        for name, filename in {
            "bundle": "bundle.jsonl",
            "references": "references.jsonl",
            "reference_manifest": "references.manifest.json",
            "private": "private.json",
            "manifest": "manifest.json",
            "plan": "materialization.json",
            "terra": "terra.jsonl",
            "terra_manifest": "terra-manifest.json",
            "sol": "sol.jsonl",
            "sol_manifest": "sol-manifest.json",
            "config": "evaluation.yaml",
            "output": "aggregation.json",
        }.items()
    }
    paths["bundle"].write_bytes("".join(_canonical(case) + "\n" for case in bundle.cases).encode())
    paths["references"].write_bytes("".join(_canonical(reference) + "\n" for reference in references).encode())
    reference_sha256 = hashlib.sha256(paths["references"].read_bytes()).hexdigest()
    reference_case_ids_sha256 = hashlib.sha256(
        (_canonical([reference["case_id"] for reference in references]) + "\n").encode()
    ).hexdigest()
    reference_manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_reference_bundle",
        "output_filename": paths["references"].name,
        "record_count": len(references),
        "records_sha256": reference_sha256,
        "case_ids_sha256": reference_case_ids_sha256,
        "split_counts": {
            split: sum(reference["split"] == split for reference in references)
            for split in sorted({reference["split"] for reference in references})
        },
        "inputs": [
            {
                "position": 0,
                "records_filename": "source.references.jsonl",
                "records_sha256": reference_sha256,
                "record_count": len(references),
                "case_ids_sha256": reference_case_ids_sha256,
                "manifest_filename": "source.manifest.json",
                "manifest_sha256": "a" * 64,
            }
        ],
    }
    paths["reference_manifest"].write_bytes((_canonical(reference_manifest) + "\n").encode())
    paths["plan"].write_bytes((_canonical(plan) + "\n").encode())
    digest = judge_bundle_sha256(bundle.cases)
    reference_manifest_sha256 = hashlib.sha256(paths["reference_manifest"].read_bytes()).hexdigest()
    plan_sha256 = judge_materialization_plan_sha256(plan)
    private_payload = {
        "schema_version": 2,
        "bundle_sha256": digest,
        "blind_seed": 270031,
        "comparison_kind": "fine_tune_vs_base",
        "candidate_labels": ["candidate_x", "candidate_y"],
        "candidate_roles": {
            "candidate_x": "fine_tune",
            "candidate_y": "base_model",
        },
        "reference_sha256": reference_sha256,
        "reference_manifest_sha256": reference_manifest_sha256,
        "coverage_policy_sha256": plan["coverage_policy_sha256"],
        "materialization_plan_sha256": plan_sha256,
        "candidate_prediction_sha256": {
            "candidate_x": "b" * 64,
            "candidate_y": "c" * 64,
        },
        "cases": bundle.private_mapping,
    }
    private_bytes = (_canonical(private_payload) + "\n").encode()
    paths["private"].write_bytes(private_bytes)
    swapped_count = sum(mapping["position_variant"] == "swapped" for mapping in bundle.private_mapping.values())
    public_manifest = {
        "schema_version": 2,
        "bundle_case_schema_version": 1,
        "reference_sha256": reference_sha256,
        "reference_manifest_sha256": reference_manifest_sha256,
        "base_case_count": len(references),
        "public_case_count": len(bundle.cases),
        "swapped_duplicate_count": swapped_count,
        "swapped_source_fraction": swapped_count / len(references),
        "bundle_sha256": digest,
        "private_mapping_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "coverage_policy_sha256": plan["coverage_policy_sha256"],
        "materialization_plan_sha256": plan_sha256,
        "contains_candidate_identity": False,
        "contains_model_or_checkpoint_identity": False,
        "contains_training_history": False,
        "contains_automatic_aggregate_scores": False,
        "hard_deterministic_errors_are_binding": True,
    }
    paths["manifest"].write_bytes((_canonical(public_manifest) + "\n").encode())
    paths["terra"].write_bytes("".join(_canonical(row) + "\n" for row in terra).encode())
    paths["terra_manifest"].write_bytes((_canonical(terra_manifest) + "\n").encode())
    paths["sol"].write_bytes("".join(_canonical(row) + "\n" for row in sol).encode())
    paths["sol_manifest"].write_bytes((_canonical(sol_manifest) + "\n").encode())
    _evaluation_config(paths["config"])
    return paths


def _run_cli(project_root: Path, paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "aggregate_judges.py"),
            "--bundle",
            str(paths["bundle"]),
            "--references",
            str(paths["references"]),
            "--reference-manifest",
            str(paths["reference_manifest"]),
            "--manifest",
            str(paths["manifest"]),
            "--private-mapping",
            str(paths["private"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--terra-verdicts",
            str(paths["terra"]),
            "--terra-output-manifest",
            str(paths["terra_manifest"]),
            "--sol-verdicts",
            str(paths["sol"]),
            "--sol-output-manifest",
            str(paths["sol_manifest"]),
            "--config",
            str(paths["config"]),
            "--release-candidate",
            "candidate_x",
            "--output",
            str(paths["output"]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_judge_aggregation_cli_verifies_all_hashes_sessions_and_outputs(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_x")

    completed = _run_cli(project_root, paths)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["evaluation_split"] == "combined"
    assert report["comparison_kind"] == "fine_tune_vs_base"
    assert report["exact_required_stage_coverage"] is True
    assert report["materialized_mandatory_coverage"] is True
    assert report["comparison_gate_passed"] is True
    assert report["release_gate_passed"] is False
    assert set(report["judge_session_ids"]) == {"terra_prejudge", "sol_release"}
    assert set(report["verdict_output_sha256s"]) == {
        "terra_prejudge",
        "sol_release",
    }

    sol_manifest = json.loads(paths["sol_manifest"].read_text(encoding="utf-8"))
    tampered_sol_manifest = deepcopy(sol_manifest)
    tampered_sol_manifest["verdict_output_sha256"] = "0" * 64
    paths["sol_manifest"].write_bytes((_canonical(tampered_sol_manifest) + "\n").encode())
    tampered = _run_cli(project_root, paths)
    assert tampered.returncode != 0
    assert "verdict bytes differ from output manifest" in tampered.stderr

    paths["sol_manifest"].write_bytes((_canonical(sol_manifest) + "\n").encode())
    aliased_paths = dict(paths)
    aliased_paths["output"] = paths["private"]
    aliased = _run_cli(project_root, aliased_paths)
    assert aliased.returncode != 0
    assert "output must not overwrite an input artifact" in aliased.stderr


def test_campaign_requires_both_comparisons_and_independent_sessions(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_x")
    completed = _run_cli(project_root, paths)
    assert completed.returncode == 0, completed.stderr
    base_report = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert base_report["release_candidate_prediction_sha256"] == "b" * 64
    assert base_report["opponent_role"] == "base_model"
    assert base_report["opponent_prediction_sha256"] == "c" * 64
    rule_report = deepcopy(base_report)
    rule_report["comparison_kind"] = "fine_tune_vs_rule_baseline"
    rule_report["opponent_role"] = "rule_baseline"
    rule_report["opponent_prediction_sha256"] = "d" * 64
    rule_report["judge_session_ids"] = {
        stage: f"{session}_rule" for stage, session in rule_report["judge_session_ids"].items()
    }
    for comparison in (base_report, rule_report):
        assert comparison["comparison_gate_passed"] is True
        assert comparison["release_gate_passed"] is False

    campaign = aggregate_judge_campaign_reports(
        [base_report, rule_report],
        aggregation_sha256s={
            "fine_tune_vs_base": "a" * 64,
            "fine_tune_vs_rule_baseline": "b" * 64,
        },
    )

    assert campaign["status"] == "complete"
    assert campaign["release_gate_passed"] is True
    assert campaign["required_comparison_kinds"] == [
        "fine_tune_vs_base",
        "fine_tune_vs_rule_baseline",
    ]
    assert campaign["independent_sessions"] is True
    assert campaign["release_candidate_prediction_sha256"] == "b" * 64
    assert campaign["comparisons"]["fine_tune_vs_rule_baseline"]["opponent_role"] == "rule_baseline"
    assert campaign["comparisons"]["fine_tune_vs_rule_baseline"]["opponent_prediction_sha256"] == "d" * 64

    base_report_path = tmp_path / "base-comparison.json"
    rule_report_path = tmp_path / "rule-comparison.json"
    base_report_path.write_text(
        json.dumps(base_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rule_report_path.write_text(
        json.dumps(rule_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    aliased_campaign = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "aggregate_judge_campaign.py"),
            "--comparison-aggregation",
            str(base_report_path),
            "--comparison-aggregation",
            str(rule_report_path),
            "--output",
            str(base_report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert aliased_campaign.returncode != 0
    assert "output must not overwrite a comparison input" in aliased_campaign.stderr

    reused_sessions = deepcopy(rule_report)
    reused_sessions["judge_session_ids"] = deepcopy(base_report["judge_session_ids"])
    blocked = aggregate_judge_campaign_reports(
        [base_report, reused_sessions],
        aggregation_sha256s={
            "fine_tune_vs_base": "c" * 64,
            "fine_tune_vs_rule_baseline": "d" * 64,
        },
    )
    assert blocked["status"] == "blocked"
    assert blocked["release_gate_passed"] is False
    assert blocked["independent_sessions"] is False

    mismatched_candidate = deepcopy(rule_report)
    mismatched_candidate["release_candidate_prediction_sha256"] = "f" * 64
    with pytest.raises(
        ValueError,
        match="release_candidate_prediction_sha256",
    ):
        aggregate_judge_campaign_reports(
            [base_report, mismatched_candidate],
            aggregation_sha256s={
                "fine_tune_vs_base": "1" * 64,
                "fine_tune_vs_rule_baseline": "2" * 64,
            },
        )

    wrong_opponent_role = deepcopy(rule_report)
    wrong_opponent_role["opponent_role"] = "base_model"
    with pytest.raises(ValueError, match="opponent role"):
        aggregate_judge_campaign_reports(
            [base_report, wrong_opponent_role],
            aggregation_sha256s={
                "fine_tune_vs_base": "3" * 64,
                "fine_tune_vs_rule_baseline": "4" * 64,
            },
        )

    with pytest.raises(ValueError, match="exactly two"):
        aggregate_judge_campaign_reports(
            [base_report],
            aggregation_sha256s={"fine_tune_vs_base": "e" * 64},
        )


def test_judge_cli_writes_required_tie_break_ids_before_returning_pending(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_y")

    completed = _run_cli(project_root, paths)

    assert completed.returncode == 3, completed.stderr
    report = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert report["status"] == "pending_tiebreak"
    assert report["required_tie_break_case_ids"]
    assert report["missing_tie_break_case_ids"] == report["required_tie_break_case_ids"]
    assert report["release_gate_passed"] is False
    assert report["exact_required_stage_coverage"] is False


def test_stage_packet_cli_materializes_exact_anonymous_terra_assignment(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_x")
    prompt_path = tmp_path / "terra-prompt.md"
    packet_path = tmp_path / "terra-stage.jsonl"
    packet_manifest_path = tmp_path / "terra-stage.manifest.json"
    prompt_path.write_text("Review only the attached anonymous cases.\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "terra_prejudge",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--prompt",
            str(prompt_path),
            "--output",
            str(packet_path),
            "--output-manifest",
            str(packet_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(paths["plan"].read_text(encoding="utf-8"))
    packet_rows = [json.loads(line) for line in packet_path.read_text(encoding="utf-8").splitlines()]
    packet_manifest = json.loads(packet_manifest_path.read_text(encoding="utf-8"))
    assert {row["case_id"] for row in packet_rows} == set(
        plan["stage_assignments"]["terra_prejudge"]["public_case_ids"]
    )
    assert packet_manifest["judge_stage"] == "terra_prejudge"
    assert packet_manifest["reviewed_bundle_sha256"] == plan["bundle_sha256"]
    assert packet_manifest["prompt_hash"] == "sha256:" + hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    public_bytes = packet_path.read_bytes() + packet_manifest_path.read_bytes()
    assert b"candidate_x" not in public_bytes
    assert b"candidate_y" not in public_bytes

    batch_path = tmp_path / "terra-stage-batch.jsonl"
    batch_manifest_path = tmp_path / "terra-stage-batch.manifest.json"
    batched = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "terra_prejudge",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--prompt",
            str(prompt_path),
            "--max-cases",
            "2",
            "--output",
            str(batch_path),
            "--output-manifest",
            str(batch_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert batched.returncode == 0, batched.stderr
    batch_manifest = json.loads(batch_manifest_path.read_text(encoding="utf-8"))
    assert batch_manifest["assigned_public_case_count"] == 2
    assert batch_manifest["stage_assignment_public_case_count"] == len(
        plan["stage_assignments"]["terra_prejudge"]["public_case_ids"]
    )


def test_stage_packet_cli_derives_dynamic_sol_assignment_from_bound_terra_output(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_x")
    prompt_path = tmp_path / "sol-prompt.md"
    packet_path = tmp_path / "sol-stage.jsonl"
    packet_manifest_path = tmp_path / "sol-stage.manifest.json"
    prompt_path.write_text("Review only the attached anonymous cases.\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "sol_release",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--private-mapping",
            str(paths["private"]),
            "--terra-verdicts",
            str(paths["terra"]),
            "--terra-output-manifest",
            str(paths["terra_manifest"]),
            "--prompt",
            str(prompt_path),
            "--output",
            str(packet_path),
            "--output-manifest",
            str(packet_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    packet_rows = [json.loads(line) for line in packet_path.read_text(encoding="utf-8").splitlines()]
    packet_manifest = json.loads(packet_manifest_path.read_text(encoding="utf-8"))
    private_payload = json.loads(paths["private"].read_text(encoding="utf-8"))
    plan = json.loads(paths["plan"].read_text(encoding="utf-8"))
    terra = [json.loads(line) for line in paths["terra"].read_text(encoding="utf-8").splitlines()]
    terra_manifest = json.loads(paths["terra_manifest"].read_text(encoding="utf-8"))
    assignment = materialize_sol_release_assignment(
        [json.loads(line) for line in paths["bundle"].read_text(encoding="utf-8").splitlines()],
        private_payload["cases"],
        plan,
        terra,
        terra_manifest,
    )
    assert {row["case_id"] for row in packet_rows} == set(assignment["public_case_ids"])
    assert packet_manifest["judge_stage"] == "sol_release"
    assert packet_manifest["stage_assignment_sha256"] == assignment["assignment_sha256"]
    public_bytes = packet_path.read_bytes() + packet_manifest_path.read_bytes()
    assert b"candidate_x" not in public_bytes
    assert b"candidate_y" not in public_bytes


def test_stage_packet_cli_materializes_only_pending_sol_tiebreak_cases(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_y")
    pending = _run_cli(project_root, paths)
    assert pending.returncode == 3, pending.stderr
    aggregation = json.loads(paths["output"].read_text(encoding="utf-8"))
    prompt_path = tmp_path / "tie-prompt.md"
    packet_path = tmp_path / "tie-stage.jsonl"
    packet_manifest_path = tmp_path / "tie-stage.manifest.json"
    prompt_path.write_text("Resolve only the attached anonymous ties.\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "sol_tiebreak",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--aggregation-report",
            str(paths["output"]),
            "--prompt",
            str(prompt_path),
            "--output",
            str(packet_path),
            "--output-manifest",
            str(packet_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    packet_ids = {json.loads(line)["case_id"] for line in packet_path.read_text(encoding="utf-8").splitlines()}
    assert packet_ids == set(aggregation["missing_tie_break_case_ids"])
    packet_manifest = json.loads(packet_manifest_path.read_text(encoding="utf-8"))
    assert packet_manifest["judge_stage"] == "sol_tiebreak"
    assert packet_manifest["model_family"] == "gpt-5.6-sol"
    assert packet_manifest["reasoning_effort"] == "ultra"

    packet_rows = [json.loads(line) for line in packet_path.read_text(encoding="utf-8").splitlines()]
    assert len(packet_rows) > 1
    first_tie_verdict = {
        "schema_version": 2,
        "case_id": packet_rows[0]["case_id"],
        "judge_id": "judge_sol_ultra_resume_01",
        "judge_session_id": "session_sol_ultra_resume_01",
        "judge_stage": "sol_tiebreak",
        "model_family": "gpt-5.6-sol",
        "reasoning_effort": "ultra",
        "prompt_hash": packet_manifest["prompt_hash"],
        "reviewed_bundle_sha256": packet_manifest["reviewed_bundle_sha256"],
        "stage_assignment_sha256": packet_manifest["stage_assignment_sha256"],
        "candidate": "A",
        "acceptable": True,
        "critical_error": False,
        "scores": dict.fromkeys(JUDGE_SCORE_DIMENSIONS, 5),
        "flags": [],
        "brief_reason": "Die gewählte Ausgabe ist im direkten Vergleich vorzuziehen.",
    }
    tie_fragment_path = tmp_path / "tie-fragment.jsonl"
    tie_merged_path = tmp_path / "tie-merged.jsonl"
    tie_resume_path = tmp_path / "tie-resume.json"
    tie_output_manifest_path = tmp_path / "tie-output.manifest.json"
    tie_fragment_path.write_bytes((_canonical(first_tie_verdict) + "\n").encode("utf-8"))
    partial = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "finalize_judge_stage.py"),
            "--stage-packet",
            str(packet_path),
            "--stage-packet-manifest",
            str(packet_manifest_path),
            "--verdict-fragment",
            str(tie_fragment_path),
            "--output",
            str(tie_merged_path),
            "--resume-manifest",
            str(tie_resume_path),
            "--output-manifest",
            str(tie_output_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert partial.returncode == 3, partial.stderr

    resumed_packet_path = tmp_path / "tie-resumed-stage.jsonl"
    resumed_manifest_path = tmp_path / "tie-resumed-stage.manifest.json"
    resumed_packet = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "sol_tiebreak",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--aggregation-report",
            str(paths["output"]),
            "--resume-manifest",
            str(tie_resume_path),
            "--prompt",
            str(prompt_path),
            "--output",
            str(resumed_packet_path),
            "--output-manifest",
            str(resumed_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert resumed_packet.returncode == 0, resumed_packet.stderr
    tie_resume = json.loads(tie_resume_path.read_text(encoding="utf-8"))
    resumed_ids = {json.loads(line)["case_id"] for line in resumed_packet_path.read_text(encoding="utf-8").splitlines()}
    assert resumed_ids == set(tie_resume["missing_case_ids"])


def test_stage_verdict_cli_resumes_batches_and_emits_final_manifest_only_when_complete(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    paths = _write_cli_evidence(tmp_path, sol_label="candidate_x")
    prompt_path = tmp_path / "terra-prompt.md"
    packet_path = tmp_path / "terra-stage.jsonl"
    packet_manifest_path = tmp_path / "terra-stage.manifest.json"
    prompt_path.write_text("Review only the attached anonymous cases.\n", encoding="utf-8")
    packet_build = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "terra_prejudge",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--prompt",
            str(prompt_path),
            "--output",
            str(packet_path),
            "--output-manifest",
            str(packet_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert packet_build.returncode == 0, packet_build.stderr

    terra_rows = [json.loads(line) for line in paths["terra"].read_text(encoding="utf-8").splitlines()]
    packet_manifest = json.loads(packet_manifest_path.read_text(encoding="utf-8"))
    for verdict in terra_rows:
        verdict["prompt_hash"] = packet_manifest["prompt_hash"]
    split_at = max(1, len(terra_rows) // 2)
    first_fragment = tmp_path / "terra-fragment-1.jsonl"
    second_fragment = tmp_path / "terra-fragment-2.jsonl"
    first_fragment.write_bytes("".join(_canonical(row) + "\n" for row in terra_rows[:split_at]).encode())
    second_fragment.write_bytes("".join(_canonical(row) + "\n" for row in terra_rows[split_at:]).encode())
    merged_path = tmp_path / "terra-merged.jsonl"
    resume_path = tmp_path / "terra-resume.json"
    final_manifest_path = tmp_path / "terra-output.manifest.json"
    common = [
        sys.executable,
        str(project_root / "scripts" / "finalize_judge_stage.py"),
        "--stage-packet",
        str(packet_path),
        "--stage-packet-manifest",
        str(packet_manifest_path),
        "--output",
        str(merged_path),
        "--resume-manifest",
        str(resume_path),
        "--output-manifest",
        str(final_manifest_path),
    ]

    partial = subprocess.run(
        [*common, "--verdict-fragment", str(first_fragment)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert partial.returncode == 3, partial.stderr
    partial_resume = json.loads(resume_path.read_text(encoding="utf-8"))
    assert partial_resume["status"] == "in_progress"
    assert partial_resume["completed_count"] == split_at
    assert partial_resume["missing_count"] == len(terra_rows) - split_at
    assert not final_manifest_path.exists()

    next_packet_path = tmp_path / "terra-next-stage.jsonl"
    next_packet_manifest_path = tmp_path / "terra-next-stage.manifest.json"
    next_packet = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_stage_packet.py"),
            "--stage",
            "terra_prejudge",
            "--bundle",
            str(paths["bundle"]),
            "--bundle-manifest",
            str(paths["manifest"]),
            "--materialization-plan",
            str(paths["plan"]),
            "--resume-manifest",
            str(resume_path),
            "--prompt",
            str(prompt_path),
            "--output",
            str(next_packet_path),
            "--output-manifest",
            str(next_packet_manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert next_packet.returncode == 0, next_packet.stderr
    next_packet_ids = {
        json.loads(line)["case_id"] for line in next_packet_path.read_text(encoding="utf-8").splitlines()
    }
    assert next_packet_ids == set(partial_resume["missing_case_ids"])
    next_manifest = json.loads(next_packet_manifest_path.read_text(encoding="utf-8"))
    assert next_manifest["assigned_public_case_count"] == partial_resume["missing_count"]
    assert next_manifest["stage_assignment_public_case_count"] == len(terra_rows)

    complete = subprocess.run(
        [
            *common,
            "--existing-verdicts",
            str(merged_path),
            "--verdict-fragment",
            str(second_fragment),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert complete.returncode == 0, complete.stderr
    complete_resume = json.loads(resume_path.read_text(encoding="utf-8"))
    assert complete_resume["status"] == "complete"
    assert complete_resume["missing_count"] == 0
    final_manifest = json.loads(final_manifest_path.read_text(encoding="utf-8"))
    assert final_manifest["verdict_count"] == len(terra_rows)
    assert final_manifest["verdict_output_sha256"] == hashlib.sha256(merged_path.read_bytes()).hexdigest()

    idempotent_finalize = subprocess.run(
        [*common, "--existing-verdicts", str(merged_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert idempotent_finalize.returncode == 0, idempotent_finalize.stderr
    assert json.loads(final_manifest_path.read_text(encoding="utf-8")) == final_manifest

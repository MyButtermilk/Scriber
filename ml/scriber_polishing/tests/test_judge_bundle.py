import hashlib
import json
import math
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.judge_bundle import (
    build_blinded_judge_bundle,
    judge_materialization_plan_sha256,
    load_mandatory_judge_coverage_policy,
    materialize_judge_coverage,
    materialize_judge_stage_packet,
    validate_judge_bundle_case,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _reference(index: int) -> dict[str, object]:
    target_sst = f"[DOC]\n[P]Bitte bis {index:02d}.08.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": f"eval_case_{index:03d}",
        "split": "challenge" if index == 3 else "test",
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
        "slice_tags": ["dates"],
        "is_identity": False,
    }


def _prediction(reference: dict[str, object], sst: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": reference["case_id"],
        "prediction_sst": sst or reference["target_sst"],
    }


def test_pairwise_bundle_randomizes_labels_and_emits_swapped_duplicates_for_at_least_half() -> None:
    references = [_reference(index) for index in range(1, 4)]
    candidate_x = [_prediction(reference) for reference in references]
    candidate_y = [_prediction(reference) for reference in references]
    candidate_y[0] = _prediction(
        references[0],
        "[DOC]\n[P]Bitte bis 09.08.2026 antworten.[/P]\n[/DOC]",
    )

    first = build_blinded_judge_bundle(
        references,
        candidate_x,
        candidate_y,
        candidate_x_label="fine_tune_secret",
        candidate_y_label="base_model_secret",
        blind_seed=270_030,
    )
    second = build_blinded_judge_bundle(
        references,
        candidate_x,
        candidate_y,
        candidate_x_label="fine_tune_secret",
        candidate_y_label="base_model_secret",
        blind_seed=270_030,
    )

    assert first == second
    assert len(first.cases) == len(references) + math.ceil(len(references) / 2)
    assert sum(item["position_variant"] == "swapped" for item in first.private_mapping.values()) == 2
    assert set(first.private_mapping) == {case["case_id"] for case in first.cases}
    for case in first.cases:
        assert validate_judge_bundle_case(case) == case
        serialized = json.dumps(case, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "fine_tune_secret",
            "base_model_secret",
            "checkpoint",
            "training_method",
            "training_logs",
            "eval_loss",
            "aggregate_score",
            "candidate_identity",
        ):
            assert forbidden not in serialized
        assert case["rubric"]["hard_deterministic_errors_are_binding"] is True

    for case_id, mapping in first.private_mapping.items():
        public_case = next(case for case in first.cases if case["case_id"] == case_id)
        assert mapping["candidate_outputs_identical"] is (public_case["candidate_a"] == public_case["candidate_b"])
        if mapping["position_variant"] != "swapped":
            continue
        primary_mapping = next(
            item
            for item in first.private_mapping.values()
            if item["base_case_id"] == mapping["base_case_id"] and item["position_variant"] == "primary"
        )
        primary = next(case for case in first.cases if case["case_id"] == primary_mapping["public_case_id"])
        swapped = next(case for case in first.cases if case["case_id"] == case_id)
        assert swapped["candidate_a"] == primary["candidate_b"]
        assert swapped["candidate_b"] == primary["candidate_a"]
        assert mapping["candidate_a_label"] == primary_mapping["candidate_b_label"]
        assert mapping["candidate_b_label"] == primary_mapping["candidate_a_label"]

    unsafe_public_cases = [
        case
        for case in first.cases
        if case["deterministic_critical_flags"]["A"] or case["deterministic_critical_flags"]["B"]
    ]
    assert unsafe_public_cases
    assert any(
        "changed_date_or_time"
        in [*case["deterministic_critical_flags"]["A"], *case["deterministic_critical_flags"]["B"]]
        for case in unsafe_public_cases
    )


def test_pairwise_bundle_rejects_incomplete_candidates_duplicate_labels_and_sub_half_swaps() -> None:
    references = [_reference(1), _reference(2)]
    predictions = [_prediction(reference) for reference in references]

    with pytest.raises(ValueError, match="missing candidate prediction"):
        build_blinded_judge_bundle(
            references,
            predictions,
            predictions[:1],
            candidate_x_label="candidate_x",
            candidate_y_label="candidate_y",
            blind_seed=1,
        )

    with pytest.raises(ValueError, match="labels must be distinct"):
        build_blinded_judge_bundle(
            references,
            predictions,
            predictions,
            candidate_x_label="same_label",
            candidate_y_label="same_label",
            blind_seed=1,
        )

    with pytest.raises(ValueError, match="at least 0.5"):
        build_blinded_judge_bundle(
            references,
            predictions,
            predictions,
            candidate_x_label="candidate_x",
            candidate_y_label="candidate_y",
            blind_seed=1,
            swapped_fraction=0.49,
        )


def test_judge_bundle_schema_is_closed() -> None:
    reference = _reference(1)
    result = build_blinded_judge_bundle(
        [reference],
        [_prediction(reference)],
        [_prediction(reference)],
        candidate_x_label="candidate_x",
        candidate_y_label="candidate_y",
        blind_seed=1,
    )
    invalid = deepcopy(result.cases[0])
    invalid["model_name"] = "leak"

    with pytest.raises(ValueError, match="judge bundle case schema"):
        validate_judge_bundle_case(invalid)

    invalid_reference = deepcopy(result.cases[0])
    invalid_reference["reference_ast"] = {}
    with pytest.raises(ValueError, match="reference AST"):
        validate_judge_bundle_case(invalid_reference)


def _coverage_policy(*, regular_test_minimum: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "selection_seed": 270036,
        "regular_test_minimum": regular_test_minimum,
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
                "id": "dates",
                "minimum": 1,
                "any_slice_tags": ["dates"],
            }
        ],
    }


def test_materialized_coverage_is_seeded_complete_and_candidate_anonymous() -> None:
    references = [_reference(index) for index in range(1, 5)]
    for reference, split in zip(
        references,
        ("test", "ai_gold_test", "ai_gold_challenge", "regression"),
        strict=True,
    ):
        reference["split"] = split
        reference["slice_tags"] = ["dates", "generator_family:test"]
    predictions = [_prediction(reference) for reference in references]
    bundle = build_blinded_judge_bundle(
        references,
        predictions,
        predictions,
        candidate_x_label="fine_tune_secret",
        candidate_y_label="base_model_secret",
        blind_seed=270032,
    )

    first = materialize_judge_coverage(references, bundle, _coverage_policy())
    second = materialize_judge_coverage(references, bundle, _coverage_policy())

    assert first == second
    assert first["coverage_passed"] is True
    assert first["coverage"]["deficits"] == []
    assert first["coverage"]["strata"]["regular_test"]["available"] == 1
    assert first["coverage"]["strata"]["ai_gold_test"]["selected"] == 1
    assert first["coverage"]["strata"]["ai_gold_challenge"]["selected"] == 1
    assert first["coverage"]["strata"]["critical_regression"]["selected"] == 1
    assert set(first["primary_stage_by_base_case"]) == {reference["case_id"] for reference in references}
    terra_primary = {
        case_id for case_id, stage in first["primary_stage_by_base_case"].items() if stage == "terra_prejudge"
    }
    sol_primary = {case_id for case_id, stage in first["primary_stage_by_base_case"].items() if stage == "sol_release"}
    assert terra_primary.isdisjoint(sol_primary)
    assert terra_primary | sol_primary == set(first["primary_stage_by_base_case"])
    assert len(judge_materialization_plan_sha256(first)) == 64
    serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "fine_tune_secret" not in serialized
    assert "base_model_secret" not in serialized


def test_stage_packet_is_anonymous_and_hash_bound_to_exact_assignment() -> None:
    references = [_reference(index) for index in range(1, 5)]
    for reference, split in zip(
        references,
        ("test", "ai_gold_test", "ai_gold_challenge", "regression"),
        strict=True,
    ):
        reference["split"] = split
        reference["slice_tags"] = ["dates", "generator_family:test"]
    predictions = [_prediction(reference) for reference in references]
    bundle = build_blinded_judge_bundle(
        references,
        predictions,
        predictions,
        candidate_x_label="fine_tune_secret",
        candidate_y_label="base_model_secret",
        blind_seed=270_032,
    )
    plan = materialize_judge_coverage(references, bundle, _coverage_policy())
    assignment = plan["stage_assignments"]["terra_prejudge"]

    packet = materialize_judge_stage_packet(
        bundle.cases,
        judge_stage="terra_prejudge",
        public_case_ids=assignment["public_case_ids"],
        assignment_sha256=assignment["assignment_sha256"],
        prompt_hash="sha256:" + "d" * 64,
    )

    assert {case["case_id"] for case in packet.cases} == set(assignment["public_case_ids"])
    assert packet.manifest["reviewed_bundle_sha256"] == plan["bundle_sha256"]
    assert packet.manifest["stage_assignment_sha256"] == assignment["assignment_sha256"]
    assert packet.manifest["assigned_public_case_count"] == len(assignment["public_case_ids"])
    assert packet.manifest["model_family"] == "gpt-5.6-terra"
    assert packet.manifest["reasoning_effort"] == "max"
    public_payload = json.dumps(
        {"cases": packet.cases, "manifest": packet.manifest},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "fine_tune_secret" not in public_payload
    assert "base_model_secret" not in public_payload

    with pytest.raises(ValueError, match="assignment hash"):
        materialize_judge_stage_packet(
            bundle.cases,
            judge_stage="terra_prejudge",
            public_case_ids=assignment["public_case_ids"],
            assignment_sha256="0" * 64,
            prompt_hash="sha256:" + "d" * 64,
        )

    remaining_packet = materialize_judge_stage_packet(
        bundle.cases,
        judge_stage="terra_prejudge",
        public_case_ids=assignment["public_case_ids"][:1],
        assignment_public_case_ids=assignment["public_case_ids"],
        assignment_sha256=assignment["assignment_sha256"],
        prompt_hash="sha256:" + "d" * 64,
    )
    assert len(remaining_packet.cases) == 1
    assert remaining_packet.manifest["assigned_public_case_count"] == 1
    assert remaining_packet.manifest["stage_assignment_public_case_count"] == len(assignment["public_case_ids"])


def test_repository_judge_policy_freezes_all_mandatory_release_strata() -> None:
    policy = load_mandatory_judge_coverage_policy()

    assert policy["regular_test_minimum"] == 1_000
    assert policy["ai_gold_test_expected_count"] == 1_000
    assert policy["ai_gold_challenge_expected_count"] == 750
    assert policy["critical_regression_expected_count"] == 500
    assert policy["terra_accepted_sample_fraction"] == 0.25
    assert set(policy["required_comparison_kinds"]) == {
        "fine_tune_vs_base",
        "fine_tune_vs_rule_baseline",
    }
    quota_ids = {quota["id"] for quota in policy["category_quotas"]}
    assert {
        "identity",
        "protected_spans",
        "legal_hierarchy",
        "speaker_labels",
        "timestamps",
    }.issubset(quota_ids)


def test_judge_bundle_cli_fails_closed_with_a_materialized_deficit_report(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    reference = _reference(1)
    references_path = tmp_path / "references.jsonl"
    reference_manifest_path = tmp_path / "references.manifest.json"
    candidate_x_path = tmp_path / "candidate-x.jsonl"
    candidate_y_path = tmp_path / "candidate-y.jsonl"
    bundle_path = tmp_path / "bundle.jsonl"
    manifest_path = tmp_path / "manifest.json"
    private_path = tmp_path / "private.json"
    materialization_path = tmp_path / "materialization.json"
    references_path.write_text(json.dumps(reference, ensure_ascii=False) + "\n", encoding="utf-8")
    reference_hash = hashlib.sha256(references_path.read_bytes()).hexdigest()
    case_ids_hash = hashlib.sha256(
        (
            json.dumps(
                [reference["case_id"]],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    ).hexdigest()
    reference_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "scriber_evaluation_reference_bundle",
                "output_filename": references_path.name,
                "record_count": 1,
                "records_sha256": reference_hash,
                "case_ids_sha256": case_ids_hash,
                "split_counts": {"test": 1},
                "inputs": [
                    {
                        "position": 0,
                        "records_filename": "source.references.jsonl",
                        "records_sha256": reference_hash,
                        "record_count": 1,
                        "case_ids_sha256": case_ids_hash,
                        "manifest_filename": "source.manifest.json",
                        "manifest_sha256": "a" * 64,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    candidate_x_path.write_text(
        json.dumps(_prediction(reference), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    candidate_y_path.write_text(
        json.dumps(_prediction(reference), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_bundle.py"),
            "--references",
            str(references_path),
            "--reference-manifest",
            str(reference_manifest_path),
            "--candidate",
            f"fine_tune_secret={candidate_x_path}",
            "--candidate",
            f"base_model_secret={candidate_y_path}",
            "--output",
            str(bundle_path),
            "--manifest",
            str(manifest_path),
            "--private-mapping",
            str(private_path),
            "--materialization-plan",
            str(materialization_path),
            "--comparison-kind",
            "fine_tune_vs_base",
            "--fine-tune-label",
            "fine_tune_secret",
            "--blind-seed",
            "270032",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "judge_bundle=blocked" in completed.stderr
    assert not bundle_path.exists()
    assert not manifest_path.exists()
    assert not private_path.exists()
    materialization = json.loads(materialization_path.read_text(encoding="utf-8"))
    assert materialization["coverage_passed"] is False
    assert any(
        deficit.startswith("stratum:regular_test:required=1000:available=1")
        for deficit in materialization["coverage"]["deficits"]
    )
    public_payload = materialization_path.read_text(encoding="utf-8")
    assert "fine_tune_secret" not in public_payload
    assert "base_model_secret" not in public_payload

    aliased = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_judge_bundle.py"),
            "--references",
            str(references_path),
            "--reference-manifest",
            str(reference_manifest_path),
            "--candidate",
            f"fine_tune_secret={candidate_x_path}",
            "--candidate",
            f"base_model_secret={candidate_y_path}",
            "--output",
            str(bundle_path),
            "--manifest",
            str(manifest_path),
            "--private-mapping",
            str(bundle_path),
            "--materialization-plan",
            str(materialization_path),
            "--comparison-kind",
            "fine_tune_vs_base",
            "--fine-tune-label",
            "fine_tune_secret",
            "--blind-seed",
            "270032",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert aliased.returncode != 0
    assert "output paths must be distinct" in aliased.stderr

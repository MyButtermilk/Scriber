import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.judge_aggregation import judge_bundle_sha256, judge_verdicts_sha256
from scriber_polishing.judge_bundle import (
    JUDGE_SCORE_DIMENSIONS,
    build_blinded_judge_bundle,
    judge_coverage_policy_sha256,
    judge_materialization_plan_sha256,
    judge_stage_assignment_sha256,
    materialize_judge_coverage,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "materialize_sol_followup.py"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.write_bytes((_canonical(value) + "\n").encode("utf-8"))


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_bytes(("\n".join(_canonical(row) for row in rows) + "\n").encode("utf-8"))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reference(index: int, split: str, *, identity: bool = False) -> dict[str, object]:
    target_sst = f"[DOC]\n[P]Bitte bis {index:02d}.08.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    tags = ["quota_case", "generator_family:synthetic"]
    if split == "regression":
        tags.append("critical_regression")
    return {
        "schema_version": 1,
        "case_id": f"eval_case_{index:03d}",
        "split": split,
        "source_text": f"SENTINEL_SOURCE_{index:03d}",
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
        "is_identity": identity,
    }


def _references() -> list[dict[str, object]]:
    splits = [
        "test",
        "test",
        "test",
        "test",
        "challenge",
        "challenge",
        "challenge",
        "challenge",
        "ai_gold_test",
        "ai_gold_challenge",
        "regression",
    ]
    return [_reference(index, split) for index, split in enumerate(splits, start=1)]


def _prediction(reference: dict[str, object], suffix: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": reference["case_id"],
        "prediction_sst": str(reference["target_sst"]).replace("antworten.", f"antworten{suffix}"),
    }


def _coverage_policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "selection_seed": 270036,
        "regular_test_minimum": 4,
        "ai_gold_test_expected_count": 1,
        "ai_gold_challenge_expected_count": 1,
        "critical_regression_expected_count": 1,
        "terra_accepted_sample_fraction": 0.25,
        "generator_family_tag_prefix": "generator_family:",
        "required_generator_families": ["generator_family:synthetic"],
        "required_comparison_kinds": ["fine_tune_vs_base", "fine_tune_vs_rule_baseline"],
        "category_quotas": [
            {"id": "quota_case", "minimum": 11, "any_slice_tags": ["quota_case"]},
        ],
    }


def _verdict(
    *,
    bundle,
    case_id: str,
    assignment_sha256: str,
    final_label: str,
    critical: bool = False,
    acceptable: bool = True,
    score: int = 5,
    flip_selected_position: bool = False,
) -> dict[str, object]:
    mapping = bundle.private_mapping[case_id]
    candidate = "A" if mapping["candidate_a_label"] == final_label else "B"
    if flip_selected_position:
        candidate = "B" if candidate == "A" else "A"
    return {
        "schema_version": 2,
        "case_id": case_id,
        "judge_id": "judge_terra_full_01",
        "judge_session_id": "session_terra_full_run_01",
        "judge_stage": "terra_prejudge",
        "model_family": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "prompt_hash": "sha256:" + "d" * 64,
        "reviewed_bundle_sha256": judge_bundle_sha256(bundle.cases),
        "stage_assignment_sha256": assignment_sha256,
        "candidate": candidate,
        "acceptable": False if critical else acceptable,
        "critical_error": critical,
        "scores": dict.fromkeys(JUDGE_SCORE_DIMENSIONS, score),
        "flags": ["changed_number"] if critical else [],
        "brief_reason": "SENTINEL_REASON_MUST_NOT_LEAK",
    }


def _build_package(root: Path, comparison_kind: str) -> tuple[Path, Path, Path, Path, Path, Path]:
    references = _references()
    final = [_prediction(reference, "!") for reference in references]
    opponent = [_prediction(reference, ".") for reference in references]
    opponent_label = "base_model" if comparison_kind == "fine_tune_vs_base" else "rule_baseline"
    bundle = build_blinded_judge_bundle(
        references,
        final,
        opponent,
        candidate_x_label="final",
        candidate_y_label=opponent_label,
        blind_seed=270031,
    )
    policy = _coverage_policy()
    plan = materialize_judge_coverage(references, bundle, policy)
    assert plan["coverage_passed"] is True
    bundle_path = root / f"{comparison_kind}.bundle.jsonl"
    mapping_path = root / f"{comparison_kind}.private.json"
    bundle_manifest_path = root / f"{comparison_kind}.bundle.manifest.json"
    plan_path = root / f"{comparison_kind}.plan.json"
    verdict_path = root / f"{comparison_kind}.verdicts.jsonl"
    resume_path = root / f"{comparison_kind}.resume.json"
    _write_jsonl(bundle_path, list(bundle.cases))
    bundle_hash = _sha256_bytes(bundle_path.read_bytes())
    assert bundle_hash == judge_bundle_sha256(bundle.cases)
    plan_hash = judge_materialization_plan_sha256(plan)
    mapping = {
        "schema_version": 2,
        "bundle_sha256": bundle_hash,
        "blind_seed": 270031,
        "comparison_kind": comparison_kind,
        "candidate_labels": ["final", opponent_label],
        "candidate_roles": {"final": "fine_tune", opponent_label: opponent_label},
        "reference_sha256": "a" * 64,
        "reference_manifest_sha256": "b" * 64,
        "coverage_policy_sha256": judge_coverage_policy_sha256(policy),
        "materialization_plan_sha256": plan_hash,
        "candidate_prediction_sha256": {"final": "c" * 64, opponent_label: "d" * 64},
        "cases": bundle.private_mapping,
    }
    _write_json(mapping_path, mapping)
    _write_json(plan_path, plan)
    bundle_manifest = {
        "schema_version": 2,
        "bundle_case_schema_version": 1,
        "reference_sha256": "a" * 64,
        "reference_manifest_sha256": "b" * 64,
        "base_case_count": len(references),
        "public_case_count": len(bundle.cases),
        "swapped_duplicate_count": len(bundle.cases) - len(references),
        "swapped_source_fraction": 0.5,
        "bundle_sha256": bundle_hash,
        "private_mapping_sha256": _sha256_bytes(mapping_path.read_bytes()),
        "coverage_policy_sha256": judge_coverage_policy_sha256(policy),
        "materialization_plan_sha256": plan_hash,
        "contains_candidate_identity": False,
        "contains_model_or_checkpoint_identity": False,
        "contains_training_history": False,
        "contains_automatic_aggregate_scores": False,
        "hard_deterministic_errors_are_binding": True,
    }
    _write_json(bundle_manifest_path, bundle_manifest)
    assignment = plan["stage_assignments"]["terra_prejudge"]
    assignment_ids = set(assignment["public_case_ids"])
    assert assignment["assignment_sha256"] == judge_stage_assignment_sha256(
        "terra_prejudge", bundle_hash, assignment["public_case_ids"]
    )
    complete_ids = [case["case_id"] for case in bundle.cases if case["case_id"] in assignment_ids]
    partial_pair = next(
        case_id
        for case_id in complete_ids
        if bundle.private_mapping[case_id]["paired_public_case_id"] in assignment_ids
    )
    omitted = bundle.private_mapping[partial_pair]["paired_public_case_id"]
    verdicts: list[dict[str, object]] = []
    for position, case_id in enumerate(complete_ids):
        if case_id == omitted:
            continue
        verdicts.append(
            _verdict(
                bundle=bundle,
                case_id=case_id,
                assignment_sha256=assignment["assignment_sha256"],
                final_label="final",
                critical=(position == 1),
                acceptable=(position != 2),
                score=4 if position == 3 else 5,
                flip_selected_position=(
                    position == 4 and bundle.private_mapping[case_id]["paired_public_case_id"] != omitted
                ),
            )
        )
    _write_jsonl(verdict_path, verdicts)
    missing = [
        case_id for case_id in assignment["public_case_ids"] if case_id not in {row["case_id"] for row in verdicts}
    ]
    resume = {
        "schema_version": 1,
        "status": "in_progress",
        "judge_stage": "terra_prejudge",
        "judge_id": "judge_terra_full_01",
        "judge_session_id": "session_terra_full_run_01",
        "model_family": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "prompt_hash": "sha256:" + "d" * 64,
        "reviewed_bundle_sha256": bundle_hash,
        "stage_assignment_sha256": assignment["assignment_sha256"],
        "stage_packet_sha256": "e" * 64,
        "stage_packet_manifest_sha256": "f" * 64,
        "expected_count": len(assignment["public_case_ids"]),
        "completed_count": len(verdicts),
        "missing_count": len(missing),
        "missing_case_ids": missing,
        "merged_verdict_output_sha256": judge_verdicts_sha256(verdicts),
        "output_manifest_sha256": None,
        "hard_deterministic_errors_are_binding": True,
    }
    _write_json(resume_path, resume)
    return verdict_path, resume_path, bundle_path, mapping_path, bundle_manifest_path, plan_path


def _packages(root: Path) -> list[tuple[str, Path, Path, Path, Path, Path, Path]]:
    return [
        (comparison, *_build_package(root, comparison))
        for comparison in ("fine_tune_vs_base", "fine_tune_vs_rule_baseline")
    ]


def _cli_args(output_dir: Path, packages: list[tuple[str, Path, Path, Path, Path, Path, Path]]) -> list[str]:
    args = [
        sys.executable,
        str(SCRIPT),
        "--prompt",
        str(PROJECT_ROOT / "contracts" / "sol_followup_judge_prompt_v1.md"),
    ]
    for comparison, verdicts, resume, bundle, mapping, manifest, plan in packages:
        args.extend(
            [
                "--package",
                comparison,
                str(verdicts),
                str(resume),
                str(bundle),
                str(mapping),
                str(manifest),
                str(plan),
            ]
        )
    return [*args, "--output-dir", str(output_dir)]


def test_sol_followup_selector_is_deterministic_hash_bound_and_leakage_safe(tmp_path: Path) -> None:
    from scriber_polishing.sol_followup import (
        SolFollowupPackagePaths,
        audit_sol_followup_package,
        build_sol_followup_artifacts,
        select_sol_followup_cases,
    )

    packages = _packages(tmp_path)
    audits = [
        audit_sol_followup_package(
            SolFollowupPackagePaths(
                comparison_kind=comparison,
                verdicts=verdicts,
                resume_manifest=resume,
                bundle=bundle,
                private_mapping=mapping,
                bundle_manifest=manifest,
                materialization_plan=plan,
            )
        )
        for comparison, verdicts, resume, bundle, mapping, manifest, plan in packages
    ]
    first = select_sol_followup_cases(audits)
    second = select_sol_followup_cases(audits)
    assert first == second
    assert all(selection.selection_manifest["selection_seed"] == 270036 for selection in first)
    assert all(selection.selection_manifest["selection_namespace"] == "sol_followup_v1" for selection in first)
    assert all(
        selection.selection_manifest["sol_release"]["stage_assignment_sha256"]
        == judge_stage_assignment_sha256(
            "sol_release",
            audit.bundle_sha256,
            selection.public_case_ids,
        )
        for audit, selection in zip(audits, first, strict=True)
    )

    artifacts = build_sol_followup_artifacts(
        audits,
        prompt_hash="sha256:" + "1" * 64,
    )
    emitted_metadata = [
        *(selection.selection_manifest for selection in artifacts.selections),
        artifacts.diagnostic_report,
        artifacts.artifact_manifest,
    ]
    rendered = "\n".join(_canonical(value) for value in emitted_metadata)
    assert "SENTINEL_SOURCE" not in rendered
    assert "SENTINEL_REASON" not in rendered
    assert "candidate_a" not in rendered
    assert "candidate_b" not in rendered
    assert "source_text" not in rendered
    assert "target_sst" not in rendered
    assert "brief_reason" not in rendered


def test_sol_followup_cli_materializes_two_blind_packets_and_report(tmp_path: Path) -> None:
    packages = _packages(tmp_path)
    output_dir = tmp_path / "sol_followup_v1"
    result = subprocess.run(_cli_args(output_dir, packages), cwd=PROJECT_ROOT, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "sol_followup=diagnostic_only packages=2" in result.stdout
    assert not list(output_dir.rglob("*.verdict*.json*"))
    report = json.loads((output_dir / "sol-followup-diagnostic-report.json").read_text(encoding="utf-8"))
    assert report["diagnostic_only"] is True
    assert report["release_aggregation"]["full_campaign_aggregation_performed"] is False
    assert report["training_policy"]["new_training_examples_created"] == 0
    for comparison in ("fine_tune_vs_base", "fine_tune_vs_rule_baseline"):
        packet = output_dir / comparison / "sol-release.packet.jsonl"
        packet_manifest = output_dir / comparison / "sol-release.packet.manifest.json"
        selection = output_dir / "private" / comparison / "sol-followup.selection.manifest.json"
        assert packet.exists()
        assert packet_manifest.exists()
        assert selection.exists()
        assert "SENTINEL_SOURCE" not in selection.read_text(encoding="utf-8")
        assert "SENTINEL_REASON" not in selection.read_text(encoding="utf-8")


@pytest.mark.parametrize("tamper", ["resume_hash", "incomplete_pair", "wrong_assignment"])
def test_sol_followup_cli_fails_closed_without_output_on_tampered_sources(tmp_path: Path, tamper: str) -> None:
    packages = _packages(tmp_path)
    comparison, verdicts, resume, bundle, mapping, manifest, plan = packages[0]
    if tamper == "resume_hash":
        payload = json.loads(resume.read_text(encoding="utf-8"))
        payload["merged_verdict_output_sha256"] = "0" * 64
        _write_json(resume, payload)
    elif tamper == "incomplete_pair":
        rows = [json.loads(line) for line in verdicts.read_text(encoding="utf-8").splitlines()]
        _write_jsonl(verdicts, rows[:-1])
    else:
        payload = json.loads(resume.read_text(encoding="utf-8"))
        payload["stage_assignment_sha256"] = "0" * 64
        _write_json(resume, payload)

    output_dir = tmp_path / f"blocked-{tamper}"
    result = subprocess.run(_cli_args(output_dir, packages), cwd=PROJECT_ROOT, text=True, capture_output=True)
    assert result.returncode == 2
    assert not output_dir.exists()


def test_sol_followup_writer_is_atomic_when_commit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scriber_polishing.sol_followup as sol_followup
    from scriber_polishing.sol_followup import (
        SolFollowupPackagePaths,
        audit_sol_followup_package,
        build_sol_followup_artifacts,
        write_sol_followup_artifacts,
    )

    packages = _packages(tmp_path)
    audits = [
        audit_sol_followup_package(
            SolFollowupPackagePaths(comparison, verdicts, resume, bundle, mapping, manifest, plan)
        )
        for comparison, verdicts, resume, bundle, mapping, manifest, plan in packages
    ]
    artifacts = build_sol_followup_artifacts(audits, prompt_hash="sha256:" + "1" * 64)
    output_dir = tmp_path / "atomic-output"

    def _boom(source: Path, target: Path) -> None:
        raise OSError("simulated commit failure")

    monkeypatch.setattr(sol_followup.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated commit failure"):
        write_sol_followup_artifacts(output_dir, artifacts)
    assert not output_dir.exists()
    assert not list(tmp_path.glob(".atomic-output.*"))

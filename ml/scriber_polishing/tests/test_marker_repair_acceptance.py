from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.evaluation_io import join_evaluation_cases
from scriber_polishing.marker_repair_acceptance import (
    compare_marker_repair_acceptance,
    validate_marker_repair_acceptance_report,
)
from scriber_polishing.metrics import evaluate_predictions
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text

_EMPTY_IDS_SHA256 = "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
_ORDER_SHA256 = "50daec69ef0ab52c38393d045551122bf339fae33536cd1ca432edf7e8e4e95f"
_PARENT_TREE_SHA256 = "sha256:f3186be5e03b48dea35063f8ea502b2c4849abc03a9b8c4b41753bdcf7141513"
_POLICY_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_TOKENIZER_SHA256 = "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"


def _write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> str:
    return _write_bytes(path, canonical_json_bytes(value))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    payload = b"".join(canonical_json_bytes(row) for row in rows)
    return _write_bytes(path, payload)


def _reference(index: int) -> dict[str, Any]:
    protected = f"Kennwert-{index:03d}"
    target_sst = f"[DOC]\n[P]{protected} bleibt unverändert.[/P]\n[P]Die Angabe bleibt bestehen.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": f"repair_case_{index:03d}",
        "split": "regression",
        "source_text": (f"{protected} bleibt unverändert. Die Angabe bleibt bestehen."),
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": f"{protected} bleibt unverändert.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [protected],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Angabe bleibt bestehen.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Kennwert", "Bestand"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [protected],
        "slice_tags": ["remediation_parity_dev"],
        "is_identity": False,
    }


def _prediction(reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": reference["case_id"],
        "prediction_sst": reference["target_sst"],
    }


def _evaluation(
    *,
    candidate: str,
    references: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    references_sha256: str,
    predictions_sha256: str,
) -> dict[str, Any]:
    semantic_plans = {reference["case_id"]: reference["semantic_plan"] for reference in references}
    cases = tuple(
        replace(case, semantic_plan=semantic_plans[case.case_id])
        for case in join_evaluation_cases(references, predictions)
    )
    return {
        "schema_version": 1,
        "candidate": candidate,
        "evaluation_config_sha256": "1" * 64,
        "reference_sha256": references_sha256,
        "prediction_sha256": predictions_sha256,
        "exact_case_coverage": True,
        "metrics": asdict(evaluate_predictions(cases)),
        "gate": {
            "schema_version": 1,
            "candidate": candidate,
            "status": "passed",
            "passed": True,
            "hard_gates": {},
            "minimums": {},
            "failed_checks": [],
        },
    }


def _policy_binding(policy_id: str, policy_sha256: str, artifact: str) -> dict[str, Any]:
    return {
        "filename": ("training-policy.json" if policy_id == "gemma_lexical_v1" else "selection-policy.json"),
        "artifact_sha256": artifact,
        "policy_id": policy_id,
        "policy_sha256": policy_sha256,
        "detector": "product_critical_v1",
        "pair_mapping": ("ordered_occurrence_v1" if policy_id == "gemma_lexical_v1" else "unique_exact_value_v1"),
        "tokenizer_identity_sha256": _TOKENIZER_SHA256,
    }


def _preflight(count: int) -> dict[str, Any]:
    return {
        "loaded_record_count": count,
        "accepted_record_count": count,
        "protection_rejected_record_count": 0,
        "sequence_rejected_record_count": 0,
        "protected_example_count": count,
        "marker_count": count,
        "span_count": count,
        "max_sequence_length": 128,
        "sequence_length_limit": 768,
        "accepted_example_ids_sha256": "2" * 64,
        "transformed_pairs_sha256": "3" * 64,
        "sequence_lengths_sha256": "4" * 64,
    }


def _artifact(path: Path) -> dict[str, str]:
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _refresh_cloud_result(result: Path) -> None:
    result_path = result / "cloud-result.json"
    current = json.loads(result_path.read_text(encoding="utf-8"))
    files = {}
    for path in sorted(result.rglob("*")):
        if path.is_file() and path != result_path:
            payload = path.read_bytes()
            files[path.relative_to(result).as_posix()] = {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
    current["files"] = files
    _write_json(result_path, current)


def _training_report(policy_artifact_sha256: str) -> dict[str, Any]:
    policy = {
        "artifact_sha256": policy_artifact_sha256,
        "policy_id": "gemma_lexical_v1",
        "policy_sha256": _POLICY_SHA256,
    }

    def split_evidence(count: int) -> dict[str, Any]:
        return {
            "loaded_record_count": count,
            "accepted_record_count": count,
            "rejected_record_count": 0,
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SHA256,
            "source_target_marker_parity": True,
            "protected_marker_count": count,
            "protected_span_count": count,
            "accepted_example_ids_sha256": "2" * 64,
            "transformed_pairs_sha256": "3" * 64,
        }

    return {
        "schema_version": 2,
        "training_completed": True,
        "max_steps": 146,
        "schedule": {
            "max_steps": 146,
            "update_steps_per_epoch": 146,
            "declared_epochs": 1.0,
            "epoch_boundaries_preserved": True,
            "schedule_source": "epochs",
        },
        "checkpoint_state": {
            "global_step": 146,
            "epoch": 1.0,
            "last_checkpoint": "checkpoint-146",
        },
        "train_examples_loaded": 2335,
        "train_examples_used": 2335,
        "validation_examples_loaded": 431,
        "validation_examples_used": 431,
        "oversize_rejections": {"train": 0, "validation": 0},
        "protection_rejections": {"train": 0, "validation": 0},
        "warm_start_parent_checkpoint": {
            "tree_sha256": _PARENT_TREE_SHA256,
            "relationship": "warm_start_parent_not_resume",
        },
        "resume_from_checkpoint": None,
        "protection_policy": {
            **policy,
            "sha256": f"sha256:{policy_artifact_sha256}",
        },
        "protection_application": {
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SHA256,
            "train": split_evidence(2335),
            "validation": split_evidence(431),
        },
        "remediation_order": {
            "mode": "shuffled",
            "record_count": 2335,
            "epochs": 1,
            "implicit_trainer_shuffling": False,
            "full_epoch_required": True,
            "parent_checkpoint": {
                "tree_sha256": _PARENT_TREE_SHA256,
                "relationship": "warm_start_parent_not_resume",
            },
            "protection_policy": policy,
            "effective_epoch_orders": [
                {
                    "epoch": 0,
                    "record_count": 2335,
                    "effective_example_ids_sha256": _ORDER_SHA256,
                }
            ],
        },
        "final_model": {
            "directory": "final-repair",
            "inventory": {"tree_sha256": "sha256:" + "5" * 64},
            "reload_verification": {
                "status": "passed",
                "protection_policy_sha256": (f"sha256:{policy_artifact_sha256}"),
            },
        },
    }


def _build_fixture(
    tmp_path: Path,
    *,
    critical_regression: bool = False,
) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    result = tmp_path / "cloud-output"
    bundle.mkdir()
    result.mkdir()
    references = [_reference(index) for index in range(200)]
    baseline_predictions = [_prediction(reference) for reference in references]
    candidate_predictions = [_prediction(reference) for reference in references]
    if critical_regression:
        candidate_predictions[0]["prediction_sst"] = str(candidate_predictions[0]["prediction_sst"]).replace(
            "Kennwert-000", "Fremdwert-000"
        )
    training_policy_sha = _write_json(
        bundle / "training-policy.json",
        {
            "schema_version": 1,
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SHA256,
        },
    )
    selection_policy_sha = _write_json(
        bundle / "selection-policy.json",
        {
            "schema_version": 1,
            "policy_id": "gemma_unused_v1",
        },
    )

    references_sha = _write_jsonl(bundle / "parity-dev.references.jsonl", references)
    baseline_predictions_sha = _write_jsonl(
        bundle / "baseline.predictions.jsonl",
        baseline_predictions,
    )
    candidate_predictions_sha = _write_jsonl(
        result / "predictions.jsonl",
        candidate_predictions,
    )
    baseline_manifest = {
        "schema_version": 1,
        "candidate": "shuffled",
        "record_count": 200,
        "reference_sha256": references_sha,
        "prediction_sha256": baseline_predictions_sha,
        "valid_sst_count": 200,
        "restoration_error_count": 0,
    }
    _write_json(bundle / "baseline.predictions.manifest.json", baseline_manifest)
    candidate_manifest = {
        "schema_version": 1,
        "candidate": "lexical_repair",
        "model": "/outputs/pilot-marker-lexical-repair-v1/training/final-repair",
        "revision": None,
        "device": "cuda",
        "quantization": "none",
        "record_count": 200,
        "reference_sha256": references_sha,
        "prediction_sha256": candidate_predictions_sha,
        "valid_sst_count": 200,
        "restoration_error_count": 0,
        "protection_policy": {
            "artifact_sha256": training_policy_sha,
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SHA256,
            "source": "explicit_artifact",
            "path": "/outputs/model/scriber-protection-policy.json",
        },
        "total_generation_seconds": 1.0,
        "total_input_tokens": 200,
        "total_output_tokens": 200,
    }
    _write_json(result / "predictions.manifest.json", candidate_manifest)
    _write_json(
        bundle / "baseline.evaluation.json",
        _evaluation(
            candidate="shuffled",
            references=references,
            predictions=baseline_predictions,
            references_sha256=references_sha,
            predictions_sha256=baseline_predictions_sha,
        ),
    )
    _write_json(
        result / "evaluation.json",
        _evaluation(
            candidate="lexical_repair",
            references=references,
            predictions=candidate_predictions,
            references_sha256=references_sha,
            predictions_sha256=candidate_predictions_sha,
        ),
    )
    _write_json(bundle / "baseline.comparison.json", {"schema_version": 1})
    _write_bytes(
        bundle / "acceptance-policy.yaml",
        (
            b"schema_version: 1\n"
            b"kind: scriber_marker_repair_acceptance_policy\n"
            b"decision: accept_only_if_all_hard_gates_pass\n"
            b"case_scope: validation_only_parity_dev\n"
            b"case_count: 200\n"
            b"hard_gates:\n"
            b"  raw_sst_parse_rate: 1.0\n"
            b"  protected_span_restoration_rate: 1.0\n"
            b"  protected_span_deletions: 0\n"
            b"  protected_span_duplications: 0\n"
            b"  protected_span_malformed: 0\n"
            b"  protected_span_reordered: 0\n"
            b"  new_critical_errors: 0\n"
            b"  quality_metrics_noninferior: true\n"
            b"fallback: reject_repair_keep_frozen_shuffled_control\n"
        ),
    )
    _write_json(bundle / "source-shuffled.training-report.json", {"schema_version": 2})

    source_binding = _artifact(bundle / "source-shuffled.training-report.json")
    artifacts = {
        "parity_references": _artifact(bundle / "parity-dev.references.jsonl"),
        "baseline_predictions": _artifact(bundle / "baseline.predictions.jsonl"),
        "baseline_prediction_manifest": _artifact(bundle / "baseline.predictions.manifest.json"),
        "baseline_evaluation": _artifact(bundle / "baseline.evaluation.json"),
        "baseline_comparison": _artifact(bundle / "baseline.comparison.json"),
        "acceptance_policy": _artifact(bundle / "acceptance-policy.yaml"),
        "training_policy": _artifact(bundle / "training-policy.json"),
        "source_training_report": source_binding,
    }
    manifest = {
        "schema_version": 1,
        "kind": "scriber_marker_repair_bundle",
        "experiment_id": "pilot-marker-lexical-repair-v1",
        "repair_input_sha256": "sha256:" + "6" * 64,
        "source": {
            "experiment_manifest": source_binding,
            "shuffled_plan": source_binding,
            "shuffled_training_config": source_binding,
            "shuffled_run_identity": source_binding,
            "shuffled_training_report": source_binding,
            "prior_run_fingerprint": "sha256:" + "7" * 64,
            "prior_comparison_pair_sha256": "8" * 64,
        },
        "policies": {
            "selection": _policy_binding(
                "gemma_unused_v1",
                "sha256:" + "9" * 64,
                selection_policy_sha,
            ),
            "training": _policy_binding(
                "gemma_lexical_v1",
                _POLICY_SHA256,
                training_policy_sha,
            ),
        },
        "data": {
            "train": {
                "filename": "selected-train.jsonl",
                "sha256": "b" * 64,
                "record_count": 2335,
            },
            "validation": {
                "filename": "safe-validation.jsonl",
                "sha256": "c" * 64,
                "record_count": 431,
            },
            "safety_source": {
                "filename": "safety.jsonl",
                "sha256": "d" * 64,
                "record_count": 7000,
            },
            "parity_references": {
                "filename": "parity-dev.references.jsonl",
                "sha256": references_sha,
                "record_count": 200,
            },
            "parity_selection_manifest": source_binding,
            "selected_train_example_ids_sha256": "e" * 64,
        },
        "parent_checkpoint": {
            "path": "parent-checkpoint",
            "tree_sha256": _PARENT_TREE_SHA256,
            "relationship": "warm_start_parent_not_resume",
        },
        "training_contract": {"effective_batch_size": 16},
        "schedule": {
            "effective_batch_size": 16,
            "epochs": 1,
            "update_steps": 146,
        },
        "order_lock": {
            "algorithm": "frozen_source_effective_order_v1",
            "record_count": 2335,
            "source_declared_order_sha256": "f" * 64,
            "source_effective_order_sha256": _ORDER_SHA256,
            "source_bucket_algorithm": "fixed_window_length_descending_v1",
            "source_bucket_window": 64,
        },
        "preflight": {
            "train": _preflight(2335),
            "validation": _preflight(431),
        },
        "baseline": {
            "prediction_manifest": {
                "path": "baseline.predictions.manifest.json",
                "sha256": artifacts["baseline_prediction_manifest"]["sha256"],
            },
            "predictions": {
                "path": "baseline.predictions.jsonl",
                "sha256": artifacts["baseline_predictions"]["sha256"],
            },
            "evaluation": {
                "path": "baseline.evaluation.json",
                "sha256": artifacts["baseline_evaluation"]["sha256"],
            },
            "comparison": {
                "path": "baseline.comparison.json",
                "sha256": artifacts["baseline_comparison"]["sha256"],
            },
        },
        "holdout_isolation": {
            "opened_splits": ["train", "validation"],
            "test_inputs": False,
            "challenge_inputs": False,
            "ai_gold_inputs": False,
            "decision_data": "validation_only_parity_dev",
        },
        "only_intended_difference": ("protected_marker_representation_and_pair_mapping"),
        "execution_contract": {
            "run_kind": "improvement",
            "optimizer_state": "fresh",
            "scheduler_state": "fresh",
            "parent_as_resume": "forbidden",
            "same_run_resume": "identity_bound_recovery_only",
            "implicit_trainer_shuffling": False,
        },
        "artifacts": artifacts,
    }
    manifest_sha = _write_json(bundle / "repair-manifest.json", manifest)
    _write_bytes(
        result / "repair-manifest.json",
        (bundle / "repair-manifest.json").read_bytes(),
    )
    _write_json(
        result / "training-report.json",
        _training_report(training_policy_sha),
    )
    _write_bytes(
        result / "acceptance-policy.yaml",
        (bundle / "acceptance-policy.yaml").read_bytes(),
    )
    _write_json(result / "preflight.json", {"ready_for_model_load": True})
    _write_bytes(result / "training" / "final-repair" / "model.bin", b"model")
    _write_json(
        result / "cloud-result.json",
        {
            "schema_version": 1,
            "kind": "scriber_hf_marker_repair_result",
            "training_completed": True,
            "evaluation_completed": True,
            "evaluation_gate_exit": 0,
            "final_model_directory": "final-repair",
            "files": {},
        },
    )
    _refresh_cloud_result(result)
    assert hashlib.sha256((result / "repair-manifest.json").read_bytes()).hexdigest() == manifest_sha
    return bundle, result


def test_marker_repair_acceptance_adopts_only_the_complete_bound_repair(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)

    report = compare_marker_repair_acceptance(
        bundle_dir=bundle,
        cloud_output_dir=result,
    )

    assert report["decision"] == "ACCEPT_REPAIR"
    assert report["accepted"] is True
    assert report["execution"]["actual_completed_steps"] == 146
    assert report["execution"]["effective_order_sha256"] == _ORDER_SHA256
    assert report["parity"]["candidate_prediction_count"] == 200
    assert report["safety"]["restoration_error_count"] == 0
    assert report["safety"]["valid_sst_count"] == 200
    assert report["safety"]["new_critical_errors"]["actual"] == 0
    assert validate_marker_repair_acceptance_report(report) == report


def test_marker_repair_acceptance_rejects_and_binds_one_new_critical_error(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(
        tmp_path,
        critical_regression=True,
    )

    report = compare_marker_repair_acceptance(
        bundle_dir=bundle,
        cloud_output_dir=result,
    )

    assert report["decision"] == "REJECT_REPAIR_KEEP_FROZEN_SHUFFLED"
    assert report["accepted"] is False
    critical = report["safety"]["new_critical_errors"]
    assert critical["actual"] > 0
    check = next(value for value in critical["checks"].values() if value["new"] > 0)
    assert check["new_case_ids"] == ["repair_case_000"]

    forged = json.loads(json.dumps(report))
    forged_check = next(
        value for value in forged["safety"]["new_critical_errors"]["checks"].values() if value["new"] > 0
    )
    forged_check["new_case_ids_sha256"] = "0" * 64
    with pytest.raises(
        ValueError,
        match="critical-error case binding is inconsistent",
    ):
        validate_marker_repair_acceptance_report(forged)


def test_marker_repair_acceptance_report_cannot_forge_noninferiority(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)
    report = compare_marker_repair_acceptance(
        bundle_dir=bundle,
        cloud_output_dir=result,
    )
    forged = json.loads(json.dumps(report))
    punctuation = forged["safety"]["quality_metrics_noninferior"]["checks"]["punctuation_f1"]
    punctuation["candidate"] = 0.0
    punctuation["passed"] = True

    with pytest.raises(
        ValueError,
        match="quality noninferiority check is inconsistent",
    ):
        validate_marker_repair_acceptance_report(forged)


def test_marker_repair_acceptance_rejects_impossible_prediction_counts(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)
    manifest_path = result / "predictions.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["restoration_error_count"] = 1
    manifest["valid_sst_count"] = 200
    _write_json(manifest_path, manifest)
    _refresh_cloud_result(result)

    with pytest.raises(
        ValueError,
        match="prediction counts cannot both describe the 200 cases",
    ):
        compare_marker_repair_acceptance(
            bundle_dir=bundle,
            cloud_output_dir=result,
        )


def test_marker_repair_acceptance_cli_writes_only_an_accepted_report(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)
    output = tmp_path / "acceptance.json"
    project_root = Path(__file__).parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "compare_marker_repair_acceptance.py"),
            "--bundle-dir",
            str(bundle),
            "--cloud-output-dir",
            str(result),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "decision=ACCEPT_REPAIR" in completed.stdout
    report = json.loads(output.read_text(encoding="utf-8"))
    assert validate_marker_repair_acceptance_report(report)["accepted"] is True


def test_marker_repair_acceptance_requires_one_lexical_marker_per_occurrence(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)
    report_path = result / "training-report.json"
    training_report = json.loads(report_path.read_text(encoding="utf-8"))
    training_report["protection_application"]["train"]["protected_marker_count"] = 2334
    _write_json(report_path, training_report)
    _refresh_cloud_result(result)

    with pytest.raises(
        ValueError,
        match="protection application train is not lossless",
    ):
        compare_marker_repair_acceptance(
            bundle_dir=bundle,
            cloud_output_dir=result,
        )


def test_marker_repair_acceptance_report_cannot_forge_restoration_gate(
    tmp_path: Path,
) -> None:
    bundle, result = _build_fixture(tmp_path)
    report = compare_marker_repair_acceptance(
        bundle_dir=bundle,
        cloud_output_dir=result,
    )
    forged = json.loads(json.dumps(report))
    forged["safety"]["restoration_error_count"] = 1

    with pytest.raises(
        ValueError,
        match="derived hard-gate checks are inconsistent",
    ):
        validate_marker_repair_acceptance_report(forged)

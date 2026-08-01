from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_hf_training_continuation import _training_report as _full_training_report

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation import (
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
    FinalEvaluationError,
    _packet_inventory,
    bind_final_continuation_evidence,
    bind_final_continuation_model,
    bind_final_evaluation_prediction_manifest,
    bind_final_evaluation_retry_evidence,
    build_final_evaluation_budget,
    build_hf_final_evaluation_launch_contract,
    build_hf_final_evaluation_packet,
    claim_final_evaluation_launch,
    final_evaluation_runner_payload,
    finalize_hf_final_evaluation,
    list_final_evaluation_suites,
    plan_final_evaluation_prediction_lane,
    record_final_evaluation_exit,
    recover_final_evaluation_exit,
    release_final_evaluation_writer,
    validate_final_evaluation_references,
    verify_final_evaluation_output_provenance,
    verify_hf_final_evaluation_packet,
)
from scriber_polishing.hf_training_continuation import (
    build_hf_cost_audit,
    reserve_hf_budget,
)
from scriber_polishing.hf_training_continuation_finalization import (
    ATTEMPT9_FINAL_MODEL_URI,
    ATTEMPT9_JOB_ID,
    ATTEMPT9_SOURCE_URI,
    FINALIZATION_REMOTE_OUTPUT_PATH,
    attempt9_finalization_source_contract,
    build_finalization_result,
    normalize_attempt9_training_report,
)
from scriber_polishing.hf_training_continuation_recovery import (
    ATTEMPT8_JOB_ID,
)

_FAILED_EVALUATIONS = [
    {
        "job_id": "6a6ba10023ed89c748ec83f1",
        "flavor": "l4x1",
        "terminal_status": "ERROR",
        "running_seconds": 63,
        "packet_tree_sha256": ("sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93"),
    },
    {
        "job_id": "6a6ba85b23ed89c748ec8491",
        "flavor": "l4x1",
        "terminal_status": "ERROR",
        "running_seconds": 45,
        "packet_tree_sha256": ("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
    },
]
_AUXILIARY_DIAGNOSTICS = [
    {
        "job_id": "6a6ba991b36a6516e96a31bc",
        "flavor": "cpu-basic",
        "terminal_status": "COMPLETED",
        "running_seconds": 23,
        "packet_tree_sha256": ("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
    },
    {
        "job_id": "6a6ba9d8b36a6516e96a31be",
        "flavor": "cpu-basic",
        "terminal_status": "COMPLETED",
        "running_seconds": 24,
        "packet_tree_sha256": ("sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749"),
    },
]


def test_final_evaluation_budget_uses_actual_continuation_cost_and_reserves_120_minutes() -> None:
    budget = build_final_evaluation_budget(
        continuation_evidence={
            "historical_cost_usd": "0.593",
            "actual_job_cost_usd": "0.250000",
        },
        total_budget_usd="5.000",
    )

    assert budget == {
        "total_budget_usd": "5.000",
        "historical_cost_usd": "0.593",
        "continuation_actual_cost_usd": "0.250000",
        "actual_before_evaluation_usd": "0.843000",
        "evaluation_timeout_minutes": 120,
        "evaluation_maximum_cost_usd": "1.600",
        "maximum_total_cost_usd": "2.443000",
        "remaining_after_reservation_usd": "2.557000",
        "strictly_below_total_budget": True,
    }


def test_final_evaluation_budget_prices_an_explicit_a10g_fallback() -> None:
    budget = build_final_evaluation_budget(
        continuation_evidence={
            "historical_cost_usd": "0.593",
            "actual_job_cost_usd": "0.250000",
        },
        total_budget_usd="5.000",
        evaluation_flavor="a10g-small",
    )

    assert budget["evaluation_maximum_cost_usd"] == "2.000"
    assert budget["maximum_total_cost_usd"] == "2.843000"
    assert budget["remaining_after_reservation_usd"] == "2.157000"


def test_final_evaluation_budget_rejects_an_unapproved_gpu_flavor() -> None:
    with pytest.raises(FinalEvaluationError, match="l4x1 or a10g-small"):
        build_final_evaluation_budget(
            continuation_evidence={
                "historical_cost_usd": "0.593",
                "actual_job_cost_usd": "0.250000",
            },
            total_budget_usd="5.000",
            evaluation_flavor="a100-large",
        )


def test_final_evaluation_budget_uses_the_complete_no_retrain_recovery_chain() -> None:
    budget = build_final_evaluation_budget(
        continuation_evidence={
            "historical_cost_usd": "0.593",
            "actual_job_cost_usd": "0.266667",
            "actual_campaign_cost_usd": "0.893167",
            "recovery_chain": {
                "recovery_job_cost_usd": "0.033333",
                "finalization_job_cost_usd": "0.000167",
            },
        },
        total_budget_usd="5.000",
    )

    assert budget["continuation_actual_cost_usd"] == "0.300167"
    assert budget["actual_before_evaluation_usd"] == "0.893167"
    assert budget["maximum_total_cost_usd"] == "2.493167"


def test_final_evaluation_retry_evidence_is_exact_and_budgeted_before_retry() -> None:
    retry = bind_final_evaluation_retry_evidence(
        failed_evaluations=_FAILED_EVALUATIONS,
        auxiliary_diagnostic_jobs=_AUXILIARY_DIAGNOSTICS,
    )
    budget = build_final_evaluation_budget(
        continuation_evidence={
            "historical_cost_usd": "0.593",
            "actual_job_cost_usd": "0.266667",
            "actual_campaign_cost_usd": "0.893167",
            "recovery_chain": {
                "recovery_job_cost_usd": "0.033333",
                "finalization_job_cost_usd": "0.000167",
            },
        },
        retry_evidence=retry,
        total_budget_usd="5.000",
    )

    assert retry["schema_version"] == 2
    assert [job["job_id"] for job in retry["failed_evaluations"]] == [
        "6a6ba10023ed89c748ec83f1",
        "6a6ba85b23ed89c748ec8491",
    ]
    assert [job["job_id"] for job in retry["auxiliary_diagnostic_jobs"]] == [
        "6a6ba991b36a6516e96a31bc",
        "6a6ba9d8b36a6516e96a31be",
    ]
    assert retry["failed_evaluation_total_cost_usd"] == "0.040000"
    assert retry["auxiliary_diagnostic_total_cost_usd"] == "0.000334"
    assert retry["campaign_cost_before_next_job_usd"] == "0.933501"
    assert budget["failed_evaluation_actual_cost_usd"] == "0.040000"
    assert budget["auxiliary_diagnostic_actual_cost_usd"] == "0.000334"
    assert budget["actual_before_evaluation_usd"] == "0.933501"
    assert budget["maximum_total_cost_usd"] == "2.533501"

    third_diagnostic = [
        *_AUXILIARY_DIAGNOSTICS,
        {
            "job_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
            "flavor": "cpu-basic",
            "terminal_status": "COMPLETED",
            "running_seconds": 25,
            "packet_tree_sha256": "sha256:" + "b" * 64,
        },
    ]
    after_preflight = bind_final_evaluation_retry_evidence(
        failed_evaluations=_FAILED_EVALUATIONS,
        auxiliary_diagnostic_jobs=third_diagnostic,
    )
    assert after_preflight["campaign_cost_before_next_job_usd"] == "0.933668"
    final_budget = build_final_evaluation_budget(
        continuation_evidence={
            "historical_cost_usd": "0.593",
            "actual_job_cost_usd": "0.266667",
            "actual_campaign_cost_usd": "0.893167",
            "recovery_chain": {
                "recovery_job_cost_usd": "0.033333",
                "finalization_job_cost_usd": "0.000167",
            },
        },
        retry_evidence=after_preflight,
        total_budget_usd="5.000",
    )
    assert final_budget["actual_before_evaluation_usd"] == "0.933668"
    assert final_budget["maximum_total_cost_usd"] == "2.533668"

    changed_attempt11 = [dict(job) for job in _FAILED_EVALUATIONS]
    changed_attempt11[0]["terminal_status"] = "COMPLETED"
    with pytest.raises(FinalEvaluationError, match="prior HF job identity"):
        bind_final_evaluation_retry_evidence(
            failed_evaluations=changed_attempt11,
            auxiliary_diagnostic_jobs=_AUXILIARY_DIAGNOSTICS,
        )


def test_final_evaluation_budget_fails_closed_at_the_five_dollar_boundary() -> None:
    try:
        build_final_evaluation_budget(
            continuation_evidence={
                "historical_cost_usd": "3.000",
                "actual_job_cost_usd": "0.400000",
            },
            total_budget_usd="5.000",
        )
    except FinalEvaluationError as error:
        assert "must remain strictly below the $5.000 budget" in str(error)
    else:
        raise AssertionError("the exact $5.000 boundary must be rejected")


def test_final_evaluation_budget_rejects_noncanonical_cost_precision() -> None:
    with pytest.raises(FinalEvaluationError, match="6 decimal places"):
        build_final_evaluation_budget(
            continuation_evidence={
                "historical_cost_usd": "0.593",
                "actual_job_cost_usd": "0.25",
            },
            total_budget_usd="5.000",
        )


def test_final_evaluation_binds_the_three_frozen_holdout_suites() -> None:
    project = Path(__file__).parents[1]

    suites = validate_final_evaluation_references(project / "artifacts/evaluation/final_1100_v1/references")

    assert suites == {
        "ai_gold": {
            "records_filename": "ai_gold_automatic_test_challenge.references.jsonl",
            "manifest_filename": "ai_gold_automatic_test_challenge.references.manifest.json",
            "record_count": 1750,
            "split_counts": {"challenge": 750, "test": 1000},
            "records_sha256": "e12c672e45fccf8beccc1fc06c36d8a45540c7ee7c0149bbce773a1136c6c664",
            "manifest_sha256": "20b375a4584ff91468f6482f074004f72cecd3c2b70c7dc140f3dacd25f9248a",
            "usage": "inference_and_evaluation_only",
        },
        "critical_regression": {
            "records_filename": "critical_regression_as_challenge.references.jsonl",
            "manifest_filename": "critical_regression_as_challenge.references.manifest.json",
            "record_count": 500,
            "split_counts": {"challenge": 500},
            "records_sha256": "5b5bffe762e718b0f8d2dd0905a732d71f5ff21256c9c8d6cc06bb396ff2fd14",
            "manifest_sha256": "a980d4d84510c5de3d3cb647322f0af4d36e23b333b33940c7d41c7b399fd52f",
            "usage": "inference_and_evaluation_only",
        },
        "regular_test": {
            "records_filename": "regular_test_1000.references.jsonl",
            "manifest_filename": "regular_test_1000.references.manifest.json",
            "record_count": 1000,
            "split_counts": {"test": 1000},
            "records_sha256": "01e92d42516a5356f1b3607e15fc7faee2e47ddf717d0781aca60c2933a276fb",
            "manifest_sha256": "ee6ee04a72234adfb336d7907a1e5e4143c58ddee3b78dc79a03d2b401af3ca2",
            "usage": "inference_and_evaluation_only",
        },
    }


def test_final_evaluation_rejects_a_holdout_relabelled_as_train(
    tmp_path: Path,
) -> None:
    records = tmp_path / "ai_gold_automatic_test_challenge.references.jsonl"
    records.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": "forbidden_train_case",
                "split": "train",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_reference_bundle",
        "output_filename": records.name,
        "record_count": 1,
        "records_sha256": hashlib.sha256(records.read_bytes()).hexdigest(),
        "case_ids_sha256": "0" * 64,
        "split_counts": {"train": 1},
        "inputs": [],
    }
    (tmp_path / "ai_gold_automatic_test_challenge.references.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(
        FinalEvaluationError,
        match="frozen AI-Gold references differ",
    ):
        validate_final_evaluation_references(tmp_path)


def _continuation_training_report() -> dict[str, object]:
    report, _ = _full_training_report()
    return report


_TEST_FINAL_DIRECTORY = str(_continuation_training_report()["final_model"]["directory"])


def _write_continuation_evidence(
    root: Path,
) -> tuple[Path, Path]:
    report_path = root / "training-report.json"
    report_path.write_text(
        json.dumps(_continuation_training_report()),
        encoding="utf-8",
    )
    result_path = root / "continuation-result.json"
    training_report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "scriber_training_continuation_result",
                "status": "complete",
                "flavor": "a10g-small",
                "start_updates": 846,
                "additional_updates": 254,
                "target_updates": 1100,
                "cumulative_updates": 1100,
                "replayed_updates": 0,
                "parent_initialization": "warm_start_weights_only",
                "same_run_resume": "identity_bound_recovery_only",
                "final_model_directory": _TEST_FINAL_DIRECTORY,
                "final_model_tree_sha256": "sha256:" + "5" * 64,
                "final_model_file_count": 12,
                "final_model_total_bytes": 575466744,
                "training_report_sha256": ("sha256:" + training_report_sha256),
                "quantization_performed": False,
                "publication_performed": False,
            }
        ),
        encoding="utf-8",
    )
    return result_path, report_path


def _write_continuation_budget_evidence(
    root: Path,
) -> tuple[Path, Path]:
    audit_path = root / "hf-cost-audit.json"
    audit_payload = canonical_json_bytes(build_hf_cost_audit())
    audit_path.write_bytes(audit_payload)
    budget = reserve_hf_budget(
        prior_cost_usd=str(build_hf_cost_audit()["conservative_prior_cost_usd"]),
        flavor="a10g-small",
        timeout_minutes=30,
    )
    manifest_path = root / "continuation-manifest.json"
    manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "scriber_hf_training_continuation",
                "status": "ready",
                "arithmetic": {
                    "start_updates": 846,
                    "additional_updates": 254,
                    "target_updates": 1100,
                    "replayed_updates": 0,
                    "parent_initialization": "warm_start_weights_only",
                    "same_run_resume": "identity_bound_recovery_only",
                },
                "data": {
                    "test_inputs": False,
                    "challenge_inputs": False,
                },
                "training": {
                    "quantization": None,
                    "publication": False,
                },
                "budget": {
                    **budget,
                    "flavor": "a10g-small",
                    "cost_audit_sha256": ("sha256:" + hashlib.sha256(audit_payload).hexdigest()),
                },
                "storage": {
                    "remote_result_uri": (
                        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
                    )
                },
            }
        )
    )
    return manifest_path, audit_path


def _write_continuation_packet_verification(
    root: Path,
    *,
    continuation_manifest_path: Path,
    packet_tree_sha256: str = "sha256:" + "6" * 64,
) -> Path:
    path = root / "scriber-1100-packet-verification.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "scriber_hf_training_continuation_packet_verification",
                "packet_tree_sha256": packet_tree_sha256,
                "continuation_manifest_sha256": (
                    "sha256:" + hashlib.sha256(continuation_manifest_path.read_bytes()).hexdigest()
                ),
                "file_count": 174,
                "byte_count": 12635547,
                "flavor": "a10g-small",
                "parent_model_embedded": False,
                "credential_material_present": False,
            }
        )
    )
    return path


def _write_no_retrain_finalization_evidence(
    root: Path,
    *,
    finalization_job_id: str = "6a6b99a8b36a6516e96a30aa",
    finalization_packet_tree_sha256: str = "sha256:" + "7" * 64,
) -> Path:
    project = Path(__file__).parents[1]
    source_contract = attempt9_finalization_source_contract()
    source_contract_payload = canonical_json_bytes(source_contract)
    (root / "attempt9-finalization-source-contract.json").write_bytes(source_contract_payload)
    source_report = json.loads(
        (project / "artifacts/cloud/hf-training-continuation-recovery-attempt9-partial/training-report.json").read_text(
            encoding="utf-8"
        )
    )
    run_identity = json.loads(
        (project / "artifacts/cloud/hf-training-continuation-recovery-attempt9-partial/run-identity.json").read_text(
            encoding="utf-8"
        )
    )
    corrected_report = normalize_attempt9_training_report(
        source_report,
        run_identity=run_identity,
        source_training_report_sha256=source_contract["files"]["training-report.json"]["sha256"],
        source_job_id=ATTEMPT9_JOB_ID,
    )
    corrected_report_payload = canonical_json_bytes(corrected_report)
    (root / "training-report.json").write_bytes(corrected_report_payload)
    verification = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_finalization_source_verification",
        "status": "complete",
        "source_job_id": ATTEMPT9_JOB_ID,
        "run_fingerprint": source_contract["run_fingerprint"],
        "completed_training_steps": 254,
        "cumulative_training_steps": 1100,
        "training_steps_executed": 0,
        "validation_steps_verified": 216,
        "validation_reexecuted": False,
        "validation_reused_from_job_id": ATTEMPT9_JOB_ID,
        "final_model": {
            "tree_sha256": source_contract["final_model"]["tree_sha256"],
            "file_count": source_contract["final_model"]["file_count"],
            "total_bytes": source_contract["final_model"]["total_bytes"],
            "model_copied": False,
        },
    }
    verification_payload = canonical_json_bytes(verification)
    (root / "attempt9-finalization-verification.json").write_bytes(verification_payload)
    (root / "finalization-launch-guard.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "scriber_hf_training_continuation_finalization_launch_guard",
                "status": "active",
                "owner_id": "11111111-1111-4111-8111-111111111111",
                "output_root": "/outputs/" + FINALIZATION_REMOTE_OUTPUT_PATH,
                "source_remote_uri": ATTEMPT9_SOURCE_URI,
                "source_job_id": ATTEMPT9_JOB_ID,
                "packet_tree_sha256": finalization_packet_tree_sha256,
                "source_contract_sha256": ("sha256:" + hashlib.sha256(source_contract_payload).hexdigest()),
                "training_steps_executed": 0,
                "validation_reexecuted": False,
                "model_copied": False,
                "overwrite_policy": "forbidden_new_prefix_required",
            }
        )
    )
    result = build_finalization_result(
        corrected_training_report_sha256=("sha256:" + hashlib.sha256(corrected_report_payload).hexdigest()),
        source_verification_sha256=("sha256:" + hashlib.sha256(verification_payload).hexdigest()),
    )
    result_path = root / "continuation-result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    return result_path


def _frozen_reference_bindings_for_manifest() -> dict[str, object]:
    return {
        "ai_gold": {
            "records_filename": "ai_gold_automatic_test_challenge.references.jsonl",
            "manifest_filename": "ai_gold_automatic_test_challenge.references.manifest.json",
            "record_count": 1750,
            "split_counts": {"challenge": 750, "test": 1000},
            "records_sha256": "e12c672e45fccf8beccc1fc06c36d8a45540c7ee7c0149bbce773a1136c6c664",
            "manifest_sha256": "20b375a4584ff91468f6482f074004f72cecd3c2b70c7dc140f3dacd25f9248a",
            "usage": "inference_and_evaluation_only",
        },
        "critical_regression": {
            "records_filename": "critical_regression_as_challenge.references.jsonl",
            "manifest_filename": "critical_regression_as_challenge.references.manifest.json",
            "record_count": 500,
            "split_counts": {"challenge": 500},
            "records_sha256": "5b5bffe762e718b0f8d2dd0905a732d71f5ff21256c9c8d6cc06bb396ff2fd14",
            "manifest_sha256": "a980d4d84510c5de3d3cb647322f0af4d36e23b333b33940c7d41c7b399fd52f",
            "usage": "inference_and_evaluation_only",
        },
        "regular_test": {
            "records_filename": "regular_test_1000.references.jsonl",
            "manifest_filename": "regular_test_1000.references.manifest.json",
            "record_count": 1000,
            "split_counts": {"test": 1000},
            "records_sha256": "01e92d42516a5356f1b3607e15fc7faee2e47ddf717d0781aca60c2933a276fb",
            "manifest_sha256": "ee6ee04a72234adfb336d7907a1e5e4143c58ddee3b78dc79a03d2b401af3ca2",
            "usage": "inference_and_evaluation_only",
        },
    }


def _write_final_evaluation_core_manifest(
    root: Path,
    *,
    total_budget_usd: str = "5.000",
    evaluation_flavor: str = "l4x1",
) -> tuple[Path, dict[str, object]]:
    result_path, _ = _write_continuation_evidence(root)
    continuation_manifest_path, _ = _write_continuation_budget_evidence(root)
    packet_tree = "sha256:" + "6" * 64
    _write_continuation_packet_verification(
        root,
        continuation_manifest_path=continuation_manifest_path,
        packet_tree_sha256=packet_tree,
    )
    remote_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "attempt8/scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
    )
    bound = bind_final_continuation_evidence(
        continuation_result_path=result_path,
        continuation_job_id="6a6b8a78b36a6516e96a2fb1",
        continuation_running_seconds=863,
        expected_packet_tree_sha256=packet_tree,
        remote_model_uri=remote_uri,
        total_budget_usd=total_budget_usd,
    )
    bound["budget"] = build_final_evaluation_budget(
        continuation_evidence=bound["continuation_evidence"],
        total_budget_usd=total_budget_usd,
        evaluation_flavor=evaluation_flavor,
    )
    path = root / "final-evaluation-manifest.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "kind": "scriber_hf_final_evaluation",
                "source_git_head": "7ecd7e1ed349d1bcadbbb8151b584b766e9746d5",
                "image": "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime",
                "flavor": evaluation_flavor,
                "batch_size": 8,
                "max_new_tokens": 512,
                "base_model": {
                    "model_id": BASE_MODEL_ID,
                    "revision": BASE_MODEL_REVISION,
                    "remote_model_uri": ("hf://models/google/gemma-3-270m-it@ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"),
                    "mount_mode": "read_only",
                    "tree_sha256": ("sha256:0d610d30384d641366232e4bd8ad556c6a28470781e68e93757fe9e4ae1a0f57"),
                    "config_sha256": ("sha256:8983343e5bfbf8e336d222a522a1d781443491e9de2d081b8856f8fd26ab6046"),
                    "file_count": 11,
                    "total_bytes": 575485707,
                    "preprocessing_policy": "implicit_legacy_default",
                },
                "final_model": {
                    **bound["final_model"],
                    "preprocessing_policy": {
                        "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
                        "policy_id": "gemma_lexical_v1",
                        "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
                    },
                },
                "continuation_evidence": bound["continuation_evidence"],
                "references": _frozen_reference_bindings_for_manifest(),
                "evaluation": {
                    "config_filename": "evaluation.yaml",
                    "config_sha256": ("656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998"),
                    "candidates": ["final", "base_model"],
                    "reports_per_candidate": 3,
                },
                "budget": bound["budget"],
                "training": {
                    "training_steps_executed": 0,
                    "reference_usage": "inference_and_evaluation_only",
                    "train_split_allowed": False,
                },
                "output": {
                    "bucket_root": ("hf://buckets/Buttermilk03/scriber-polishing-private-runs"),
                    "relative_path": "attempt8/scriber-1100-final-evaluation-v1",
                    "container_path": ("/outputs/attempt8/scriber-1100-final-evaluation-v1"),
                    "unique": True,
                },
                "publication": {
                    "allowed": False,
                    "destination_kind": "private_hf_bucket",
                },
            }
        )
    )
    return path, bound


def _build_test_final_evaluation_packet(
    root: Path,
    *,
    output_dir: Path,
    total_budget_usd: str = "5.000",
    evaluation_flavor: str = "l4x1",
):
    project = Path(__file__).parents[1]
    result_path, _ = _write_continuation_evidence(root)
    continuation_manifest_path, _ = _write_continuation_budget_evidence(root)
    packet_tree = "sha256:" + "6" * 64
    _write_continuation_packet_verification(
        root,
        continuation_manifest_path=continuation_manifest_path,
        packet_tree_sha256=packet_tree,
    )
    return build_hf_final_evaluation_packet(
        continuation_result_path=result_path,
        continuation_job_id="6a6b8a78b36a6516e96a2fb1",
        continuation_running_seconds=863,
        expected_continuation_packet_tree_sha256=packet_tree,
        remote_model_uri=(
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
            "attempt8/scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
        ),
        total_budget_usd=total_budget_usd,
        reference_root=(project / "artifacts/evaluation/final_1100_v1/references"),
        output_dir=output_dir,
        evaluation_flavor=evaluation_flavor,
    )


def _build_actual_retry_packet(output_dir: Path):
    project = Path(__file__).parents[1]
    return build_hf_final_evaluation_packet(
        continuation_result_path=(
            project
            / "artifacts/cloud/hf-training-continuation-finalization-attempt10-complete/continuation-result.json"
        ),
        continuation_job_id=ATTEMPT8_JOB_ID,
        continuation_running_seconds=912,
        expected_continuation_packet_tree_sha256=(
            "sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"
        ),
        remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
        total_budget_usd="5.000",
        reference_root=(project / "artifacts/evaluation/final_1100_v1/references"),
        output_dir=output_dir,
        finalization_job_id="6a6b9ee723ed89c748ec83a1",
        finalization_running_seconds=20,
        expected_finalization_packet_tree_sha256=(
            "sha256:2db2a308dcfe5807209100fd823453619428435dbeff5b4679d5dc25dabd80a5"
        ),
        failed_evaluations=_FAILED_EVALUATIONS,
        auxiliary_diagnostic_jobs=_AUXILIARY_DIAGNOSTICS,
    )


def test_final_evaluation_binds_actual_a10g_continuation_job_and_attempt8_model(
    tmp_path: Path,
) -> None:
    result_path, _ = _write_continuation_evidence(tmp_path)
    manifest_path, _ = _write_continuation_budget_evidence(tmp_path)
    packet_tree = "sha256:" + "6" * 64
    _write_continuation_packet_verification(
        tmp_path,
        continuation_manifest_path=manifest_path,
        packet_tree_sha256=packet_tree,
    )
    remote_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "attempt8/scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
    )

    binding = bind_final_continuation_evidence(
        continuation_result_path=result_path,
        continuation_job_id="6a6b8a78b36a6516e96a2fb1",
        continuation_running_seconds=863,
        expected_packet_tree_sha256=packet_tree,
        remote_model_uri=remote_uri,
        total_budget_usd="5.000",
    )

    assert binding["continuation_evidence"] == {
        "schema_version": 1,
        "kind": "scriber_final_evaluation_continuation_evidence",
        "job_id": "6a6b8a78b36a6516e96a2fb1",
        "terminal_status": "COMPLETED",
        "owner": "Buttermilk03",
        "flavor": "a10g-small",
        "timeout_minutes": 30,
        "running_seconds": 863,
        "billed_minutes": 15,
        "hourly_rate_usd": "1.00",
        "actual_job_cost_usd": "0.250000",
        "historical_cost_usd": "0.893",
        "actual_campaign_cost_usd": "1.143000",
        "remote_result_uri": (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
        ),
        "continuation_packet_tree_sha256": packet_tree,
        "continuation_manifest_sha256": binding["continuation_evidence"]["continuation_manifest_sha256"],
        "packet_verification_sha256": binding["continuation_evidence"]["packet_verification_sha256"],
        "cost_audit_sha256": binding["continuation_evidence"]["cost_audit_sha256"],
        "continuation_result_sha256": binding["continuation_evidence"]["continuation_result_sha256"],
        "training_report_sha256": binding["continuation_evidence"]["training_report_sha256"],
    }
    assert binding["final_model"]["remote_model_uri"] == remote_uri
    assert binding["budget"]["maximum_total_cost_usd"] == "2.743000"


def test_final_evaluation_binds_attempt8_attempt9_and_cpu_finalization_without_retraining(
    tmp_path: Path,
) -> None:
    finalization_job_id = "6a6b99a8b36a6516e96a30aa"
    finalization_packet_tree = "sha256:" + "7" * 64
    result_path = _write_no_retrain_finalization_evidence(
        tmp_path,
        finalization_job_id=finalization_job_id,
        finalization_packet_tree_sha256=finalization_packet_tree,
    )
    original_packet_tree = "sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"

    binding = bind_final_continuation_evidence(
        continuation_result_path=result_path,
        continuation_job_id=ATTEMPT8_JOB_ID,
        continuation_running_seconds=912,
        expected_packet_tree_sha256=original_packet_tree,
        remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
        total_budget_usd="5.000",
        finalization_job_id=finalization_job_id,
        finalization_running_seconds=42,
        expected_finalization_packet_tree_sha256=finalization_packet_tree,
    )

    evidence = binding["continuation_evidence"]
    assert evidence["kind"] == ("scriber_final_evaluation_recovered_continuation_evidence")
    assert evidence["job_id"] == ATTEMPT8_JOB_ID
    assert evidence["terminal_status"] == "ERROR"
    assert evidence["running_seconds"] == 912
    assert evidence["actual_job_cost_usd"] == "0.266667"
    assert evidence["continuation_packet_tree_sha256"] == original_packet_tree
    assert evidence["recovery_chain"]["recovery_job_id"] == ATTEMPT9_JOB_ID
    assert evidence["recovery_chain"]["recovery_job_cost_usd"] == "0.033333"
    assert evidence["recovery_chain"]["finalization_job_id"] == (finalization_job_id)
    assert evidence["recovery_chain"]["finalization_job_cost_usd"] == ("0.000167")
    assert evidence["actual_campaign_cost_usd"] == "0.893167"
    assert binding["final_model"]["remote_model_uri"] == (ATTEMPT9_FINAL_MODEL_URI)
    assert binding["budget"]["actual_before_evaluation_usd"] == "0.893167"
    assert binding["budget"]["maximum_total_cost_usd"] == "2.493167"


def test_final_evaluation_binds_the_actual_completed_attempt10_evidence() -> None:
    project = Path(__file__).parents[1]
    result = (
        project / "artifacts/cloud/hf-training-continuation-finalization-attempt10-complete/continuation-result.json"
    )

    binding = bind_final_continuation_evidence(
        continuation_result_path=result,
        continuation_job_id=ATTEMPT8_JOB_ID,
        continuation_running_seconds=912,
        expected_packet_tree_sha256=("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
        remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
        total_budget_usd="5.000",
        finalization_job_id="6a6b9ee723ed89c748ec83a1",
        finalization_running_seconds=20,
        expected_finalization_packet_tree_sha256=(
            "sha256:2db2a308dcfe5807209100fd823453619428435dbeff5b4679d5dc25dabd80a5"
        ),
    )

    assert binding["continuation_evidence"]["actual_campaign_cost_usd"] == ("0.893167")
    assert binding["continuation_evidence"]["recovery_chain"]["finalization_job_id"] == "6a6b9ee723ed89c748ec83a1"
    assert binding["final_model"]["remote_model_uri"] == (ATTEMPT9_FINAL_MODEL_URI)
    assert binding["budget"]["maximum_total_cost_usd"] == "2.493167"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("training_steps_executed", 1),
        ("evaluation_executed", True),
        ("model_copied", True),
        ("optimizer_state_loaded", True),
        ("validation_reexecuted", True),
        ("source_job_id", ATTEMPT8_JOB_ID),
    ),
)
def test_final_evaluation_rejects_a_changed_no_retrain_recovery_claim(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    finalization_job_id = "6a6b99a8b36a6516e96a30aa"
    finalization_packet_tree = "sha256:" + "7" * 64
    result_path = _write_no_retrain_finalization_evidence(
        tmp_path,
        finalization_job_id=finalization_job_id,
        finalization_packet_tree_sha256=finalization_packet_tree,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result[field] = value
    result_path.write_bytes(canonical_json_bytes(result))

    with pytest.raises(
        FinalEvaluationError,
        match="not accepted",
    ):
        bind_final_continuation_evidence(
            continuation_result_path=result_path,
            continuation_job_id=ATTEMPT8_JOB_ID,
            continuation_running_seconds=912,
            expected_packet_tree_sha256=("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
            remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
            total_budget_usd="5.000",
            finalization_job_id=finalization_job_id,
            finalization_running_seconds=42,
            expected_finalization_packet_tree_sha256=(finalization_packet_tree),
        )


def test_final_evaluation_rejects_wrong_original_or_finalization_packet_identity(
    tmp_path: Path,
) -> None:
    finalization_job_id = "6a6b99a8b36a6516e96a30aa"
    finalization_packet_tree = "sha256:" + "7" * 64
    result_path = _write_no_retrain_finalization_evidence(
        tmp_path,
        finalization_job_id=finalization_job_id,
        finalization_packet_tree_sha256=finalization_packet_tree,
    )

    with pytest.raises(FinalEvaluationError, match="original Attempt-8"):
        bind_final_continuation_evidence(
            continuation_result_path=result_path,
            continuation_job_id=ATTEMPT8_JOB_ID,
            continuation_running_seconds=912,
            expected_packet_tree_sha256="sha256:" + "8" * 64,
            remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
            total_budget_usd="5.000",
            finalization_job_id=finalization_job_id,
            finalization_running_seconds=42,
            expected_finalization_packet_tree_sha256=(finalization_packet_tree),
        )
    with pytest.raises(
        FinalEvaluationError,
        match="not cross-bound",
    ):
        bind_final_continuation_evidence(
            continuation_result_path=result_path,
            continuation_job_id=ATTEMPT8_JOB_ID,
            continuation_running_seconds=912,
            expected_packet_tree_sha256=("sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"),
            remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
            total_budget_usd="5.000",
            finalization_job_id=finalization_job_id,
            finalization_running_seconds=42,
            expected_finalization_packet_tree_sha256="sha256:" + "8" * 64,
        )


def test_recovered_finalization_packet_carries_all_five_cross_bound_files(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    finalization_job_id = "6a6b99a8b36a6516e96a30aa"
    finalization_packet_tree = "sha256:" + "7" * 64
    result_path = _write_no_retrain_finalization_evidence(
        tmp_path,
        finalization_job_id=finalization_job_id,
        finalization_packet_tree_sha256=finalization_packet_tree,
    )

    packet = build_hf_final_evaluation_packet(
        continuation_result_path=result_path,
        continuation_job_id=ATTEMPT8_JOB_ID,
        continuation_running_seconds=912,
        expected_continuation_packet_tree_sha256=(
            "sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2"
        ),
        remote_model_uri=ATTEMPT9_FINAL_MODEL_URI,
        total_budget_usd="5.000",
        reference_root=(project / "artifacts/evaluation/final_1100_v1/references"),
        output_dir=tmp_path / "packet",
        finalization_job_id=finalization_job_id,
        finalization_running_seconds=42,
        expected_finalization_packet_tree_sha256=finalization_packet_tree,
    )

    job_input = packet.output_dir / "repo/ml/scriber_polishing/job-input"
    for filename in (
        "attempt9-finalization-source-contract.json",
        "attempt9-finalization-verification.json",
        "continuation-result.json",
        "finalization-launch-guard.json",
        "training-report.json",
    ):
        assert (job_input / filename).is_file()
    assert packet.manifest["continuation_evidence"]["recovery_chain"]["finalization_job_id"] == finalization_job_id
    assert packet.manifest["budget"]["actual_before_evaluation_usd"] == ("0.893167")
    verified = verify_hf_final_evaluation_packet(
        packet.output_dir,
        expected_packet_tree_sha256=packet.manifest["packet_tree_sha256"],
    )
    assert verified == packet.manifest


def test_final_evaluation_binds_only_the_exact_private_1100_model(
    tmp_path: Path,
) -> None:
    result_path, report_path = _write_continuation_evidence(tmp_path)
    remote_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "attempt8/scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
    )
    remote_result_uri = "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"

    binding = bind_final_continuation_model(
        continuation_result_path=result_path,
        training_report_path=report_path,
        remote_model_uri=remote_uri,
        remote_result_uri=remote_result_uri,
    )

    assert binding["cumulative_updates"] == 1100
    assert binding["replayed_updates"] == 0
    assert binding["final_model_directory"] == _TEST_FINAL_DIRECTORY
    assert binding["final_model_tree_sha256"] == "sha256:" + "5" * 64
    assert binding["remote_model_uri"] == remote_uri
    assert binding["mount_mode"] == "read_only"


def test_final_evaluation_rejects_a_public_or_wrong_model_mount(
    tmp_path: Path,
) -> None:
    result_path, report_path = _write_continuation_evidence(tmp_path)

    with pytest.raises(
        FinalEvaluationError,
        match="exact private continuation model",
    ):
        bind_final_continuation_model(
            continuation_result_path=result_path,
            training_report_path=report_path,
            remote_model_uri=(f"hf://models/Buttermilk03/scriber-public/{_TEST_FINAL_DIRECTORY}"),
            remote_result_uri=(
                "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
            ),
        )


def test_final_evaluation_rejects_a_quantized_continuation_result(
    tmp_path: Path,
) -> None:
    result_path, report_path = _write_continuation_evidence(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["quantization_performed"] = True
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(
        FinalEvaluationError,
        match="exact cumulative-1100 model",
    ):
        bind_final_continuation_model(
            continuation_result_path=result_path,
            training_report_path=report_path,
            remote_model_uri=(
                "hf://buckets/Buttermilk03/"
                "scriber-polishing-private-runs/attempt8/"
                "scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
            ),
            remote_result_uri=(
                "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
            ),
        )


def test_final_evaluation_runner_is_inference_only_resumable_and_policy_explicit() -> None:
    runner = final_evaluation_runner_payload().decode("utf-8")

    assert "export PYTHONDONTWRITEBYTECODE=1" in runner
    assert runner.index("export PYTHONDONTWRITEBYTECODE=1") < runner.index(
        'export PYTHONPATH="$SOURCE_REPO/ml/scriber_polishing/src"'
    )
    assert runner.index("export PYTHONDONTWRITEBYTECODE=1") < runner.index(
        'python "$SOURCE_REPO/ml/scriber_polishing/scripts/verify_hf_final_evaluation.py"'
    )
    assert 'RUN_MODE="${2:-gpu-evaluation}"' in runner
    assert "cloud_packet_self_verification=complete" in runner
    assert runner.index("--mode packet") < runner.index('if [ "$RUN_MODE" = packet-self-verify ]')
    assert "train_sft.py" not in runner
    assert "push_to_hub" not in runner
    assert "publish_private_hub" not in runner
    assert "hf upload" not in runner
    assert "REMOTE_FINAL_MODEL=/final-model" in runner
    assert 'cp -a --no-preserve=ownership "$REMOTE_FINAL_MODEL"' in runner
    assert "verify_frozen_lexical_policy_artifact" in runner
    assert "--references" in runner
    assert "--batch-size 8" in runner
    assert runner.count("--resume-journal") == 2
    assert "final/$suite_id" in runner
    assert "base_model/$suite_id" in runner
    assert "final/$SUITE_ID" not in runner
    assert "base_model/$SUITE_ID" not in runner
    assert runner.count("--mode plan-predictions") == 2
    assert runner.count("--mode bind-prediction") == 4
    assert "--mode recover-exit" in runner
    assert "--mode claim-launch" in runner
    assert ('--manifest "$SOURCE_REPO/ml/scriber_polishing/job-input/final-evaluation-manifest.json"') in runner
    assert "--mode release-writer" in runner
    assert "uuid.uuid4().hex" in runner
    assert 'os.environ.get("JOB_ID", "")' in runner
    assert "hashlib.sha256(job_id.encode" in runner
    assert "job-manifest.json" in runner
    assert 'sync "$destination"' in runner
    assert 'sync "$lane_root/evaluation.json"' in runner
    assert f"BASE_MODEL_ID={BASE_MODEL_ID}" in runner
    assert f"BASE_MODEL_REVISION={BASE_MODEL_REVISION}" in runner
    assert "REMOTE_BASE_MODEL=/base-model" in runner
    assert "snapshot_download" not in runner
    assert "huggingface-hub==" not in runner
    assert "HF_TOKEN" not in runner
    assert '--protection-policy-artifact "$LOCAL_FINAL_MODEL/' in runner
    assert "BASE_POLICY_MODE=implicit_legacy_default" in runner
    base_block = runner.split("run_base_suite()", maxsplit=1)[1]
    assert (
        "--protection-policy-artifact"
        not in base_block.split(
            "finalize",
            maxsplit=1,
        )[0]
    )
    assert "scripts/evaluate_predictions.py" in runner
    assert "evaluation-exit.json" in runner
    assert "training_steps_executed" in runner
    assert "continuation-packet-verification.json" in runner


def test_packet_inventory_uses_platform_independent_case_sensitive_posix_order(
    tmp_path: Path,
) -> None:
    payloads = {
        "README.md": b"readme\n",
        "repo/B.py": b"B\n",
        "repo/Z.py": b"Z\n",
        "repo/a.py": b"a\n",
    }
    simulated_windows_order = ["repo/a.py", "repo/B.py", "README.md", "repo/Z.py"]
    simulated_linux_order = ["README.md", "repo/B.py", "repo/Z.py", "repo/a.py"]
    inventories = []
    trees = []
    for root_name, creation_order in (
        ("windows-upload", simulated_windows_order),
        ("linux-mount", simulated_linux_order),
    ):
        root = tmp_path / root_name
        for relative in creation_order:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payloads[relative])
        inventory, tree = _packet_inventory(root)
        inventories.append(inventory)
        trees.append(tree)

    assert [item["path"] for item in inventories[0]] == [
        "README.md",
        "repo/B.py",
        "repo/Z.py",
        "repo/a.py",
    ]
    assert inventories[0] == inventories[1]
    assert trees == [
        "sha256:65fa2b9e5feba99b743789a06c7e3a5d7224fdfb4b176c090da6bb66a29a8efa",
        "sha256:65fa2b9e5feba99b743789a06c7e3a5d7224fdfb4b176c090da6bb66a29a8efa",
    ]


def test_retry_packet_self_verification_writes_no_bytecode_and_inventory_rejects_it(
    tmp_path: Path,
) -> None:
    packet = _build_actual_retry_packet(tmp_path / "packet")
    project = packet.output_dir / "repo/ml/scriber_polishing"
    launch = build_hf_final_evaluation_launch_contract(packet.output_dir)
    cpu_launch = build_hf_final_evaluation_launch_contract(
        packet.output_dir,
        launch_kind="cpu-self-verify",
    )
    assert packet.manifest["retry_evidence"]["failed_evaluations"][0]["job_id"] == ("6a6ba10023ed89c748ec83f1")
    assert packet.manifest["budget"]["actual_before_evaluation_usd"] == "0.933501"
    assert launch["retry_evidence"] == packet.manifest["retry_evidence"]
    assert launch["maximum_total_cost_usd"] == "2.533501"
    assert cpu_launch["flavor"] == "cpu-basic"
    assert cpu_launch["packet_self_verification_only"] is True
    assert cpu_launch["maximum_job_cost_usd"] == "0.001667"
    assert cpu_launch["maximum_total_cost_usd"] == "0.935168"
    assert cpu_launch["argv"][-1] == "packet-self-verify"
    assert cpu_launch["argv"].count("-v") == 1
    assert not any("/final-model" in argument for argument in cpu_launch["argv"])
    assert not list(packet.output_dir.rglob("__pycache__"))
    assert not list(packet.output_dir.rglob("*.pyc"))

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(project / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(project / "scripts/verify_hf_final_evaluation.py"),
            "--mode",
            "packet",
            "--packet-root",
            str(packet.output_dir),
            "--expected-packet-tree-sha256",
            str(packet.manifest["packet_tree_sha256"]),
        ],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not list(packet.output_dir.rglob("__pycache__"))
    assert not list(packet.output_dir.rglob("*.pyc"))

    forged_cache = project / "src/scriber_polishing/__pycache__"
    forged_cache.mkdir()
    (forged_cache / "forged.pyc").write_bytes(b"not bytecode")
    with pytest.raises(FinalEvaluationError, match="may not contain Python bytecode"):
        verify_hf_final_evaluation_packet(
            packet.output_dir,
            expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
        )


def test_final_evaluation_packet_and_launch_are_private_bounded_and_read_only(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    result_path, _ = _write_continuation_evidence(tmp_path)
    continuation_manifest_path, _ = _write_continuation_budget_evidence(tmp_path)
    packet_tree = "sha256:" + "6" * 64
    _write_continuation_packet_verification(
        tmp_path,
        continuation_manifest_path=continuation_manifest_path,
        packet_tree_sha256=packet_tree,
    )
    remote_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "attempt8/scriber-1100-continuation-v1/training/" + _TEST_FINAL_DIRECTORY
    )
    packet_dir = tmp_path / "final-evaluation-packet"

    packet = build_hf_final_evaluation_packet(
        continuation_result_path=result_path,
        continuation_job_id="6a6b8a78b36a6516e96a2fb1",
        continuation_running_seconds=863,
        expected_continuation_packet_tree_sha256=packet_tree,
        remote_model_uri=remote_uri,
        total_budget_usd="5.000",
        reference_root=(project / "artifacts/evaluation/final_1100_v1/references"),
        output_dir=packet_dir,
    )
    launch = build_hf_final_evaluation_launch_contract(packet_dir)

    assert packet.manifest["base_model"] == {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "remote_model_uri": ("hf://models/google/gemma-3-270m-it@ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"),
        "mount_mode": "read_only",
        "tree_sha256": ("sha256:0d610d30384d641366232e4bd8ad556c6a28470781e68e93757fe9e4ae1a0f57"),
        "config_sha256": ("sha256:8983343e5bfbf8e336d222a522a1d781443491e9de2d081b8856f8fd26ab6046"),
        "file_count": 11,
        "total_bytes": 575485707,
        "preprocessing_policy": "implicit_legacy_default",
    }
    assert packet.manifest["final_model"]["preprocessing_policy"] == {
        "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
        "policy_id": "gemma_lexical_v1",
        "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
    }
    assert packet.manifest["training"] == {
        "training_steps_executed": 0,
        "reference_usage": "inference_and_evaluation_only",
        "train_split_allowed": False,
    }
    assert packet.manifest["budget"]["maximum_total_cost_usd"] == "2.743000"
    assert packet.manifest["budget"]["strictly_below_total_budget"] is True
    assert packet.manifest["continuation_evidence"]["actual_campaign_cost_usd"] == "1.143000"
    assert packet.manifest["publication"] == {
        "allowed": False,
        "destination_kind": "private_hf_bucket",
    }
    assert launch["timeout"] == "120m"
    assert launch["flavor"] == "l4x1"
    assert launch["model_volume"] == f"{remote_uri}:/final-model:ro"
    assert launch["output_volume"] == ("hf://buckets/Buttermilk03/scriber-polishing-private-runs:/outputs:rw")
    assert launch["output_root"] == ("/outputs/attempt8/scriber-1100-final-evaluation-v1")
    assert launch["base_model_volume"] == (
        "hf://models/google/gemma-3-270m-it@ac82b4e820549b854eebf28ce6dedaf9fdfa17b3:/base-model:ro"
    )
    assert launch["argv"].count("-v") == 4
    assert launch["argv"][launch["argv"].index("--timeout") + 1] == "120m"
    assert launch["external_job_started"] is False
    assert launch["maximum_total_cost_usd"] == "2.743000"
    assert not any("upload" in argument for argument in launch["argv"])
    assert (
        packet_dir / "repo/ml/scriber_polishing/evaluation-input/ai_gold_automatic_test_challenge.references.jsonl"
    ).is_file()
    assert (
        packet_dir / "repo/ml/scriber_polishing/evaluation-input/critical_regression_as_challenge.references.jsonl"
    ).is_file()


def test_final_evaluation_packet_binds_a10g_flavor_cost_and_launch(
    tmp_path: Path,
) -> None:
    packet = _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=tmp_path / "a10g-packet",
        evaluation_flavor="a10g-small",
    )
    launch = build_hf_final_evaluation_launch_contract(packet.output_dir)

    assert packet.manifest["flavor"] == "a10g-small"
    assert packet.manifest["budget"]["evaluation_maximum_cost_usd"] == "2.000"
    assert packet.manifest["budget"]["maximum_total_cost_usd"] == "3.143000"
    assert launch["flavor"] == "a10g-small"
    assert launch["maximum_job_cost_usd"] == "2.000"
    assert launch["maximum_total_cost_usd"] == "3.143000"
    assert launch["argv"][launch["argv"].index("--flavor") + 1] == "a10g-small"
    assert verify_hf_final_evaluation_packet(
        packet.output_dir,
        expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
    ) == packet.manifest


def test_final_evaluation_core_rejects_any_fourth_suite(
    tmp_path: Path,
) -> None:
    packet_dir = tmp_path / "packet"
    _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    source = packet_dir / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    forged = json.loads(source.read_text(encoding="utf-8"))
    forged["references"]["unexpected_fourth_suite"] = dict(forged["references"]["ai_gold"])
    forged["evaluation"]["reports_per_candidate"] = 4
    forged_path = tmp_path / "forged-core.json"
    forged_path.write_bytes(canonical_json_bytes(forged))

    with pytest.raises(FinalEvaluationError, match="exact three frozen suites"):
        list_final_evaluation_suites(forged_path)


@pytest.mark.parametrize("candidate", ("final", "base_model"))
def test_final_evaluation_prediction_manifest_binds_the_exact_preflight_model(
    tmp_path: Path,
    candidate: str,
) -> None:
    packet_dir = tmp_path / "packet"
    _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    core = packet_dir / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    model_root = tmp_path / ("final-model" if candidate == "final" else "base-model")
    model_root.mkdir()
    prediction_manifest = {
        "schema_version": 1,
        "candidate": candidate,
        "model": str(model_root.resolve()),
        "revision": None,
        "record_count": 1,
        "valid_sst_count": 1,
        "restoration_error_count": 0,
        "prediction_sha256": "0" * 64,
        "total_generation_seconds": 0.1,
        "total_input_tokens": 1,
        "total_output_tokens": 1,
        "batch_size": 8,
        "reference_sha256": "1" * 64,
        "quantization": "none",
        "device": "cuda",
    }
    if candidate == "final":
        prediction_manifest["protection_policy"] = {
            "source": "explicit_artifact",
            "path": str(model_root / "scriber-protection-policy.json"),
            "artifact_sha256": "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce",
        }
    manifest_path = tmp_path / f"{candidate}.manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(prediction_manifest))

    bound = bind_final_evaluation_prediction_manifest(
        manifest_path=core,
        prediction_manifest_path=manifest_path,
        candidate=candidate,
        model_root=model_root,
        allow_create=True,
    )
    verified = bind_final_evaluation_prediction_manifest(
        manifest_path=core,
        prediction_manifest_path=manifest_path,
        candidate=candidate,
        model_root=model_root,
        allow_create=False,
    )
    manifest_path.write_bytes(canonical_json_bytes(prediction_manifest))
    manifest_temporary = manifest_path.with_name(manifest_path.name + ".tmp")
    manifest_temporary.write_bytes(canonical_json_bytes(bound))
    recovered = bind_final_evaluation_prediction_manifest(
        manifest_path=core,
        prediction_manifest_path=manifest_path,
        candidate=candidate,
        model_root=model_root,
        allow_create=False,
    )

    assert verified == bound
    assert recovered == bound
    assert manifest_path.read_bytes() == canonical_json_bytes(bound)
    assert not manifest_temporary.exists()
    identity = bound["final_evaluation_model_identity"]
    if candidate == "final":
        assert identity["model_tree_sha256"] == "sha256:" + "5" * 64
        assert identity["cumulative_updates"] == 1100
        assert identity["preprocessing_policy"]["policy_id"] == "gemma_lexical_v1"
    else:
        assert identity["model_id"] == BASE_MODEL_ID
        assert identity["revision"] == BASE_MODEL_REVISION
        assert identity["tree_sha256"] == ("sha256:0d610d30384d641366232e4bd8ad556c6a28470781e68e93757fe9e4ae1a0f57")


def test_final_evaluation_preexisting_prediction_manifest_requires_model_identity(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "predictions.manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FinalEvaluationError, match="has no exact model identity"):
        bind_final_evaluation_prediction_manifest(
            manifest_path=tmp_path / "missing-core.json",
            prediction_manifest_path=manifest_path,
            candidate="final",
            model_root=tmp_path / "final-model",
            allow_create=False,
        )


def test_final_evaluation_recovers_gate_exit_and_matching_known_temporary(
    tmp_path: Path,
) -> None:
    packet_dir = tmp_path / "packet"
    _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    package_project = packet_dir / "repo/ml/scriber_polishing"
    core = package_project / "job-input/final-evaluation-manifest.json"
    references = package_project / "evaluation-input/ai_gold_automatic_test_challenge.references.jsonl"
    reference_sha256 = hashlib.sha256(references.read_bytes()).hexdigest()
    lane = tmp_path / "final" / "ai_gold"
    lane.mkdir(parents=True)
    predictions = lane / "predictions.jsonl"
    predictions.write_text("{}\n", encoding="utf-8")
    prediction_sha256 = hashlib.sha256(predictions.read_bytes()).hexdigest()
    (lane / "predictions.manifest.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "candidate": "final",
                "model": "/workspace/scriber-1100-final-evaluation/final-model",
                "revision": None,
                "record_count": 1750,
                "valid_sst_count": 1750,
                "restoration_error_count": 0,
                "prediction_sha256": prediction_sha256,
                "total_generation_seconds": 1.0,
                "total_input_tokens": 1,
                "total_output_tokens": 1,
                "batch_size": 8,
                "reference_sha256": reference_sha256,
                "quantization": "none",
                "device": "cuda",
                "protection_policy": {
                    "source": "explicit_artifact",
                    "path": "/workspace/scriber-1100-final-evaluation/final-model/scriber-protection-policy.json",
                    "artifact_sha256": "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
                    "policy_id": "gemma_lexical_v1",
                    "policy_sha256": "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce",
                },
                "final_evaluation_model_identity": {
                    "kind": "scriber_final_evaluation_prediction_model_identity",
                    "candidate": "final",
                    "cumulative_updates": 1100,
                    "model_tree_sha256": "sha256:" + "5" * 64,
                    "model_file_count": 12,
                    "model_total_bytes": 575466744,
                    "preprocessing_policy": {
                        "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
                        "policy_id": "gemma_lexical_v1",
                        "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
                    },
                },
            }
        )
    )
    evaluation_payload = canonical_json_bytes(
        {
            "schema_version": 1,
            "candidate": "final",
            "evaluation_config_sha256": ("656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998"),
            "reference_sha256": reference_sha256,
            "prediction_sha256": prediction_sha256,
            "exact_case_coverage": True,
            "metrics": {},
            "gate": {"status": "failed", "passed": False},
        }
    )
    (lane / "evaluation.json").write_bytes(evaluation_payload)
    (lane / "evaluation.json.tmp").write_bytes(evaluation_payload)
    expected_exit = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_gate_exit",
        "candidate": "final",
        "suite": "ai_gold",
        "evaluation_exit": 2,
    }
    (lane / "evaluation-exit.json.tmp").write_bytes(canonical_json_bytes(expected_exit))

    recovered = recover_final_evaluation_exit(
        manifest_path=core,
        candidate="final",
        suite_id="ai_gold",
        lane_root=lane,
    )

    assert recovered == expected_exit
    assert (lane / "evaluation-exit.json").read_bytes() == canonical_json_bytes(expected_exit)
    assert not (lane / "evaluation-exit.json.tmp").exists()
    assert not (lane / "evaluation.json.tmp").exists()
    conflicting_exit = {**expected_exit, "evaluation_exit": 0}
    (lane / "evaluation-exit.json.tmp").write_bytes(canonical_json_bytes(conflicting_exit))
    with pytest.raises(FinalEvaluationError, match="temporary output differs"):
        recover_final_evaluation_exit(
            manifest_path=core,
            candidate="final",
            suite_id="ai_gold",
            lane_root=lane,
        )
    assert (lane / "evaluation-exit.json").read_bytes() == canonical_json_bytes(expected_exit)


def test_final_evaluation_output_provenance_binds_packet_tree_and_runner(
    tmp_path: Path,
) -> None:
    packet_dir = tmp_path / "packet"
    packet = _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "job-manifest.json").write_bytes((packet_dir / "job-manifest.json").read_bytes())
    core = packet_dir / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"

    provenance = verify_final_evaluation_output_provenance(
        manifest_path=core,
        output_root=output_root,
    )

    assert provenance == {
        "packet_tree_sha256": packet.manifest["packet_tree_sha256"],
        "runner_sha256": packet.manifest["runner_sha256"],
        "source_git_head": packet.manifest["source_git_head"],
    }


def test_final_evaluation_finalizer_binds_exactly_six_lanes_and_recovers_result(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    packet_dir = tmp_path / "packet"
    packet = _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    package_project = packet_dir / "repo/ml/scriber_polishing"
    job_input = package_project / "job-input"
    core_path = job_input / "final-evaluation-manifest.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"
    launch_id = "1" * 32
    owner_token = "a" * 32
    claim_final_evaluation_launch(
        manifest_path=core_path,
        output_root=output_root,
        launch_id=launch_id,
        owner_token=owner_token,
    )
    try:
        for filename in (
            "continuation-manifest.json",
            "continuation-packet-verification.json",
            "continuation-result.json",
            "final-evaluation-manifest.json",
            "hf-cost-audit.json",
            "training-report.json",
        ):
            (output_root / filename).write_bytes((job_input / filename).read_bytes())
        (output_root / "job-manifest.json").write_bytes((packet_dir / "job-manifest.json").read_bytes())
        for filename in (
            "preflight-local.json",
            "preflight-remote.json",
            "preflight-base-local.json",
            "preflight-base-remote.json",
        ):
            (output_root / filename).write_bytes(canonical_json_bytes({"status": "passed"}))

        model_roots = {
            "final": tmp_path / "final-model",
            "base_model": tmp_path / "base-model",
        }
        for model_root in model_roots.values():
            model_root.mkdir()
        for candidate in ("final", "base_model"):
            for suite_id, suite in core["references"].items():
                lane = output_root / candidate / suite_id
                lane.mkdir(parents=True)
                prediction_path = lane / "predictions.jsonl"
                prediction_path.write_text(
                    json.dumps({"suite": suite_id, "candidate": candidate}) + "\n",
                    encoding="utf-8",
                )
                reference_path = package_project / "evaluation-input" / suite["records_filename"]
                prediction_manifest = {
                    "schema_version": 1,
                    "candidate": candidate,
                    "model": str(model_roots[candidate].resolve()),
                    "revision": None,
                    "record_count": suite["record_count"],
                    "valid_sst_count": suite["record_count"],
                    "restoration_error_count": 0,
                    "prediction_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
                    "total_generation_seconds": 1.0,
                    "total_input_tokens": 1,
                    "total_output_tokens": 1,
                    "batch_size": 8,
                    "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
                    "quantization": "none",
                    "device": "cuda",
                }
                if candidate == "final":
                    prediction_manifest["protection_policy"] = {
                        "source": "explicit_artifact",
                        "path": str(model_roots[candidate].resolve() / "scriber-protection-policy.json"),
                        "artifact_sha256": ("8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
                        "policy_id": "gemma_lexical_v1",
                        "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
                    }
                prediction_manifest_path = lane / "predictions.manifest.json"
                prediction_manifest_path.write_bytes(canonical_json_bytes(prediction_manifest))
                bound_prediction_manifest = bind_final_evaluation_prediction_manifest(
                    manifest_path=core_path,
                    prediction_manifest_path=prediction_manifest_path,
                    candidate=candidate,
                    model_root=model_roots[candidate],
                    allow_create=True,
                )
                (lane / "evaluation.json").write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": 1,
                            "candidate": candidate,
                            "evaluation_config_sha256": core["evaluation"]["config_sha256"],
                            "reference_sha256": bound_prediction_manifest["reference_sha256"],
                            "prediction_sha256": bound_prediction_manifest["prediction_sha256"],
                            "exact_case_coverage": True,
                            "metrics": {},
                            "gate": {"status": "passed", "passed": True},
                        }
                    )
                )
                record_final_evaluation_exit(
                    candidate=candidate,
                    suite_id=suite_id,
                    evaluation_exit=0,
                    output_path=lane / "evaluation-exit.json",
                )

        final_result_path = output_root / "final-evaluation-result.json"
        final_result = finalize_hf_final_evaluation(
            manifest_path=core_path,
            output_root=output_root,
            output_path=final_result_path,
        )
        result_temporary = output_root / "final-evaluation-result.json.tmp"
        final_result_path.replace(result_temporary)
        recovered_result = finalize_hf_final_evaluation(
            manifest_path=core_path,
            output_root=output_root,
            output_path=final_result_path,
        )
    finally:
        release_final_evaluation_writer(
            output_root=output_root,
            launch_id=launch_id,
            owner_token=owner_token,
        )

    assert recovered_result == final_result
    assert final_result["packet"] == {
        "packet_tree_sha256": packet.manifest["packet_tree_sha256"],
        "runner_sha256": packet.manifest["runner_sha256"],
        "source_git_head": packet.manifest["source_git_head"],
    }
    assert set(final_result["reports"]) == {"final", "base_model"}
    assert all(
        set(candidate_reports) == {"ai_gold", "critical_regression", "regular_test"}
        for candidate_reports in final_result["reports"].values()
    )
    assert len(final_result["files"]) == 36
    assert final_result_path.read_bytes() == canonical_json_bytes(final_result)
    assert not result_temporary.exists()
    forged_job_manifest = json.loads(json.dumps(dict(packet.manifest)))
    forged_job_manifest["evaluation"]["unexpected"] = True
    job_schema = json.loads(
        (project / "contracts/hf_final_evaluation_job_manifest_schema.json").read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(job_schema).iter_errors(forged_job_manifest))
    forged_result = json.loads(json.dumps(final_result))
    forged_result["final_model"]["unexpected"] = True
    result_schema = json.loads(
        (project / "contracts/hf_final_evaluation_result_schema.json").read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(result_schema).iter_errors(forged_result))


def test_final_evaluation_packet_refuses_to_overwrite(
    tmp_path: Path,
) -> None:
    packet_dir = tmp_path / "existing"
    packet_dir.mkdir()

    with pytest.raises(FinalEvaluationError, match="refusing to overwrite"):
        _build_test_final_evaluation_packet(
            tmp_path,
            output_dir=packet_dir,
        )


def test_final_evaluation_preparer_binds_an_explicit_a10g_launch(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[1]
    packet_dir = tmp_path / "a10g-packet"
    launch_output = tmp_path / "a10g-launch.json"
    command = [
        sys.executable,
        str(project / "scripts/prepare_hf_final_evaluation_job.py"),
        "--continuation-result",
        str(project / "artifacts/cloud/hf-training-continuation-finalization-attempt10-complete/continuation-result.json"),
        "--continuation-job-id",
        ATTEMPT8_JOB_ID,
        "--continuation-running-seconds",
        "912",
        "--expected-continuation-packet-tree-sha256",
        "sha256:fbf4c0f0eaac705ec6539540be5380d6404c49dbfd36d476a9b4648ec1143bb2",
        "--remote-model-uri",
        ATTEMPT9_FINAL_MODEL_URI,
        "--finalization-job-id",
        "6a6b9ee723ed89c748ec83a1",
        "--finalization-running-seconds",
        "20",
        "--expected-finalization-packet-tree-sha256",
        "sha256:2db2a308dcfe5807209100fd823453619428435dbeff5b4679d5dc25dabd80a5",
    ]
    for option, jobs in (
        ("--failed-evaluation", _FAILED_EVALUATIONS),
        ("--auxiliary-diagnostic", _AUXILIARY_DIAGNOSTICS),
    ):
        for job in jobs:
            command.extend(
                [
                    option,
                    str(job["job_id"]),
                    str(job["flavor"]),
                    str(job["terminal_status"]),
                    str(job["running_seconds"]),
                    str(job["packet_tree_sha256"]),
                ]
            )
    command.extend(
        [
            "--gpu-flavor",
            "a10g-small",
            "--launch-kind",
            "gpu-evaluation",
            "--reference-root",
            str(project / "artifacts/evaluation/final_1100_v1/references"),
            "--output-dir",
            str(packet_dir),
            "--launch-contract-output",
            str(launch_output),
        ]
    )

    completed = subprocess.run(
        command,
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((packet_dir / "job-manifest.json").read_bytes())
    launch = json.loads(launch_output.read_bytes())
    assert manifest["flavor"] == "a10g-small"
    assert manifest["budget"]["evaluation_maximum_cost_usd"] == "2.000"
    assert launch["flavor"] == "a10g-small"
    assert launch["maximum_job_cost_usd"] == "2.000"
    assert launch["argv"][launch["argv"].index("--flavor") + 1] == "a10g-small"


@pytest.mark.parametrize(
    ("launch_inside_packet", "expected_error"),
    (
        (False, "refusing to overwrite final-evaluation launch contract"),
        (True, "final-evaluation launch contract must be outside the immutable packet"),
    ),
)
def test_final_evaluation_preparer_fails_before_leaving_a_partial_packet(
    tmp_path: Path,
    launch_inside_packet: bool,
    expected_error: str,
) -> None:
    packet_dir = tmp_path / "packet"
    launch_output = packet_dir / "launch.json" if launch_inside_packet else tmp_path / "launch.json"
    if not launch_inside_packet:
        launch_output.write_text("{}\n", encoding="utf-8")

    project = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project / "scripts/prepare_hf_final_evaluation_job.py"),
            "--continuation-result",
            str(tmp_path / "missing-continuation-result.json"),
            "--continuation-job-id",
            "6a6b8a78b36a6516e96a2fb1",
            "--continuation-running-seconds",
            "863",
            "--expected-continuation-packet-tree-sha256",
            "sha256:" + "6" * 64,
            "--remote-model-uri",
            "hf://buckets/example/missing",
            "--failed-evaluation",
            "6a6ba10023ed89c748ec83f1",
            "l4x1",
            "ERROR",
            "63",
            "sha256:9f239bdbaa6b21292863d63dbb525cc13dcd085d625d4dd839a1a5170081be93",
            "--failed-evaluation",
            "6a6ba85b23ed89c748ec8491",
            "l4x1",
            "ERROR",
            "45",
            "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749",
            "--auxiliary-diagnostic",
            "6a6ba991b36a6516e96a31bc",
            "cpu-basic",
            "COMPLETED",
            "23",
            "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749",
            "--auxiliary-diagnostic",
            "6a6ba9d8b36a6516e96a31be",
            "cpu-basic",
            "COMPLETED",
            "24",
            "sha256:1160f561994f0f430f9f2f2ef87bee7c16236017ff972455fa83c14a960a2749",
            "--launch-kind",
            "cpu-self-verify",
            "--reference-root",
            str(tmp_path / "missing-references"),
            "--output-dir",
            str(packet_dir),
            "--launch-contract-output",
            str(launch_output),
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert expected_error in completed.stderr
    assert not packet_dir.exists()


def test_final_evaluation_rejects_an_unexpected_gate_exit(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        FinalEvaluationError,
        match="gate exit must be 0 or 2",
    ):
        record_final_evaluation_exit(
            candidate="final",
            suite_id="ai_gold",
            evaluation_exit=1,
            output_path=tmp_path / "evaluation-exit.json",
        )


@pytest.mark.parametrize(
    ("predictions", "manifest", "journal", "expected"),
    (
        (False, False, False, "start"),
        (False, False, True, "resume"),
        (True, False, True, "resume"),
        (False, True, True, "resume"),
        (True, True, True, "resume"),
        (True, True, False, "verify"),
    ),
)
def test_final_evaluation_prediction_resume_matrix_routes_safe_states(
    tmp_path: Path,
    predictions: bool,
    manifest: bool,
    journal: bool,
    expected: str,
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    if predictions:
        (lane / "predictions.jsonl").write_text("{}\n", encoding="utf-8")
    if manifest:
        (lane / "predictions.manifest.json").write_text("{}\n", encoding="utf-8")
    if journal:
        (lane / "predictions.resume.json").write_text("{}\n", encoding="utf-8")

    assert plan_final_evaluation_prediction_lane(lane) == expected


@pytest.mark.parametrize(
    ("predictions", "manifest"),
    (
        (True, False),
        (False, True),
    ),
)
def test_final_evaluation_prediction_resume_matrix_rejects_orphans_without_journal(
    tmp_path: Path,
    predictions: bool,
    manifest: bool,
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    if predictions:
        (lane / "predictions.jsonl").write_text("{}\n", encoding="utf-8")
    if manifest:
        (lane / "predictions.manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FinalEvaluationError, match="orphaned prediction artifact"):
        plan_final_evaluation_prediction_lane(lane)


def test_final_evaluation_prediction_planner_removes_only_known_atomic_temporaries(
    tmp_path: Path,
) -> None:
    lane = tmp_path / "lane"
    lane.mkdir()
    known_temporaries = (
        lane / ".predictions.jsonl.deadbeef.tmp",
        lane / ".predictions.manifest.json.deadbeef.tmp",
        lane / ".predictions.resume.json.deadbeef.tmp",
    )
    for temporary in known_temporaries:
        temporary.write_text("partial", encoding="utf-8")
    unknown_temporary = lane / ".unrecognized-output.deadbeef.tmp"
    unknown_temporary.write_text("retain", encoding="utf-8")

    assert plan_final_evaluation_prediction_lane(lane) == "start"
    assert all(not temporary.exists() for temporary in known_temporaries)
    assert unknown_temporary.exists()


def test_final_evaluation_launch_ledger_is_atomic_bounded_and_resume_idempotent(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_final_evaluation_core_manifest(tmp_path)
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"
    first_launch = "1" * 32
    first_owner = "a" * 32

    first = claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id=first_launch,
        owner_token=first_owner,
    )
    release_final_evaluation_writer(
        output_root=output_root,
        launch_id=first_launch,
        owner_token=first_owner,
    )
    resumed = claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id=first_launch,
        owner_token="b" * 32,
    )

    assert first["new_reservation"] is True
    assert first["ledger"]["evaluation_launch_count"] == 1
    assert first["ledger"]["maximum_total_cost_usd"] == "2.743000"
    assert resumed["new_reservation"] is False
    assert resumed["ledger"] == first["ledger"]
    ledger_path = output_root / "final-evaluation-launch-ledger.json"
    assert ledger_path.read_bytes() == canonical_json_bytes(first["ledger"])

    release_final_evaluation_writer(
        output_root=output_root,
        launch_id=first_launch,
        owner_token="b" * 32,
    )
    second = claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id="2" * 32,
        owner_token="c" * 32,
    )
    assert second["ledger"]["evaluation_launch_count"] == 2
    assert second["ledger"]["maximum_total_cost_usd"] == "4.343000"
    release_final_evaluation_writer(
        output_root=output_root,
        launch_id="2" * 32,
        owner_token="c" * 32,
    )

    with pytest.raises(FinalEvaluationError, match="exceed the approved budget"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="3" * 32,
            owner_token="d" * 32,
        )
    assert not (output_root.parent / ".scriber-1100-final-evaluation-v1.writer-lock").exists()


def test_final_evaluation_launch_ledger_uses_the_packet_bound_a10g_reservation(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_final_evaluation_core_manifest(
        tmp_path,
        evaluation_flavor="a10g-small",
    )
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"

    first = claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )

    assert first["ledger"]["evaluation_maximum_cost_per_launch_usd"] == "2.000"
    assert first["ledger"]["evaluation_launches"][0]["maximum_cost_usd"] == "2.000"
    assert first["ledger"]["maximum_total_cost_usd"] == "3.143000"
    release_final_evaluation_writer(
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )

    with pytest.raises(FinalEvaluationError, match="exceed the approved budget"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="2" * 32,
            owner_token="b" * 32,
        )


def test_final_evaluation_launch_ledger_honors_an_explicitly_increased_budget(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_final_evaluation_core_manifest(
        tmp_path,
        total_budget_usd="6.000",
    )
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"

    for sequence in range(1, 4):
        launch_id = str(sequence) * 32
        owner_token = chr(ord("a") + sequence - 1) * 32
        claim = claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id=launch_id,
            owner_token=owner_token,
        )
        release_final_evaluation_writer(
            output_root=output_root,
            launch_id=launch_id,
            owner_token=owner_token,
        )

    assert claim["ledger"]["evaluation_launch_count"] == 3
    assert claim["ledger"]["maximum_total_cost_usd"] == "5.943000"

    with pytest.raises(FinalEvaluationError, match="exceed the approved budget"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="4" * 32,
            owner_token="d" * 32,
        )


def test_final_evaluation_writer_lock_rejects_a_concurrent_owner(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_final_evaluation_core_manifest(tmp_path)
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"
    claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )

    with pytest.raises(FinalEvaluationError, match="already has an active writer"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="2" * 32,
            owner_token="b" * 32,
        )
    with pytest.raises(FinalEvaluationError, match="already has an active writer"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="1" * 32,
            owner_token="a" * 32,
        )

    release_final_evaluation_writer(
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )


def test_final_evaluation_launch_rejects_an_output_with_a_final_result(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_final_evaluation_core_manifest(tmp_path)
    output_root = tmp_path / "attempt8" / "scriber-1100-final-evaluation-v1"
    claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )
    release_final_evaluation_writer(
        output_root=output_root,
        launch_id="1" * 32,
        owner_token="a" * 32,
    )
    (output_root / "final-evaluation-result.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FinalEvaluationError, match="already contains a final result"):
        claim_final_evaluation_launch(
            manifest_path=manifest_path,
            output_root=output_root,
            launch_id="2" * 32,
            owner_token="b" * 32,
        )

    ledger = json.loads((output_root / "final-evaluation-launch-ledger.json").read_text(encoding="utf-8"))
    assert ledger["evaluation_launch_count"] == 1
    assert not (output_root.parent / ".scriber-1100-final-evaluation-v1.writer-lock").exists()


def test_final_evaluation_launch_rejects_a_tampered_packet(
    tmp_path: Path,
) -> None:
    packet_dir = tmp_path / "packet"
    _build_test_final_evaluation_packet(
        tmp_path,
        output_dir=packet_dir,
    )
    runner = packet_dir / "run-final-evaluation.sh"
    runner.write_bytes(runner.read_bytes() + b"\n# tampered\n")

    with pytest.raises(
        FinalEvaluationError,
        match="immutable inventory",
    ):
        build_hf_final_evaluation_launch_contract(packet_dir)

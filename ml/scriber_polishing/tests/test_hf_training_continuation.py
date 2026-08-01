from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_training_continuation import (
    CONTINUATION_ADDITIONAL_UPDATES,
    CONTINUATION_START_UPDATES,
    CONTINUATION_TARGET_UPDATES,
    HF_COST_AUDIT_JOBS,
    HF_COST_AUDIT_NON_BILLABLE_JOBS,
    TrainingContinuationError,
    _copy_project_tree,
    _copy_training_code_identity_files,
    _packet_inventory,
    _stable_file_binding,
    _verify_full_file_binding,
    _verify_training_code_identity_files,
    build_continuation_postflight_result,
    build_hf_cost_audit,
    build_hf_job_launch_contract,
    claim_continuation_output_guard,
    continuation_runner_payload,
    normalize_continuation_training_order,
    reserve_hf_budget,
    select_continuation_mix,
    validate_continuation_arithmetic,
    verify_accepted_repair_parent,
    verify_continuation_training_report,
    verify_hf_training_continuation_bundle,
    verify_hf_training_continuation_packet,
)


def test_continuation_contract_adds_only_254_new_updates_to_accepted_846() -> None:
    contract = validate_continuation_arithmetic(
        start_updates=846,
        additional_updates=254,
        target_updates=1100,
    )

    assert contract == {
        "start_updates": CONTINUATION_START_UPDATES,
        "additional_updates": CONTINUATION_ADDITIONAL_UPDATES,
        "target_updates": CONTINUATION_TARGET_UPDATES,
        "replayed_updates": 0,
        "parent_initialization": "warm_start_weights_only",
        "same_run_resume": "identity_bound_recovery_only",
    }


@pytest.mark.parametrize(
    ("start", "additional", "target"),
    [
        (845, 255, 1100),
        (846, 253, 1099),
        (846, 254, 1099),
        (865, 235, 1100),
        (700, 400, 1100),
    ],
)
def test_continuation_contract_rejects_any_attempt_to_recompute_old_updates(
    start: int,
    additional: int,
    target: int,
) -> None:
    with pytest.raises(
        TrainingContinuationError,
        match="846 accepted updates.*254 new updates.*1100",
    ):
        validate_continuation_arithmetic(
            start_updates=start,
            additional_updates=additional,
            target_updates=target,
        )


def test_budget_reservation_is_bounded_by_five_dollars() -> None:
    reservation = reserve_hf_budget(
        prior_cost_usd="0.593",
        flavor="l4x1",
        timeout_minutes=30,
    )

    assert reservation == {
        "total_budget_usd": 5.0,
        "prior_cost_usd": 0.593,
        "hourly_rate_usd": 0.8,
        "timeout_minutes": 30,
        "maximum_job_cost_usd": 0.4,
        "maximum_total_cost_usd": 0.993,
        "remaining_after_reservation_usd": 4.007,
    }


def test_a10g_budget_reservation_binds_exact_fallback_cost() -> None:
    reservation = reserve_hf_budget(
        prior_cost_usd="0.593",
        flavor="a10g-small",
        timeout_minutes=30,
    )

    assert reservation == {
        "total_budget_usd": 5.0,
        "prior_cost_usd": 0.593,
        "hourly_rate_usd": 1.0,
        "timeout_minutes": 30,
        "maximum_job_cost_usd": 0.5,
        "maximum_total_cost_usd": 1.093,
        "remaining_after_reservation_usd": 3.907,
    }


def test_cost_audit_binds_every_prior_hf_job_and_rounds_up() -> None:
    audit = build_hf_cost_audit()

    assert len(HF_COST_AUDIT_JOBS) == 20
    assert len({job["job_id"] for job in HF_COST_AUDIT_JOBS}) == 20
    assert audit["counts"] == {"a10g-small": 4, "cpu-basic": 9, "l4x1": 7}
    assert audit["running_seconds"] == {
        "a10g-small": 1242,
        "cpu-basic": 714,
        "l4x1": 2096,
    }
    assert audit["billed_minutes"] == {
        "a10g-small": 23,
        "cpu-basic": 18,
        "l4x1": 38,
    }
    assert audit["billing_increment"] == "per_started_minute"
    assert audit["zero_second_job_minimum_billed_minutes"] == 1
    assert audit["calculated_cost_usd"] == "0.893000"
    assert audit["conservative_prior_cost_usd"] == "0.893"
    a10g_failure = next(job for job in audit["jobs"] if job["job_id"] == "6a6b84adb36a6516e96a2f30")
    assert a10g_failure == {
        "job_id": "6a6b84adb36a6516e96a2f30",
        "flavor": "a10g-small",
        "status": "ERROR",
        "running_seconds": 49,
        "billed_minutes": 1,
        "hourly_rate_usd": "1.00",
        "role": "preflight_cross_platform_path_failure_no_training",
    }
    dependency_failure = next(job for job in audit["jobs"] if job["job_id"] == "6a6b87c8b36a6516e96a2f66")
    assert dependency_failure == {
        "job_id": "6a6b87c8b36a6516e96a2f66",
        "flavor": "a10g-small",
        "status": "ERROR",
        "running_seconds": 188,
        "billed_minutes": 4,
        "hourly_rate_usd": "1.00",
        "role": "code_identity_dependency_missing_no_training",
    }
    publish_failure = next(job for job in audit["jobs"] if job["job_id"] == "6a6b8a78b36a6516e96a2fb1")
    assert publish_failure == {
        "job_id": "6a6b8a78b36a6516e96a2fb1",
        "flavor": "a10g-small",
        "status": "ERROR",
        "running_seconds": 912,
        "billed_minutes": 16,
        "hourly_rate_usd": "1.00",
        "role": "training_complete_publish_io_failure",
    }
    finalization_binding_failure = next(job for job in audit["jobs"] if job["job_id"] == "6a6b94e6b36a6516e96a307c")
    assert finalization_binding_failure == {
        "job_id": "6a6b94e6b36a6516e96a307c",
        "flavor": "a10g-small",
        "status": "ERROR",
        "running_seconds": 93,
        "billed_minutes": 2,
        "hourly_rate_usd": "1.00",
        "role": "recovery_eval_complete_finalization_binding_failure_training_steps_0",
    }
    assert len(HF_COST_AUDIT_NON_BILLABLE_JOBS) == 1
    assert audit["non_billable_jobs"] == [
        {
            "job_id": "6a6b7d4fb36a6516e96a2ec7",
            "flavor": "l4x1",
            "status": "CANCELED",
            "started": False,
            "started_at": None,
            "running_seconds": None,
            "billed_minutes": 0,
            "role": "scheduling_only_canceled_unbilled",
        }
    ]
    assert audit["secret_fields_present"] is False
    runtime = audit["runtime_projection"]
    assert isinstance(runtime, dict)
    assert runtime["training_artifact_job_id"] == "6a6b5cc723ed89c748ec7bad"
    assert runtime["training_artifact_job_running_seconds"] == 567
    assert runtime["training_report_train_runtime_seconds"] == "400.814"
    assert runtime["evaluation_only_job_id"] == "6a6b60ce23ed89c748ec7c11"
    assert runtime["evaluation_only_job_training_steps"] == 0
    assert runtime["projected_continuation_total_seconds"] == "863.493"
    assert runtime["timeout_seconds"] == 1800
    assert runtime["projected_timeout_margin_seconds"] == "936.507"


def test_budget_reservation_fails_closed_when_aggregate_can_exceed_five() -> None:
    with pytest.raises(
        TrainingContinuationError,
        match=r"maximum aggregate HF cost \$5\.10 exceeds the \$5\.00 budget",
    ):
        reserve_hf_budget(
            prior_cost_usd="4.70",
            flavor="l4x1",
            timeout_minutes=30,
        )


_PARENT_MODEL_TREE = "sha256:4b4b758f2fa3e00af282845c42de2887bc714c36a5c8e58e7225a1d9a7d25e06"
_PARENT_DIRECTORY_TREE = "sha256:aa2e12b3366363ed6efe728fff0423d2f19c1147704a1ede1f9b1c6ba65f6c28"
_TRAIN_SHA256 = "59e6f5ef86ca754da4b671d1b3355aaafd78b8092bae64571f544cc6c2f7606a"
_VALIDATION_SHA256 = "b4840b2d06af923af4f28643b32e5de50deef3c6995ebdcb92f89a6fd3859a86"
_PLAN_SHA256 = "de3581bc448833f41ae3f8e8118ab40bcafb9341f7ece31ef886d81e8fcca76a"
_EFFECTIVE_ORDER_SHA256 = "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d"
_POLICY_ARTIFACT_SHA256 = "8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
_POLICY_SELF_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_TOKENIZER_IDENTITY_SHA256 = "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"
_FILTERED_PLAN_ORDER_SHA256 = "625bfc2b54d7403dd41532925795f8e081da7522a45346262a22ba2a9c2fbef3"


def _actual_effective_order() -> dict[str, object]:
    return {
        "bucket_algorithm": "fixed_window_length_descending_v1",
        "effective_epoch_orders": [
            {
                "epoch": 0,
                "record_count": 4064,
                "filtered_plan_example_ids_sha256": _FILTERED_PLAN_ORDER_SHA256,
                "effective_example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
            }
        ],
    }


def test_actual_effective_epoch_evidence_normalizes_to_report_lock() -> None:
    normalized = normalize_continuation_training_order(
        _actual_effective_order(),
        require_actual_epoch_evidence=True,
    )

    assert normalized["effective_epoch_orders"] == _actual_effective_order()["effective_epoch_orders"]
    assert normalized["effective_order_lock"] == {
        "algorithm": "fixed_window_length_descending_v1",
        "example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda order: order.update(effective_epoch_orders=[]),
            "exactly one effective epoch",
        ),
        (
            lambda order: order["effective_epoch_orders"].append(dict(order["effective_epoch_orders"][0])),
            "exactly one effective epoch",
        ),
        (
            lambda order: order["effective_epoch_orders"][0].update(effective_example_ids_sha256="f" * 64),
            "effective order SHA-256",
        ),
        (
            lambda order: order["effective_epoch_orders"][0].update(epoch=1),
            "epoch zero",
        ),
    ],
)
def test_actual_effective_epoch_evidence_fails_closed(
    mutation: object,
    message: str,
) -> None:
    order = _actual_effective_order()
    mutation(order)

    with pytest.raises(TrainingContinuationError, match=message):
        normalize_continuation_training_order(
            order,
            require_actual_epoch_evidence=True,
        )


_BASE_REVISION = "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3"


def _input_binding() -> dict[str, object]:
    return {
        "continuation_manifest_sha256": "sha256:" + "a" * 64,
        "hf_flavor": "l4x1",
        "config_sha256": "sha256:" + "b" * 64,
        "dataset_manifest_sha256": "sha256:" + "c" * 64,
        "train_sha256": "sha256:" + _TRAIN_SHA256,
        "validation_sha256": "sha256:" + _VALIDATION_SHA256,
        "order_plan_sha256": "sha256:" + _PLAN_SHA256,
        "effective_order_sha256": _EFFECTIVE_ORDER_SHA256,
        "protection_policy_artifact_sha256": "sha256:" + _POLICY_ARTIFACT_SHA256,
        "protection_policy_sha256": _POLICY_SELF_SHA256,
        "tokenizer_identity_sha256": _TOKENIZER_IDENTITY_SHA256,
        "parent_directory_tree_sha256": _PARENT_DIRECTORY_TREE,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": _BASE_REVISION,
    }


def _config_values() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_kind": "improvement",
        "base_model": "google/gemma-3-270m-it",
        "base_revision": _BASE_REVISION,
        "method": "full_sft",
        "seed": 270014,
        "epochs": 1,
        "train_examples": 4064,
        "validation_examples": 431,
        "max_sequence_length": 768,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "learning_rate": 0.00001,
        "optimizer": "adamw_torch_fused",
        "eval_steps": 25,
        "save_steps": 25,
        "save_total_limit": 4,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "max_grad_norm": 1.0,
        "bf16": True,
        "gradient_checkpointing": True,
        "assistant_output_loss_only": True,
        "early_stopping": False,
        "protection_policy_path": "training-policy.json",
        "protection_policy_sha256": _POLICY_ARTIFACT_SHA256,
        "parent_checkpoint_path": "parent-checkpoint",
        "parent_checkpoint_tree_sha256": _PARENT_DIRECTORY_TREE,
        "training_order_mode": "shuffled",
        "training_order_plan_path": "order-plan.json",
        "training_order_plan_sha256": _PLAN_SHA256,
    }


def _training_report(
    *,
    output_root: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    root = output_root or Path("/outputs/attempt8/scriber-1100-continuation-v1")
    training_root = root / "training"
    input_binding = _input_binding()
    schedule = {
        "declared_epochs": 1.0,
        "effective_examples_per_step": 16,
        "epoch_boundaries_preserved": True,
        "max_steps": 254,
        "schedule_source": "epochs",
        "train_batches_per_epoch": 2032,
        "update_steps_per_epoch": 254,
    }
    parent = {
        "file_count": 13,
        "path": str(Path("job-input") / "parent-checkpoint"),
        "relationship": "warm_start_parent_not_resume",
        "total_bytes": 575468681,
        "tree_sha256": _PARENT_DIRECTORY_TREE,
    }
    policy = {
        "bytes": 4361,
        "path": str(Path("job-input") / "training-policy.json"),
        "policy_id": "gemma_lexical_v1",
        "policy_sha256": _POLICY_SELF_SHA256,
        "sha256": "sha256:" + _POLICY_ARTIFACT_SHA256,
        "tokenizer": {
            "identity_sha256": _TOKENIZER_IDENTITY_SHA256,
            "model_id": "google/gemma-3-270m-it",
            "revision": _BASE_REVISION,
            "vocab_resize": False,
            "vocab_size": 262144,
        },
    }
    tokenizer = {
        "identity_sha256": _TOKENIZER_IDENTITY_SHA256,
        "model_id": "google/gemma-3-270m-it",
        "revision": _BASE_REVISION,
        "vocab_size": 262144,
    }
    application = {
        "schema_version": 1,
        "policy_id": "gemma_lexical_v1",
        "policy_sha256": _POLICY_SELF_SHA256,
        "train": {
            "loaded_record_count": 4064,
            "accepted_record_count": 4064,
            "rejected_record_count": 0,
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SELF_SHA256,
            "source_target_marker_parity": True,
        },
        "validation": {
            "loaded_record_count": 431,
            "accepted_record_count": 431,
            "rejected_record_count": 0,
            "policy_id": "gemma_lexical_v1",
            "policy_sha256": _POLICY_SELF_SHA256,
            "source_target_marker_parity": True,
        },
    }
    order = {
        "schema_version": 1,
        "kind": "scriber_remediation_order_plan",
        "mode": "shuffled",
        "seed": 270014,
        "record_count": 4064,
        "safe_record_count": 4064,
        "used_record_count": 4064,
        "source_sha256": _TRAIN_SHA256,
        "plan_path": str(Path("job-input") / "order-plan.json"),
        "plan_sha256": _PLAN_SHA256,
        "implicit_trainer_shuffling": False,
        "full_epoch_required": True,
        "selected_policy_rejected_record_count": 0,
        "sequence_rejected_record_count": 0,
        "effective_phase_counts": {
            "1": 813,
            "2": 813,
            "3": 813,
            "4": 813,
            "5": 812,
        },
        "effective_order_lock": {
            "algorithm": "frozen_source_effective_order_v1",
            "example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
        },
    }
    bindings = {
        "cli": {
            "config": str(Path("job-input") / "train.yaml"),
            "dataset_manifest": str(Path("job-input") / "dataset-manifest.json"),
            "entrypoint": "ml/scriber_polishing/scripts/train_sft.py",
            "gradient_accumulation_steps_override": None,
            "learning_rate_override": None,
            "max_sequence_length_override": None,
            "max_steps_override": None,
            "micro_batch_size_override": None,
            "output_dir": str(training_root),
            "save_final": True,
            "train": str(Path("job-input") / "selected-train.jsonl"),
            "train_limit": None,
            "training_order_plan": str(Path("job-input") / "order-plan.json"),
            "validation": str(Path("job-input") / "safe-validation.jsonl"),
            "validation_limit": None,
        },
        "code": {
            "code_tree_sha256": "sha256:" + "d" * 64,
            "dirty": False,
        },
        "config": {
            "bytes": 1024,
            "path": str(Path("job-input") / "train.yaml"),
            "sha256": input_binding["config_sha256"],
            "values": _config_values(),
        },
        "dataset": {
            "manifest": {
                "bytes": 512,
                "declared_train_records": 4064,
                "declared_train_sha256": "sha256:" + _TRAIN_SHA256,
                "declared_validation_records": 431,
                "declared_validation_sha256": "sha256:" + _VALIDATION_SHA256,
                "generative_api": False,
                "human_curation": False,
                "path": str(Path("job-input") / "dataset-manifest.json"),
                "sha256": input_binding["dataset_manifest_sha256"],
                "split_leakage": False,
                "synthetic_only": True,
            },
            "train": {
                "selected_records": 4064,
                "total_records": 4064,
                "sha256": "sha256:" + _TRAIN_SHA256,
            },
            "validation": {
                "selected_records": 431,
                "total_records": 431,
                "sha256": "sha256:" + _VALIDATION_SHA256,
            },
        },
        "effective_training": {
            "bf16": True,
            "completion_only_loss": True,
            "effective_batch_size": 16,
            "gradient_accumulation_steps": 8,
            "gradient_checkpointing": True,
            "learning_rate": 0.00001,
            "max_sequence_length": 768,
            "method": "full_sft",
            "micro_batch_size": 2,
            "optimizer": "adamw_torch_fused",
            "run_kind": "improvement",
            "save_total_limit": 4,
            "schedule": schedule,
            "sdpa": True,
            "seed": 270014,
            "train_examples_loaded": 4064,
            "train_examples_used": 4064,
            "training_order_mode": "shuffled",
            "validation_examples_loaded": 431,
            "validation_examples_used": 431,
        },
        "parent_checkpoint": parent,
        "protection_application": application,
        "protection_policy": policy,
        "runtime": {
            "cuda_runtime": "12.8",
            "gpu": "NVIDIA L4",
            "python": "3.12.3",
            "torch": "2.11.0+cu128",
            "transformers": "4.57.6",
        },
        "tokenizer": tokenizer,
        "training_order": order,
    }
    identity = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": bindings,
    }
    run_fingerprint = "sha256:" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    identity["run_fingerprint"] = run_fingerprint
    identity_payload = canonical_json_bytes(identity)
    final_directory = "final-" + run_fingerprint.removeprefix("sha256:")[:16]
    report: dict[str, object] = {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": _BASE_REVISION,
        "run_kind": "improvement",
        "run_fingerprint": run_fingerprint,
        "run_identity": {
            "path": str(training_root / "run-identity.json"),
            "sha256": "sha256:" + hashlib.sha256(identity_payload).hexdigest(),
        },
        "bindings": bindings,
        "training_completed": True,
        "max_steps": 254,
        "schedule": schedule,
        "checkpoint_state": {
            "epoch": 1.0,
            "global_step": 254,
            "last_checkpoint": "checkpoint-254",
        },
        "resume_from_checkpoint": None,
        "warm_start_parent_checkpoint": parent,
        "train_examples_loaded": 4064,
        "train_examples_used": 4064,
        "validation_examples_loaded": 431,
        "validation_examples_used": 431,
        "oversize_rejections": {"train": 0, "validation": 0},
        "protection_rejections": {"train": 0, "validation": 0},
        "protection_policy": policy,
        "protection_application": application,
        "seed": 270014,
        "method": "full_sft",
        "optimizer": "adamw_torch_fused",
        "learning_rate": 0.00001,
        "max_sequence_length": 768,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "effective_batch_size": 16,
        "bf16": True,
        "sdpa": True,
        "early_stopping": False,
        "save_total_limit": 4,
        "remediation_order": order,
        "final_model": {
            "directory": final_directory,
            "path": str(training_root / final_directory),
            "run_fingerprint": run_fingerprint,
            "inventory": {
                "tree_sha256": "sha256:" + "5" * 64,
                "file_count": 12,
                "total_bytes": 575466744,
            },
            "reload_verification": {
                "loader": "transformers_local",
                "model_type": "gemma3_text",
                "parameter_count": 268098176,
                "protection_policy_sha256": "sha256:" + _POLICY_ARTIFACT_SHA256,
                "run_fingerprint": run_fingerprint,
                "schema_version": 1,
                "status": "passed",
                "tokenizer_class": ("transformers.models.gemma.tokenization_gemma_fast.GemmaTokenizerFast"),
            },
        },
    }
    return report, identity


def test_training_result_proves_exactly_254_new_updates_without_resume(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "training-report.json"
    report, _ = _training_report()
    report_path.write_text(
        json.dumps(report),
        encoding="utf-8",
    )

    result = verify_continuation_training_report(
        report_path,
        expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
        expected_input_binding=_input_binding(),
    )

    assert result["additional_updates"] == 254
    assert result["cumulative_updates"] == 1100
    assert result["replayed_updates"] == 0
    assert str(result["final_model_directory"]).startswith("final-")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("checkpoint_state.global_step", 253, "global step must equal 254"),
        (
            "resume_from_checkpoint",
            {"global_step": 846},
            "must not resume or replay",
        ),
        ("train_examples_used", 4063, "must use exactly 4064"),
        ("bindings.dataset.train.sha256", "sha256:" + "0" * 64, "train SHA-256"),
        ("bindings.training_order.plan_sha256", "0" * 64, "order plan SHA-256"),
        (
            "bindings.training_order.effective_order_lock.example_ids_sha256",
            "0" * 64,
            "effective order SHA-256",
        ),
        ("bindings.config.values.seed", 270013, "seed"),
        (
            "bindings.protection_policy.policy_sha256",
            "sha256:" + "0" * 64,
            "policy self SHA-256",
        ),
        (
            "bindings.tokenizer.identity_sha256",
            "sha256:" + "0" * 64,
            "tokenizer identity",
        ),
        ("base_revision", "0" * 40, "base revision"),
        ("protection_rejections.validation", 1, "zero protection rejections"),
    ],
)
def test_training_result_rejects_partial_replayed_or_wrong_size_runs(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    report, _ = _training_report()
    target: dict[str, object] = report
    parts = field.split(".")
    for part in parts[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target[parts[-1]] = value
    report_path = tmp_path / "training-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(TrainingContinuationError, match=message):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )


def test_training_result_rejects_minimal_report_even_when_step_counts_match(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "training-report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_kind": "improvement",
                "training_completed": True,
                "max_steps": 254,
                "schedule": {"max_steps": 254, "update_steps_per_epoch": 254},
                "checkpoint_state": {
                    "global_step": 254,
                    "last_checkpoint": "checkpoint-254",
                },
                "resume_from_checkpoint": None,
                "warm_start_parent_checkpoint": {
                    "relationship": "warm_start_parent_not_resume",
                    "tree_sha256": _PARENT_DIRECTORY_TREE,
                },
                "train_examples_loaded": 4064,
                "train_examples_used": 4064,
                "effective_batch_size": 16,
                "remediation_order": {
                    "implicit_trainer_shuffling": False,
                    "full_epoch_required": True,
                    "used_record_count": 4064,
                },
                "final_model": {
                    "directory": "final-0123456789abcdef",
                    "inventory": {"tree_sha256": "sha256:" + "5" * 64},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TrainingContinuationError, match="base model"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )


@pytest.mark.parametrize(
    "directory",
    [
        "final-continuation",
        "final-0123456789abcde",
        "final-0123456789abcdef0",
        "../final-0123456789abcdef",
        "final-0123456789ABCDEf",
    ],
)
def test_training_result_rejects_non_exact_or_traversing_final_directory(
    tmp_path: Path,
    directory: str,
) -> None:
    report, _ = _training_report()
    final = report["final_model"]
    assert isinstance(final, dict)
    final["directory"] = directory
    report_path = tmp_path / "training-report.json"
    report_path.write_bytes(canonical_json_bytes(report))

    with pytest.raises(TrainingContinuationError, match="final model directory"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )


def test_training_result_rejects_parent_tree_republished_as_final(
    tmp_path: Path,
) -> None:
    report, _ = _training_report()
    final = report["final_model"]
    assert isinstance(final, dict)
    inventory = final["inventory"]
    assert isinstance(inventory, dict)
    inventory["tree_sha256"] = _PARENT_MODEL_TREE
    report_path = tmp_path / "training-report.json"
    report_path.write_bytes(canonical_json_bytes(report))

    with pytest.raises(TrainingContinuationError, match="differs from accepted parent"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )


def _row(
    example_id: str,
    *,
    phase: int,
    source: str,
    split: str = "train",
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "split": split,
        "_continuation_phase": phase,
        "_continuation_source": source,
    }


def test_continuation_mix_uses_every_eligible_nonpilot_ai_gold_train_case() -> None:
    pilot_ids = frozenset({"ai_0000", "final_p1_0000"})
    ai_gold = [_row(f"ai_{index:04d}", phase=5, source="ai_gold_train") for index in range(768)]
    final_by_phase = {
        phase: [
            _row(
                f"final_p{phase}_{index:04d}",
                phase=phase,
                source="final_data_train",
            )
            for index in range(900)
        ]
        for phase in range(1, 6)
    }

    selected, evidence = select_continuation_mix(
        eligible_ai_gold_records=ai_gold,
        eligible_final_records_by_phase=final_by_phase,
        pilot_example_ids=pilot_ids,
        seed=270014,
    )

    selected_ids = {str(item["example_id"]) for item in selected}
    assert len(selected) == 4064
    assert selected_ids.isdisjoint(pilot_ids)
    assert evidence["ai_gold_train_selected"] == 767
    assert evidence["final_data_train_selected"] == 3297
    assert evidence["phase_counts"] == {
        "1": 813,
        "2": 813,
        "3": 813,
        "4": 813,
        "5": 812,
    }
    assert all(item["split"] == "train" for item in selected)
    assert all(item["_continuation_source"] != "ai_gold_train" or item["_continuation_phase"] == 5 for item in selected)


def test_continuation_mix_rejects_ai_gold_holdout_rows() -> None:
    ai_gold = [
        _row(
            "ai_challenge_001",
            phase=5,
            source="ai_gold_train",
            split="challenge",
        )
    ]
    final_by_phase = {
        phase: [
            _row(
                f"final_p{phase}_{index:04d}",
                phase=phase,
                source="final_data_train",
            )
            for index in range(900)
        ]
        for phase in range(1, 6)
    }

    with pytest.raises(
        TrainingContinuationError,
        match="AI-Gold continuation input may contain only split=train",
    ):
        select_continuation_mix(
            eligible_ai_gold_records=ai_gold,
            eligible_final_records_by_phase=final_by_phase,
            pilot_example_ids=frozenset(),
            seed=270014,
        )


def test_cloud_runner_copies_and_revalidates_external_parent_without_resume() -> None:
    runner = continuation_runner_payload().decode("utf-8")

    assert "--expected-packet-tree-sha256" in runner
    assert "--expected-flavor" in runner
    assert "expected packet tree SHA-256 argument is required" in runner
    assert "REMOTE_PARENT_ROOT=/accepted-parent" in runner
    assert "LOCAL_PARENT_ROOT=" in runner
    assert "verify_hf_training_continuation.py" in runner
    assert runner.index("--mode packet") < runner.index('cp -a --no-preserve=ownership "$SOURCE_REPO" "$WORK_ROOT"')
    assert runner.index("--mode preflight-remote") < runner.index(
        'cp -a --no-preserve=ownership "$REMOTE_PARENT_ROOT" "$LOCAL_PARENT_ROOT"'
    )
    assert runner.index("--mode preflight-local") < runner.index("scripts/train_sft.py")
    assert "--resume none" in runner
    assert "--resume auto" not in runner
    assert "--quantization" not in runner
    assert "push_to_hub" not in runner
    assert "--mode claim-output" in runner
    assert "output-claim.json" in runner
    assert "rm -rf" not in runner


def test_training_code_identity_dependencies_are_copied_and_verified(
    tmp_path: Path,
) -> None:
    source_project = Path(__file__).resolve().parents[1]
    packet_project = tmp_path / "repo" / "ml" / "scriber_polishing"

    copied = _copy_training_code_identity_files(
        source_project,
        packet_project,
    )

    required_builder = packet_project / "scripts" / "build_remediation_experiment.py"
    assert required_builder in copied
    assert required_builder.is_file()
    _verify_training_code_identity_files(packet_project)

    required_builder.unlink()
    with pytest.raises(
        TrainingContinuationError,
        match="training code identity dependency",
    ):
        _verify_training_code_identity_files(packet_project)


def _make_packet(root: Path, *, flavor: str = "l4x1") -> str:
    job_input = root / "repo" / "ml" / "scriber_polishing" / "job-input"
    job_input.mkdir(parents=True)
    _copy_training_code_identity_files(
        Path(__file__).resolve().parents[1],
        root / "repo" / "ml" / "scriber_polishing",
    )
    budget = reserve_hf_budget(
        prior_cost_usd="0.893",
        flavor=flavor,
        timeout_minutes=30,
    )
    cost_payload = canonical_json_bytes(build_hf_cost_audit())
    (job_input / "hf-cost-audit.json").write_bytes(cost_payload)
    continuation_payload = canonical_json_bytes(
        {
            "schema_version": 1,
            "kind": "scriber_hf_training_continuation",
            "status": "ready",
            "budget": {
                **budget,
                "flavor": flavor,
                "cost_audit_sha256": ("sha256:" + hashlib.sha256(cost_payload).hexdigest()),
            },
        }
    )
    (job_input / "continuation-manifest.json").write_bytes(continuation_payload)
    (root / "run-training-continuation.sh").write_bytes(continuation_runner_payload())
    (root / "repo" / "source.txt").write_text("bound source\n", encoding="utf-8")
    inventory, packet_tree_sha256 = _packet_inventory(root)
    job_manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation_job_packet",
        "source_git_head": "1" * 40,
        "continuation_manifest_sha256": ("sha256:" + hashlib.sha256(continuation_payload).hexdigest()),
        "image": "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime",
        "flavor": flavor,
        "timeout_minutes": 30,
        "maximum_job_cost_usd": budget["maximum_job_cost_usd"],
        "maximum_aggregate_cost_usd": budget["maximum_total_cost_usd"],
        "total_budget_usd": 5.0,
        "private_output_required": True,
        "parent_model_embedded": False,
        "external_parent_mount_required": True,
        "quantization": False,
        "publication": False,
        "packet_tree_sha256": packet_tree_sha256,
        "inventory_excludes": ["job-manifest.json"],
        "file_count": len(inventory),
        "byte_count": sum(int(item["bytes"]) for item in inventory),
        "output_root": "/outputs/attempt8/scriber-1100-continuation-v1",
        "remote_result_uri": (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
        ),
    }
    (root / "job-manifest.json").write_bytes(canonical_json_bytes(job_manifest))
    return packet_tree_sha256


def test_hf_launch_contract_mounts_bucket_root_without_double_attempt8(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "continuation-packet"
    packet.mkdir()
    packet_tree_sha256 = _make_packet(packet)

    launch = build_hf_job_launch_contract(
        packet,
        expected_flavor="l4x1",
    )

    assert launch["output_volume"] == ("hf://buckets/Buttermilk03/scriber-polishing-private-runs:/outputs:rw")
    assert launch["output_root"] == ("/outputs/attempt8/scriber-1100-continuation-v1")
    assert launch["remote_result_uri"] == (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt8/scriber-1100-continuation-v1"
    )
    assert "attempt8/attempt8" not in json.dumps(launch)
    assert launch["argv"].count("-v") == 3
    assert "--timeout" in launch["argv"]
    assert launch["argv"][launch["argv"].index("--timeout") + 1] == "30m"
    assert launch["expected_packet_tree_sha256"] == packet_tree_sha256
    assert launch["argv"][-4:] == [
        "--expected-packet-tree-sha256",
        packet_tree_sha256,
        "--expected-flavor",
        "l4x1",
    ]


def test_hf_launch_contract_binds_a10g_in_packet_budget_and_runner(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "continuation-packet"
    packet.mkdir()
    packet_tree_sha256 = _make_packet(packet, flavor="a10g-small")

    launch = build_hf_job_launch_contract(
        packet,
        expected_flavor="a10g-small",
    )

    assert launch["flavor"] == "a10g-small"
    assert launch["argv"][launch["argv"].index("--flavor") + 1] == "a10g-small"
    assert launch["argv"][-4:] == [
        "--expected-packet-tree-sha256",
        packet_tree_sha256,
        "--expected-flavor",
        "a10g-small",
    ]


def test_packet_verification_requires_external_expected_tree_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    packet_tree_sha256 = _make_packet(packet)

    verified = verify_hf_training_continuation_packet(
        packet,
        expected_packet_tree_sha256=packet_tree_sha256,
        expected_flavor="l4x1",
    )
    assert verified["packet_tree_sha256"] == packet_tree_sha256

    with pytest.raises(TrainingContinuationError, match="expected packet tree"):
        verify_hf_training_continuation_packet(
            packet,
            expected_packet_tree_sha256="",
            expected_flavor="l4x1",
        )

    (packet / "repo" / "source.txt").write_text(
        "tampered source\n",
        encoding="utf-8",
    )
    with pytest.raises(TrainingContinuationError, match="packet tree"):
        verify_hf_training_continuation_packet(
            packet,
            expected_packet_tree_sha256=packet_tree_sha256,
            expected_flavor="l4x1",
        )


def test_packet_verification_rejects_flavor_substitution(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    packet_tree_sha256 = _make_packet(packet, flavor="a10g-small")

    with pytest.raises(TrainingContinuationError, match="flavor"):
        verify_hf_training_continuation_packet(
            packet,
            expected_packet_tree_sha256=packet_tree_sha256,
            expected_flavor="l4x1",
        )


def test_packet_verification_rejects_embedded_credentials(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "packet"
    packet.mkdir()
    _make_packet(packet)
    (packet / "repo" / "credential.txt").write_text(
        "access_token=hf_" + "A" * 32 + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TrainingContinuationError, match="credential"):
        _packet_inventory(packet)


def test_output_guard_is_atomic_and_forbids_second_cost_reservation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "attempt7" / "scriber-1100-continuation-v1"
    packet_tree = "sha256:" + "1" * 64
    manifest_sha = "sha256:" + "2" * 64
    first = claim_continuation_output_guard(
        output,
        expected_packet_tree_sha256=packet_tree,
        continuation_manifest_sha256=manifest_sha,
        owner_id="11111111-1111-4111-8111-111111111111",
    )
    assert first["status"] == "active"
    assert (output / "continuation-launch-guard.json").is_file()

    with pytest.raises(
        TrainingContinuationError,
        match="identity-bound partial output preserved.*second cost reservation",
    ):
        claim_continuation_output_guard(
            output,
            expected_packet_tree_sha256=packet_tree,
            continuation_manifest_sha256=manifest_sha,
            owner_id="22222222-2222-4222-8222-222222222222",
        )

    with pytest.raises(TrainingContinuationError, match="foreign or concurrent owner"):
        claim_continuation_output_guard(
            output,
            expected_packet_tree_sha256="sha256:" + "3" * 64,
            continuation_manifest_sha256=manifest_sha,
            owner_id="33333333-3333-4333-8333-333333333333",
        )


def test_packet_verification_rejects_source_symlinks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    link = source / "linked.txt"
    try:
        link.symlink_to(external)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    with pytest.raises(TrainingContinuationError, match="symlink"):
        _copy_project_tree(source, tmp_path / "copy")


def test_full_second_source_hash_catches_same_size_rewrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "final-train.jsonl"
    source.write_bytes(b'{"example_id":"aaa"}\n')
    binding = _stable_file_binding(source, label="final-data train")
    original_stat = source.stat()
    source.write_bytes(b'{"example_id":"bbb"}\n')
    os.utime(
        source,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(TrainingContinuationError, match="full second hash"):
        _verify_full_file_binding(
            source,
            binding,
            label="final-data train",
        )


def test_bundle_rejects_traversing_artifact_name_before_reading_outside_root(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    payload = outside.read_bytes()
    manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_training_continuation",
        "status": "ready",
        "artifacts": {
            "../outside.json": {
                "filename": "../outside.json",
                "bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
        },
    }
    (bundle / "continuation-manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(TrainingContinuationError, match="unsafe artifact"):
        verify_hf_training_continuation_bundle(
            bundle,
            parent_model=tmp_path / "parent",
            mode="preflight-remote",
        )


def _materialize_report_output(
    root: Path,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    report, identity = _training_report(output_root=root)
    training = root / "training"
    training.mkdir(parents=True)
    (training / "run-identity.json").write_bytes(canonical_json_bytes(identity))
    final = report["final_model"]
    assert isinstance(final, dict)
    final_root = training / str(final["directory"])
    final_root.mkdir()
    (final_root / "model.safetensors").write_bytes(b"bound fake model\n")
    report_path = root / "training-report.json"
    report_path.write_bytes(canonical_json_bytes(report))
    return report_path, report, identity


def test_postflight_requires_exact_output_paths_and_physical_run_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "output"
    report_path, report, _ = _materialize_report_output(root)
    final = report["final_model"]
    assert isinstance(final, dict)
    declared = final["inventory"]
    assert isinstance(declared, dict)
    monkeypatch.setattr(
        "scriber_polishing.inference._inventory_local_model_tree",
        lambda _: dict(declared),
    )

    result = build_continuation_postflight_result(
        training_report_path=report_path,
        output_root=root,
        input_binding=_input_binding(),
    )

    assert result["status"] == "complete"
    assert result["flavor"] == "l4x1"
    assert result["final_model_tree_sha256"] == "sha256:" + "5" * 64

    moved = root / "renamed-report.json"
    shutil.copy2(report_path, moved)
    with pytest.raises(TrainingContinuationError, match="training-report.json"):
        build_continuation_postflight_result(
            training_report_path=moved,
            output_root=root,
            input_binding=_input_binding(),
        )

    with pytest.raises(TrainingContinuationError, match="postflight flavor"):
        build_continuation_postflight_result(
            training_report_path=report_path,
            output_root=root,
            input_binding=_input_binding(),
            expected_flavor="a10g-small",
        )


def test_postflight_rejects_tampered_physical_run_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    report_path, _, identity = _materialize_report_output(root)
    identity["fingerprint_algorithm"] = "tampered"
    (root / "training" / "run-identity.json").write_bytes(canonical_json_bytes(identity))

    with pytest.raises(TrainingContinuationError, match="run identity"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
            output_root=root,
        )


@pytest.mark.parametrize("link_kind", ["training", "final"])
def test_postflight_rejects_symlinked_output_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    root = tmp_path / "output"
    report_path, report, _ = _materialize_report_output(root)
    final = report["final_model"]
    assert isinstance(final, dict)
    declared = final["inventory"]
    assert isinstance(declared, dict)
    monkeypatch.setattr(
        "scriber_polishing.inference._inventory_local_model_tree",
        lambda _: dict(declared),
    )
    training = root / "training"
    final_root = training / str(final["directory"])
    try:
        if link_kind == "training":
            real_training = root / "real-training"
            training.rename(real_training)
            training.symlink_to(real_training, target_is_directory=True)
        else:
            real_final = training / "real-final"
            final_root.rename(real_final)
            final_root.symlink_to(real_final, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    with pytest.raises(TrainingContinuationError, match="must not be a symlink"):
        build_continuation_postflight_result(
            training_report_path=report_path,
            output_root=root,
            input_binding=_input_binding(),
        )


def test_training_result_rejects_run_fingerprint_or_reload_inconsistency(
    tmp_path: Path,
) -> None:
    report, _ = _training_report()
    report["run_fingerprint"] = "sha256:" + "f" * 64
    report_path = tmp_path / "training-report.json"
    report_path.write_bytes(canonical_json_bytes(report))

    with pytest.raises(TrainingContinuationError, match="run fingerprint"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )

    report, _ = _training_report()
    final = report["final_model"]
    assert isinstance(final, dict)
    reload_verification = final["reload_verification"]
    assert isinstance(reload_verification, dict)
    reload_verification["status"] = "failed"
    report_path.write_bytes(canonical_json_bytes(report))
    with pytest.raises(TrainingContinuationError, match="reload verification"):
        verify_continuation_training_report(
            report_path,
            expected_parent_tree_sha256=_PARENT_DIRECTORY_TREE,
            expected_input_binding=_input_binding(),
        )


def test_accepted_repair_parent_resolves_to_846_cumulative_updates() -> None:
    project = Path(__file__).parents[1]
    evidence = verify_accepted_repair_parent(
        acceptance_report_path=project / "artifacts/remediation/pilot-marker-lexical-repair-acceptance-v1.json",
        repair_manifest_path=project / "artifacts/remediation/pilot-marker-lexical-repair-v1/repair-manifest.json",
        training_report_path=project / "artifacts/cloud/hf-marker-repair-attempt6-recovered/training-report.json",
        cloud_result_path=project / "artifacts/cloud/hf-marker-repair-attempt6-recovered/cloud-result.json",
        accepted_parent_dir=project
        / ("artifacts/cloud/hf-marker-repair-attempt6-recovered/training/final-3015c63021cf53d8"),
        pilot_selection_report_path=project
        / ("artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"),
    )

    assert evidence["pilot_selected_updates"] == 700
    assert evidence["repair_updates"] == 146
    assert evidence["cumulative_updates"] == 846
    assert evidence["accepted_parent_model_tree_sha256"] == (
        "sha256:4b4b758f2fa3e00af282845c42de2887bc714c36a5c8e58e7225a1d9a7d25e06"
    )
    assert evidence["accepted_parent_directory_tree_sha256"] == (
        "sha256:aa2e12b3366363ed6efe728fff0423d2f19c1147704a1ede1f9b1c6ba65f6c28"
    )


def test_remote_parent_preflight_does_not_hash_large_fuse_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Path(__file__).parents[1]

    def forbid_full_tree_hash(_: Path) -> str:
        raise AssertionError("remote metadata preflight hashed the full tree")

    monkeypatch.setattr(
        "scriber_polishing.hf_training_continuation.checkpoint_tree_sha256",
        forbid_full_tree_hash,
    )
    evidence = verify_accepted_repair_parent(
        acceptance_report_path=project / "artifacts/remediation/pilot-marker-lexical-repair-acceptance-v1.json",
        repair_manifest_path=project / "artifacts/remediation/pilot-marker-lexical-repair-v1/repair-manifest.json",
        training_report_path=project / "artifacts/cloud/hf-marker-repair-attempt6-recovered/training-report.json",
        cloud_result_path=project / "artifacts/cloud/hf-marker-repair-attempt6-recovered/cloud-result.json",
        accepted_parent_dir=project
        / ("artifacts/cloud/hf-marker-repair-attempt6-recovered/training/final-3015c63021cf53d8"),
        pilot_selection_report_path=project
        / ("artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"),
        parent_inventory_mode="remote_metadata",
    )

    verification = evidence["parent_verification"]
    assert isinstance(verification, dict)
    assert verification["large_remote_files_hashed"] is False
    assert verification["verification_mode"] == "inventory_bytes_and_path_sizes_only"

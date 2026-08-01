import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from scriber_polishing.evaluation_gate import (
    PRODUCT_STATUS_VALUES,
    evaluate_candidate_gate,
    load_evaluation_config,
)
from scriber_polishing.judge_bundle import JUDGE_SCORE_DIMENSIONS

_POLICY_BINDING = {
    "filename": "scriber-protection-policy.json",
    "artifact_sha256": ("sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"),
    "policy_id": "gemma_lexical_v1",
    "policy_sha256": ("sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"),
    "tokenizer_identity_sha256": ("sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"),
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _evaluation_report(config: dict[str, object], config_hash: str) -> dict[str, object]:
    slices: dict[str, dict[str, object]] = {
        "all": {
            "total": 200,
            "output_only_compliance_rate": 1.0,
            "safe_renderer_rate": 1.0,
            "renderer_round_trip_rate": 1.0,
            "punctuation_f1": 1.0,
            "filler_f1": 1.0,
            "repetition_cleanup_f1": 1.0,
            "hard_content_exact_rates": {
                "numbers": 1.0,
                "amounts": 1.0,
                "dates": 1.0,
                "times": 1.0,
                "units": 1.0,
                "legal_references": 1.0,
            },
            "critical_errors": {},
            "semantic_hard_check_counts": {
                "semantic_addition": 0,
                "semantic_omission": 0,
                "semantic_reorder": 0,
                "semantic_question_answer": 0,
                "semantic_summary": 0,
                "semantic_entity_changed": 0,
            },
        },
        "identity": {"total": 20, "identity_exact_match": 1.0},
        "split:test": {"total": 100, "sst_parse_rate": 1.0, "critical_errors": {}},
        "split:challenge": {
            "total": 100,
            "sst_parse_rate": 1.0,
            "protected_span_exact_rate": 1.0,
            "critical_errors": {},
        },
    }
    metrics = {"slice_reports": slices}
    gate = evaluate_candidate_gate(metrics, config, candidate="final")
    assert gate["passed"] is True
    return {
        "schema_version": 2,
        "candidate": "final",
        "evaluation_config_sha256": config_hash,
        "reference_sha256": "a" * 64,
        "prediction_sha256": "b" * 64,
        "exact_case_coverage": True,
        "metrics": metrics,
        "gate": gate,
    }


def _judge_report(split: str, config_hash: str, *, passed: bool = True) -> dict[str, object]:
    rubric = dict.fromkeys(JUDGE_SCORE_DIMENSIONS, 5.0)
    summary = {
        "candidate_label": "final",
        "total_cases": 100,
        "win_count": 100,
        "win_rate": 1.0,
        "acceptable_count": 100,
        "acceptable_rate": 1.0,
        "selected_acceptance_rate": 1.0,
        "deterministic_critical_case_count": 0,
        "judge_critical_case_count": 0,
        "rubric_observation_count": 100,
        "rubric_averages": rubric,
    }
    return {
        "status": "complete",
        "evaluation_split": split,
        "bundle_sha256": ("c" if split == "test" else "d") * 64,
        "evaluation_config_sha256": config_hash,
        "release_candidate": "final",
        "release_gate_passed": passed,
        "candidate_summaries": {"final": summary, "base_model": {}},
    }


def _variant_release_matrix_report() -> dict[str, object]:
    summary = {
        "artifact_tree_sha256": "sha256:" + "a" * 64,
        "artifact_manifest_sha256": "sha256:" + "b" * 64,
        "reload_report_sha256": "sha256:" + "c" * 64,
        "predictions_sha256": "sha256:" + "d" * 64,
        "evaluation_report_sha256": "sha256:" + "e" * 64,
        "delta_report_sha256": "sha256:" + "f" * 64,
        "benchmark_report_sha256": "sha256:" + "1" * 64,
        "prediction_count": 4000,
        "maximum_absolute_metric_drop": 0.0,
        "new_critical_errors": 0,
        "persistent_warm_runtime": True,
        "evidence": [
            "artifact",
            "reload",
            "predictions",
            "evaluation",
            "delta",
            "benchmark",
        ],
    }
    variants = ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"]
    return {
        "schema_version": 4,
        "status": "RELEASE_MATRIX_PASSED",
        "passed": True,
        "baseline_variant": "bf16",
        "training_fingerprint": "sha256:" + "c" * 64,
        "protection_policy": dict(_POLICY_BINDING),
        "tested_variants": variants,
        "variants": variants,
        "variant_summaries": {variant: dict(summary) for variant in variants},
        "ineligible_variants": {},
        "qualification_failures": {},
        "reasons": [],
    }


def _completed_training_report() -> dict[str, object]:
    run_fingerprint = "sha256:" + "f" * 64
    final_model_tree_sha256 = "sha256:" + "7" * 64
    return {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "run_kind": "final",
        "run_fingerprint": run_fingerprint,
        "bf16": True,
        "max_steps": 100,
        "early_stopping": True,
        "save_total_limit": 4,
        "checkpoint_state": {
            "global_step": 100,
            "best_metric": 0.01,
            "best_model_checkpoint": "checkpoint-100",
            "last_checkpoint": "checkpoint-100",
            "surviving_checkpoints": [
                {
                    "checkpoint": "checkpoint-100",
                    "global_step": 100,
                    "run_fingerprint": run_fingerprint,
                    "identity_sha256": "sha256:" + "1" * 64,
                    "trainer_state_sha256": "sha256:" + "2" * 64,
                }
            ],
        },
        "final_model": {
            "run_fingerprint": run_fingerprint,
            "inventory": {
                "tree_sha256": final_model_tree_sha256,
            },
        },
        "training_completed": True,
    }


def _private_hub_report(completed_training_report_sha256: str) -> dict[str, object]:
    counts = {
        "train": 1,
        "validation": 1,
        "test": 1,
        "challenge": 1,
    }
    categories = [
        "ai_gold_smoke",
        "protected_span",
        "business_post",
        "tax_law",
        "paragraphs",
        "headings",
        "lists",
        "units",
        "legal_citations",
        "address_pronouns",
        "identity",
    ]

    def split_inventory(prefix: str) -> dict[str, object]:
        return {
            split: {
                "path": f"{prefix}/{split}.jsonl",
                "record_count": 1,
                "size_bytes": 100,
                "sha256": digit * 64,
            }
            for split, digit in (
                ("train", "1"),
                ("validation", "2"),
                ("test", "3"),
                ("challenge", "4"),
            )
        }

    def artifact(kind: str, revision: str) -> dict[str, object]:
        repo_id = (
            "Buttermilk03/scriber-gemma3-270m-polishing-de-v1"
            if kind == "model"
            else "Buttermilk03/scriber-transcript-polishing-de-v1"
        )
        value = {
            "repo_id": repo_id,
            "repo_type": kind,
            "revision": revision,
            "manifest_sha256": "3" * 64,
            "payload_tree_sha256": "4" * 64,
            "upload_tree_sha256": "8" * 64,
            "uploaded_file_count": 1,
            "remote_files": [
                {
                    "path": "model.safetensors" if kind == "model" else "data/train.jsonl",
                    "size_bytes": 100,
                    "expected_sha256": "9" * 64,
                    "hub_hash_kind": "hub_lfs_sha256",
                    "hub_hash": "9" * 64,
                    "remote_size_verified": True,
                    "remote_identity_verified": True,
                    "source_sha256_verified": True,
                }
            ],
            "verification_mode": "fresh_exact_revision_download",
            "exact_revision_downloaded": True,
            "source_snapshot_verified": True,
            "remote_identity_verified": True,
            "reload": {
                "status": "passed",
                "loader": f"{kind}_fixture",
                "execution_isolation": "fresh_python_isolated",
                "fresh_cache": True,
                "network_disabled": True,
                "credentials_removed": True,
                "legal_files": {
                    "status": "verified",
                    "kind": (
                        "gemma_derived_model"
                        if kind == "model"
                        else "synthetic_dataset"
                    ),
                    "files": [
                        {"path": path, "sha256": digit * 64}
                        for path, digit in (
                            (
                                "NOTICE" if kind == "model" else "LICENSE",
                                "1",
                            ),
                            (
                                "GEMMA_LICENSE.md"
                                if kind == "model"
                                else "DATASET_SOURCES.md",
                                "2",
                            ),
                            (
                                "MODIFICATIONS.md"
                                if kind == "model"
                                else "DATA_LICENSES.json",
                                "3",
                            ),
                        )
                    ],
                },
                "checksums": {
                    "status": "verified",
                    "manifest_sha256": "e" * 64,
                    "payload_tree_sha256": "f" * 64,
                    "file_count": 20,
                },
            },
        }
        if kind == "model":
            value["reload"]["protection_policy"] = dict(_POLICY_BINDING)
            value["reload"]["variant_release"] = {
                "status": "verified",
                "matrix_status": "RELEASE_MATRIX_PASSED",
                "passed": True,
                "report_sha256": "sha256:" + "a" * 64,
                "training_fingerprint": "sha256:" + "f" * 64,
                "tested_variants": ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"],
                "variants": ["bf16", "int8", "int4_nf4", "Q8_0", "Q4_K_M"],
                "ineligible_variants": {},
                "qualification_failures": {},
                "reasons": [],
                "fresh_reload_report_sha256": {
                    variant: "sha256:" + digit * 64
                    for variant, digit in (
                        ("bf16", "1"),
                        ("int8", "2"),
                        ("int4_nf4", "3"),
                        ("Q8_0", "4"),
                        ("Q4_K_M", "5"),
                    )
                },
            }
            value["reload"]["runtime_reload"] = {
                "status": "verified",
                "source_equivalence": "artifact_bytes_equal_executed_package",
                "files": [
                    {"path": path, "sha256": digit * 64}
                    for path, digit in (
                        ("runtime/inference.py", "5"),
                        ("runtime/sst_parser.py", "6"),
                        ("runtime/renderers.py", "7"),
                        ("sst_v1_schema.json", "8"),
                    )
                ],
            }
            value["reload"]["regression"] = {
                "status": "passed",
                "file": "examples/regression_sample.jsonl",
                "cases_sha256": "sha256:" + "b" * 64,
                "case_count": 110,
                "minimum_case_count": 100,
                "category_counts": {category: 10 for category in categories},
                "source_partition_counts": {
                    "synthetic_non_holdout": 100,
                    "ai_gold_validation": 10,
                },
                "required_categories": categories,
                "ai_gold_smoke_count": 10,
                "ai_gold_smoke_minimum": 10,
                "private_data": False,
                "holdout_data": False,
                "exact_output_hash_matches": 110,
                "protected_span_checks": 110,
                "sst_round_trip_checks": 110,
                "renderer_checks": {
                    renderer: 110
                    for renderer in (
                        "sst",
                        "plain_text",
                        "markdown",
                        "html",
                        "html_clipboard",
                    )
                },
                "latency_ms": {
                    "sample_count": 110,
                    "p50": 1.0,
                    "p95": 2.0,
                    "p99": 3.0,
                    "maximum": 4.0,
                },
            }
        else:
            inventories = {
                "release": split_inventory("data"),
                "ai_gold": split_inventory("ai_gold"),
            }
            value["reload"]["split_counts"] = dict(counts)
            value["reload"]["split_inventory"] = inventories["release"]
            value["reload"]["ai_gold_split_counts"] = dict(counts)
            value["reload"]["ai_gold_split_inventory"] = inventories["ai_gold"]
            value["expected_split_counts"] = {
                "release": dict(counts),
                "ai_gold": dict(counts),
            }
            value["expected_split_inventory"] = inventories
        return value

    return {
        "schema_version": 6,
        "status": "verified_private",
        "namespace": "Buttermilk03",
        "visibility": "private",
        "preflight": {
            "status": "passed",
            "token_role": "write",
            "namespace_authorized": True,
            "identity_sha256": "a" * 64,
            "repository_access_confirmed": True,
        },
        "completed_training_report_sha256": f"sha256:{completed_training_report_sha256}",
        "training_run_fingerprint": "sha256:" + "f" * 64,
        "bf16_final_model_tree_sha256": "sha256:" + "7" * 64,
        "protection_policy": dict(_POLICY_BINDING),
        "selected_checkpoint_publication_sha256": "sha256:" + "c" * 64,
        "selected_candidate_id": "checkpoint-100",
        "selected_checkpoint_identity_sha256": "sha256:" + "d" * 64,
        "selected_checkpoint_tree_sha256": "sha256:" + "e" * 64,
        "selected_model_sha256": "sha256:" + "f" * 64,
        "selected_config_sha256": "sha256:" + "0" * 64,
        "base_tokenizer": {
            "id": "google/gemma-3-270m-it",
            "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        },
        "local_bundle_sha256": "b" * 64,
        "publication_binding_sha256": "c" * 64,
        "artifacts": {
            "model": artifact("model", "5" * 40),
            "dataset": artifact("dataset", "6" * 40),
        },
    }


def _paired_significance_report(config_hash: str, final_evaluation_hash: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_paired_evaluation_significance",
        "status": "complete",
        "decision": "SIGNIFICANT_IMPROVEMENT",
        "inputs": {
            "base_report_sha256": "d" * 64,
            "fine_tuned_report_sha256": final_evaluation_hash,
            "reference_sha256": "a" * 64,
            "evaluation_config_sha256": config_hash,
            "case_count": 1,
            "case_id_sha256": "e" * 64,
        },
        "candidates": {"base": "base_model", "fine_tuned": "final"},
        "metric": {"name": "normalized_exact_match", "direction": "higher_is_better"},
        "paired_delta": {"base_mean": 0.0, "fine_tuned_mean": 1.0, "mean": 1.0, "minimum": 0.0},
        "bootstrap": {
            "method": "paired_case_mean_with_replacement_nearest_rank_v1",
            "seed": 270020,
            "resamples": 10000,
            "confidence_level": 0.95,
            "lower": 1.0,
            "median": 1.0,
            "upper": 1.0,
            "passed": True,
        },
    }


def test_externally_visible_product_status_vocabulary_is_exact() -> None:
    expected = {
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
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "release_verification_report_schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert expected == PRODUCT_STATUS_VALUES
    assert expected == set(schema["properties"]["status"]["enum"])


def test_verify_release_recomputes_all_gates_and_writes_failure_before_nonzero_exit(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    config_path = project_root / "configs" / "evaluation.yaml"
    config = load_evaluation_config(config_path)
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    evaluation_path = tmp_path / "evaluation.json"
    judge_test_path = tmp_path / "judge-test.json"
    judge_challenge_path = tmp_path / "judge-challenge.json"
    variant_matrix_path = tmp_path / "variant-release-matrix.json"
    paired_significance_path = tmp_path / "paired-significance.json"
    completed_training_path = tmp_path / "completed-training.json"
    private_hub_path = tmp_path / "private-hub.json"
    output_path = tmp_path / "release.json"
    _write_json(evaluation_path, _evaluation_report(config, config_hash))
    _write_json(
        paired_significance_path,
        _paired_significance_report(
            config_hash,
            hashlib.sha256(evaluation_path.read_bytes()).hexdigest(),
        ),
    )
    _write_json(judge_test_path, _judge_report("test", config_hash))
    _write_json(variant_matrix_path, _variant_release_matrix_report())
    _write_json(completed_training_path, _completed_training_report())
    _write_json(
        private_hub_path,
        _private_hub_report(hashlib.sha256(completed_training_path.read_bytes()).hexdigest()),
    )
    failed_challenge = _judge_report("challenge", config_hash, passed=False)
    failed_challenge["candidate_summaries"]["final"]["rubric_averages"]["grammar"] = 4.0
    _write_json(judge_challenge_path, failed_challenge)

    command = [
        sys.executable,
        str(project_root / "scripts" / "verify_release.py"),
        "--config",
        str(config_path),
        "--candidate",
        "final",
        "--evaluation-report",
        str(evaluation_path),
        "--judge-test-report",
        str(judge_test_path),
        "--judge-challenge-report",
        str(judge_challenge_path),
        "--variant-release-matrix-report",
        str(variant_matrix_path),
        "--paired-significance-report",
        str(paired_significance_path),
        "--completed-training-report",
        str(completed_training_path),
        "--private-hub-report",
        str(private_hub_path),
        "--output",
        str(output_path),
    ]
    failed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert failed.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "PRIVATE_CANDIDATE"
    assert report["passed"] is False
    assert report["failed_components"] == ["judge:challenge"]
    assert report["evaluation_slice_reports"]["split:test"]["total"] == 100
    assert report["evaluation_slice_reports"]["split:challenge"]["total"] == 100

    passing_challenge = deepcopy(failed_challenge)
    passing_challenge["release_gate_passed"] = True
    passing_challenge["candidate_summaries"]["final"]["rubric_averages"]["grammar"] = 5.0
    _write_json(judge_challenge_path, passing_challenge)
    passed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert passed.returncode == 0, passed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "RELEASED"
    assert report["passed"] is True
    assert report["failed_components"] == []

    missing_pair_command = list(command)
    paired_flag_index = missing_pair_command.index("--paired-significance-report")
    del missing_pair_command[paired_flag_index : paired_flag_index + 2]
    missing_pair = subprocess.run(missing_pair_command, check=False, capture_output=True, text=True)

    assert missing_pair.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["failed_components"] == ["paired_significance"]
    assert report["components"]["paired_significance"]["reasons"] == ["missing_measured_evidence"]

    _write_json(judge_challenge_path, failed_challenge)
    missing_hub_command = list(command)
    private_hub_flag_index = missing_hub_command.index("--private-hub-report")
    del missing_hub_command[private_hub_flag_index : private_hub_flag_index + 2]
    missing_hub = subprocess.run(
        missing_hub_command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert missing_hub.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY_FOR_AI_JUDGING"
    assert report["status_basis"]["completed_final_training_verified"] is True
    assert report["status_basis"]["private_hub_upload_and_reload_verified"] is False
    assert "private_hub_publication" in report["failed_components"]

    invalid_hub_report = _private_hub_report(hashlib.sha256(completed_training_path.read_bytes()).hexdigest())
    invalid_hub_report["artifacts"]["model"]["remote_identity_verified"] = False
    _write_json(private_hub_path, invalid_hub_report)
    invalid_hub = subprocess.run(command, check=False, capture_output=True, text=True)

    assert invalid_hub.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY_FOR_AI_JUDGING"
    assert report["components"]["private_hub_publication"]["status"] == "invalid"
    assert report["status_basis"]["private_hub_upload_and_reload_verified"] is False

    _write_json(private_hub_path, _private_hub_report("0" * 64))
    mismatched_hub_binding = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert mismatched_hub_binding.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY_FOR_AI_JUDGING"
    assert "completed_training_report_hash_mismatch" in report["components"]["private_hub_publication"]["reasons"]

    _write_json(
        private_hub_path,
        _private_hub_report(hashlib.sha256(completed_training_path.read_bytes()).hexdigest()),
    )
    missing_training_command = list(command)
    training_flag_index = missing_training_command.index("--completed-training-report")
    del missing_training_command[training_flag_index : training_flag_index + 2]
    missing_training = subprocess.run(
        missing_training_command,
        check=False,
        capture_output=True,
        text=True,
    )

    assert missing_training.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY_FOR_TRAINING"
    assert report["status_basis"]["completed_final_training_verified"] is False

    pending_challenge = _judge_report("challenge", config_hash, passed=False)
    pending_challenge["status"] = "pending_tiebreak"
    pending_challenge["candidate_summaries"]["final"]["rubric_averages"] = dict.fromkeys(JUDGE_SCORE_DIMENSIONS)
    _write_json(judge_challenge_path, pending_challenge)
    pending = subprocess.run(command, check=False, capture_output=True, text=True)

    assert pending.returncode == 3
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY_FOR_AI_JUDGING"
    assert report["passed"] is False
    assert report["components"]["judge:challenge"]["status"] == "pending_tiebreak"

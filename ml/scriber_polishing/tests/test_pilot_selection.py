from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from scriber_polishing.pilot_selection import (
    PilotCandidateInput,
    PilotSelectionError,
    canonical_json_bytes,
    select_pilot_learning_rate,
    validate_pilot_selection_report,
    write_pilot_selection_report,
)


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> str:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _sha(payload)


def _tree_hash(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode())
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode())
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _make_model(root: Path, *, fingerprint: str) -> dict[str, object]:
    payloads = {
        "config.json": b'{"dtype":"bfloat16","model_type":"gemma3_text"}\n',
        "model.safetensors": b"fixture-bf16-model",
        "tokenizer.json": b'{"fixture":true}\n',
    }
    files: list[dict[str, object]] = []
    root.mkdir(parents=True)
    for name, payload in sorted(payloads.items()):
        (root / name).write_bytes(payload)
        files.append(
            {
                "path": name,
                "bytes": len(payload),
                "sha256": _sha(payload),
            }
        )
    inventory = {
        "schema_version": 1,
        "variant": "bf16",
        "format": "transformers_safetensors",
        "source_id": f"training-run:{fingerprint}",
        "model_type": "gemma3_text",
        "declared_dtype": "bfloat16",
        "weight_dtype_counts": {"BF16": 1},
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": _tree_hash(files),
        "files": files,
    }
    _write_json(root / "checkpoint-inventory.json", inventory)
    return inventory


def _training_report(
    model_root: Path,
    *,
    learning_rate: float,
    best_loss: float,
    fingerprint_character: str,
) -> dict[str, object]:
    fingerprint = _hash(fingerprint_character)
    inventory = _make_model(model_root, fingerprint=fingerprint)
    shared_config_values = {
        "schema_version": 1,
        "epochs": 2,
        "eval_steps": 50,
        "save_steps": 50,
        "early_stopping": True,
        "early_stopping_patience": 3,
        "assistant_output_loss_only": True,
        "max_grad_norm": 1.0,
        "warmup_ratio": 0.03,
        "weight_decay": 0.01,
        "learning_rate_candidates": [0.00002, 0.00005],
        "max_sequence_length_candidates": [512, 768, 1024],
    }
    dataset_manifest_hash = _hash("1")
    train_hash = _hash("2")
    validation_hash = _hash("3")
    tokenizer = {
        "model_id": "google/gemma-3-270m-it",
        "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "identity_sha256": _hash("4"),
        "vocab_size": 262144,
    }
    code = {
        "code_tree_sha256": _hash("5"),
        "git_head": "7ecd7e1ed349d1bcadbbb8151b584b766e9746d5",
        "dirty": True,
        "worktree_diff_sha256": _hash("6"),
        "worktree_status_sha256": _hash("7"),
        "files": [{"path": "train_sft.py", "sha256": _hash("8"), "bytes": 1}],
    }
    survivors = [
        {
            "checkpoint": "checkpoint-700",
            "global_step": 700,
            "run_fingerprint": fingerprint,
        },
        {
            "checkpoint": "checkpoint-850",
            "global_step": 850,
            "run_fingerprint": fingerprint,
        },
    ]
    return {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "bf16": True,
        "bindings": {
            "cli": {"learning_rate_override": learning_rate},
            "code": code,
            "config": {
                "sha256": _hash("9"),
                "values": shared_config_values,
            },
            "dataset": {
                "manifest": {
                    "sha256": dataset_manifest_hash,
                    "declared_train_sha256": train_hash,
                    "declared_validation_sha256": validation_hash,
                    "synthetic_only": True,
                    "generative_api": False,
                    "human_curation": False,
                    "split_leakage": False,
                },
                "train": {
                    "sha256": train_hash,
                    "total_records": 7000,
                    "selected_records": 7000,
                },
                "validation": {
                    "sha256": validation_hash,
                    "total_records": 700,
                    "selected_records": 700,
                },
            },
            "effective_training": {
                "learning_rate": learning_rate,
                "max_sequence_length": 768,
                "gradient_checkpointing": True,
                "completion_only_loss": True,
                "schedule": {
                    "declared_epochs": 2.0,
                    "effective_examples_per_step": 16,
                    "max_steps": 865,
                    "schedule_source": "epochs",
                },
            },
            "runtime": {
                "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU",
                "cuda_runtime": "12.8",
                "python": "3.12.13",
                "torch": "2.11.0+cu128",
                "transformers": "4.57.6",
            },
            "tokenizer": tokenizer,
        },
        "checkpoint_state": {
            "best_metric": best_loss,
            "best_model_checkpoint": "checkpoint-700",
            "global_step": 850,
            "last_checkpoint": "checkpoint-850",
            "surviving_checkpoints": survivors,
        },
        "cuda_runtime": "12.8",
        "early_stopping": True,
        "effective_batch_size": 16,
        "final_model": {
            "directory": model_root.name,
            "path": str(model_root.resolve()),
            "inventory": inventory,
            "reload_verification": {
                "schema_version": 1,
                "status": "passed",
                "run_fingerprint": fingerprint,
            },
            "run_fingerprint": fingerprint,
        },
        "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU",
        "gradient_accumulation_steps": 8,
        "learning_rate": learning_rate,
        "max_sequence_length": 768,
        "max_steps": 865,
        "method": "full_sft",
        "micro_batch_size": 2,
        "optimizer": "adamw_torch_fused",
        "oversize_rejections": {"train": 83, "validation": 3},
        "run_fingerprint": fingerprint,
        "run_kind": "pilot",
        "schedule": {
            "declared_epochs": 2.0,
            "effective_examples_per_step": 16,
            "max_steps": 865,
            "schedule_source": "epochs",
        },
        "sdpa": True,
        "seed": 270011,
        "torch_version": "2.11.0+cu128",
        "train_examples_loaded": 7000,
        "train_examples_used": 6917,
        "training_completed": True,
        "validation_examples_loaded": 700,
        "validation_examples_used": 697,
    }


def _metrics(score: float) -> dict[str, object]:
    direct = {
        "protected_span_exact_rate": score,
        "output_only_compliance_rate": score,
        "safe_renderer_rate": score,
        "renderer_round_trip_rate": score,
        "normalized_exact_match": score,
        "chrfpp_f2": score,
        "punctuation_f1": score,
        "filler_f1": score,
        "repetition_cleanup_f1": score,
        "capitalization_accuracy": score,
        "spelling_recovery_accuracy": score,
        "grammar_recovery_accuracy": score,
        "self_correction_recovery_accuracy": score,
        "sentence_boundary_f1": score,
        "sst_parse_rate": score,
        "ast_exact_match_rate": score,
        "block_sequence_exact_rate": score,
        "block_type_accuracy": score,
        "format_command_recovery_accuracy": score,
    }
    return {
        "total": 20,
        **direct,
        "cer": 1.0 - score,
        "wer": 1.0 - score,
        "hard_content_exact_rates": {"numbers": score, "units": score},
        "critical_errors": {"changed_number": 0},
        "semantic_hard_check_counts": {"semantic_omission": 0},
    }


def _hard_gate(*, passed: bool) -> dict[str, object]:
    return {
        "id": "critical_numbers",
        "slice": "all",
        "metric": "critical_errors.changed_number",
        "operator": "lte",
        "threshold": 0,
        "minimum_cases": 1,
        "observed_cases": 20,
        "actual": 0 if passed else 1,
        "passed": passed,
        "reason": None if passed else "threshold_not_met",
    }


def _make_shared_inputs(root: Path) -> tuple[Path, Path, dict[str, object]]:
    reference_manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_reference_bundle",
        "output_filename": "combined.references.jsonl",
        "records_sha256": _hash("a"),
        "case_ids_sha256": _hash("b"),
        "record_count": 20,
        "split_counts": {"test": 10, "challenge": 10},
        "inputs": [
            {
                "position": 0,
                "records_filename": "test.references.jsonl",
                "records_sha256": _hash("c"),
                "manifest_filename": "test.references.manifest.json",
                "manifest_sha256": _hash("d"),
                "case_ids_sha256": _hash("e"),
                "record_count": 10,
            },
            {
                "position": 1,
                "records_filename": "challenge.references.jsonl",
                "records_sha256": _hash("f"),
                "manifest_filename": "challenge.references.manifest.json",
                "manifest_sha256": _hash("0"),
                "case_ids_sha256": _hash("1"),
                "record_count": 10,
            },
        ],
    }
    reference_path = root / "combined.references.manifest.json"
    reference_manifest_sha = _write_json(reference_path, reference_manifest)
    config_path = root / "evaluation.yaml"
    config_path.write_text(
        "\n".join(
            [
                "schema_version: 2",
                "automatic_evaluation:",
                "  hard_gates:",
                "    - id: critical_numbers",
                "      slice: all",
                "      metric: critical_errors.changed_number",
                "      operator: lte",
                "      threshold: 0",
                "      minimum_cases: 1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return (
        reference_path,
        config_path,
        {
            "manifest_sha256": reference_manifest_sha,
            "records_sha256": reference_manifest["records_sha256"],
            "case_ids_sha256": reference_manifest["case_ids_sha256"],
            "record_count": reference_manifest["record_count"],
            "split_inputs": reference_manifest["inputs"],
        },
    )


def _make_candidate(
    root: Path,
    *,
    candidate_id: str,
    learning_rate: float,
    best_loss: float,
    score: float,
    hard_passed: bool,
    reference_path: Path,
    config_path: Path,
    fingerprint_character: str,
) -> tuple[PilotCandidateInput, dict[str, object]]:
    candidate_root = root / candidate_id
    model_root = candidate_root / "model"
    training = _training_report(
        model_root,
        learning_rate=learning_rate,
        best_loss=best_loss,
        fingerprint_character=fingerprint_character,
    )
    training_path = candidate_root / "training.json"
    _write_json(training_path, training)
    part_paths: list[Path] = []
    part_rows: list[dict[str, object]] = []
    reference_hashes = (_hash("c"), _hash("f"))
    for position, (name, reference_hash) in enumerate(zip(("test", "challenge"), reference_hashes, strict=True)):
        prediction_hash = _hash(chr(ord("2") + position))
        part = {
            "schema_version": 1,
            "candidate": "pilot",
            "model": str(model_root.resolve()),
            "prediction_sha256": prediction_hash.removeprefix("sha256:"),
            "record_count": 10,
            "reference_sha256": reference_hash.removeprefix("sha256:"),
            "device": "cuda",
            "quantization": "none",
            "revision": None,
        }
        part_path = candidate_root / f"{name}.manifest.json"
        part_manifest_hash = _write_json(part_path, part)
        part_paths.append(part_path)
        part_rows.append(
            {
                "position": position,
                "manifest_filename": part_path.name,
                "manifest_sha256": part_manifest_hash,
                "records_filename": f"{name}.predictions.jsonl",
                "records_sha256": prediction_hash,
                "record_count": 10,
                "case_ids_sha256": _hash(chr(ord("4") + position)),
            }
        )
    combined_prediction_hash = _hash("6" if candidate_id.endswith("a") else "7")
    reference_manifest_sha = _sha(reference_path.read_bytes())
    prediction_manifest = {
        "schema_version": 1,
        "kind": "scriber_evaluation_prediction_bundle",
        "candidate": "pilot",
        "output_filename": "combined.predictions.jsonl",
        "records_sha256": combined_prediction_hash,
        "prediction_sha256": combined_prediction_hash,
        "case_ids_sha256": _hash("b"),
        "record_count": 20,
        "exact_case_coverage": True,
        "inputs": part_rows,
        "reference": {
            "manifest_filename": reference_path.name,
            "manifest_sha256": reference_manifest_sha,
            "records_filename": "combined.references.jsonl",
            "records_sha256": _hash("a"),
            "case_ids_sha256": _hash("b"),
            "record_count": 20,
        },
    }
    prediction_manifest_path = candidate_root / "combined.predictions.manifest.json"
    _write_json(prediction_manifest_path, prediction_manifest)
    config_sha = _sha(config_path.read_bytes())
    evaluation = {
        "schema_version": 1,
        "candidate": "pilot",
        "evaluation_config_sha256": config_sha,
        "reference_sha256": _hash("a"),
        "prediction_sha256": combined_prediction_hash,
        "exact_case_coverage": True,
        "metrics": _metrics(score),
        "gate": {
            "schema_version": 1,
            "hard_gates": {"critical_numbers": _hard_gate(passed=hard_passed)},
        },
    }
    evaluation_path = candidate_root / "combined.evaluation.json"
    _write_json(evaluation_path, evaluation)
    return (
        PilotCandidateInput(
            candidate_id=candidate_id,
            training_report=training_path,
            model_tree=model_root,
            prediction_manifest=prediction_manifest_path,
            prediction_part_manifests=tuple(part_paths),
            evaluation_report=evaluation_path,
        ),
        {
            "training": training,
            "training_path": training_path,
            "model_root": model_root,
            "part_paths": part_paths,
            "part_rows": part_rows,
            "evaluation": evaluation,
            "evaluation_path": evaluation_path,
        },
    )


def _selection_fixture(
    tmp_path: Path,
    *,
    first_passed: bool,
    second_passed: bool,
    first_score: float = 0.99,
    second_score: float = 0.80,
    first_loss: float = 0.1,
    second_loss: float = 0.2,
) -> tuple[list[PilotCandidateInput], Path, Path, list[dict[str, object]]]:
    reference_path, config_path, _reference = _make_shared_inputs(tmp_path)
    first, first_data = _make_candidate(
        tmp_path,
        candidate_id="lr_a",
        learning_rate=0.00002,
        best_loss=first_loss,
        score=first_score,
        hard_passed=first_passed,
        reference_path=reference_path,
        config_path=config_path,
        fingerprint_character="a",
    )
    second, second_data = _make_candidate(
        tmp_path,
        candidate_id="lr_b",
        learning_rate=0.00005,
        best_loss=second_loss,
        score=second_score,
        hard_passed=second_passed,
        reference_path=reference_path,
        config_path=config_path,
        fingerprint_character="b",
    )
    return [first, second], reference_path, config_path, [first_data, second_data]


def _distribution(count: int = 1) -> dict[str, int | float]:
    return {
        "count": count,
        "minimum": 1,
        "p50": 1,
        "p95": 1,
        "maximum": 1,
        "mean": 1.0,
    }


def _maskability() -> dict[str, int]:
    return {
        "record_count": 10,
        "automatic_span_count": 2,
        "automatic_span_records": 2,
        "no_automatic_span_records": 8,
        "uniquely_maskable_records": 2,
        "ambiguous_records": 0,
        "ambiguous_repeated_value_records": 0,
        "ambiguous_extra_target_occurrence_records": 0,
        "unmaskable_records": 0,
        "exact_count_preserving_records": 2,
    }


def _attach_diagnostic(
    candidate: PilotCandidateInput,
    data: dict[str, object],
) -> PilotCandidateInput:
    part_path = data["part_paths"][0]
    part = json.loads(part_path.read_text(encoding="utf-8"))
    split_evaluation = deepcopy(data["evaluation"])
    split_evaluation["reference_sha256"] = part["reference_sha256"]
    split_evaluation["prediction_sha256"] = part["prediction_sha256"]
    split_evaluation["metrics"]["total"] = 10
    split_evaluation["gate"]["hard_gates"]["critical_numbers"]["observed_cases"] = 10
    split_eval_path = candidate.training_report.parent / "test.evaluation.json"
    split_eval_hash = _write_json(split_eval_path, split_evaluation)
    training = data["training"]
    diagnostic = {
        "schema_version": 1,
        "kind": "scriber_protected_span_diagnostic",
        "candidate": "pilot",
        "inputs": {
            "references": {
                "filename": "test.references.jsonl",
                "sha256": part["reference_sha256"],
                "record_count": 10,
            },
            "predictions": {
                "filename": "test.predictions.jsonl",
                "sha256": part["prediction_sha256"],
                "record_count": 10,
            },
            "prediction_manifest": {
                "filename": part_path.name,
                "sha256": _sha(part_path.read_bytes()).removeprefix("sha256:"),
            },
            "evaluation_report": {
                "filename": split_eval_path.name,
                "sha256": split_eval_hash.removeprefix("sha256:"),
            },
            "training_data": {
                "filename": "train.jsonl",
                "sha256": training["bindings"]["dataset"]["train"]["sha256"].removeprefix("sha256:"),
                "record_count": 7000,
            },
            "validation_data": {
                "filename": "validation.jsonl",
                "sha256": training["bindings"]["dataset"]["validation"]["sha256"].removeprefix("sha256:"),
                "record_count": 700,
            },
        },
        "counts": {
            "evaluation_cases": 10,
            "cases_with_automatic_spans": 2,
            "protected_metric_failures": 1,
            "inferred_restoration_failures": 1,
            "non_restoration_protected_failures": 0,
            "manifest_restoration_errors": 1,
            "manifest_reconciled": True,
        },
        "failure_modes": {
            "case_counts": {"deleted": 1},
            "span_counts": {"deleted": 1},
        },
        "lengths": {
            "protected_prompt_tokens_all_automatic_cases": _distribution(2),
            "protected_prompt_tokens_restoration_failures": _distribution(),
            "raw_prompt_tokens_restoration_failures": _distribution(),
            "placeholder_token_delta_restoration_failures": _distribution(),
        },
        "tokenization": {
            "canonical_marker": "⟦SCRIBER_PROTECTED_0000⟧",
            "canonical_marker_token_count": 2,
            "canonical_marker_token_ids": [1, 2],
            "canonical_marker_tokens": ["marker", "0"],
            "unused_token_probe": {
                "probe": "<unused0>",
                "token_ids": [3],
                "token_count": 1,
                "decoded_skip_special_tokens": "<unused0>",
                "is_special_token": False,
                "numeric_unused_token_count": 0,
                "contiguous_numeric_unused_from_zero": 0,
                "literal_probe_collision_records": 0,
            },
        },
        "training_exposure": {
            "record_count": 7000,
            "records_with_automatic_spans": 2,
            "automatic_span_count": 2,
            "literal_marker_record_count": 0,
            "paired_placeholder_eligible_record_count": 2,
            "maximum_automatic_spans_per_record": 1,
        },
        "maskability_by_split": {
            "train": _maskability(),
            "validation": _maskability(),
        },
        "by_family": [],
        "by_span_category": [],
        "cases": [],
        "limitations": ["Fixture limitation."],
    }
    diagnostic_path = candidate.training_report.parent / "protected-diagnostic.json"
    _write_json(diagnostic_path, diagnostic)
    return PilotCandidateInput(
        candidate_id=candidate.candidate_id,
        training_report=candidate.training_report,
        model_tree=candidate.model_tree,
        prediction_manifest=candidate.prediction_manifest,
        prediction_part_manifests=candidate.prediction_part_manifests,
        evaluation_report=candidate.evaluation_report,
        protected_span_diagnostic=diagnostic_path,
        diagnostic_evaluation_report=split_eval_path,
    )


def test_hard_gate_pass_outranks_higher_composite(tmp_path: Path) -> None:
    candidates, references, config, _data = _selection_fixture(
        tmp_path,
        first_passed=False,
        second_passed=True,
        first_score=0.99,
        second_score=0.70,
        first_loss=0.05,
        second_loss=0.20,
    )
    report = select_pilot_learning_rate(
        candidates,
        reference_manifest_path=references,
        evaluation_config_path=config,
    )
    assert report["status"] == "release_winner_selected"
    assert report["release_winner"]["candidate_id"] == "lr_b"
    assert report["release_safety_assertion"] is True
    assert report["remediation_parent"] is None
    assert report["fairness"]["candidate_outcomes"][0]["actual_global_step"] == 850
    assert report["fairness"]["candidate_outcomes"][0]["best_checkpoint"] == "checkpoint-700"


def test_both_hard_fail_never_creates_release_winner(tmp_path: Path) -> None:
    candidates, references, config, _data = _selection_fixture(
        tmp_path,
        first_passed=False,
        second_passed=False,
    )
    report = select_pilot_learning_rate(
        candidates,
        reference_manifest_path=references,
        evaluation_config_path=config,
    )
    assert report["status"] == "no_release_winner"
    assert report["release_winner"] is None
    assert report["release_safety_assertion"] is False
    assert report["remediation_parent"] is None
    assert report["remediation_evidence"]["status"] == "not_provided"


def test_shared_hash_bound_pipeline_failure_allows_only_remediation_parent(
    tmp_path: Path,
) -> None:
    candidates, references, config, data = _selection_fixture(
        tmp_path,
        first_passed=False,
        second_passed=False,
        first_score=0.95,
        second_score=0.80,
    )
    diagnosed = [
        _attach_diagnostic(candidate, candidate_data)
        for candidate, candidate_data in zip(candidates, data, strict=True)
    ]
    report = select_pilot_learning_rate(
        diagnosed,
        reference_manifest_path=references,
        evaluation_config_path=config,
    )
    assert report["release_winner"] is None
    assert report["release_safety_assertion"] is False
    assert report["remediation_evidence"]["status"] == "shared_external_failure_proven"
    assert report["remediation_parent"]["candidate_id"] == "lr_a"
    assert report["remediation_parent"]["release_eligible"] is False
    assert report["remediation_parent"]["scope"] == "remediation_only_not_release"


def test_fairness_rejects_seed_difference(tmp_path: Path) -> None:
    candidates, references, config, data = _selection_fixture(
        tmp_path,
        first_passed=True,
        second_passed=True,
    )
    second = data[1]
    training = deepcopy(second["training"])
    training["seed"] += 1
    _write_json(second["training_path"], training)
    with pytest.raises(PilotSelectionError, match="not fair"):
        select_pilot_learning_rate(
            candidates,
            reference_manifest_path=references,
            evaluation_config_path=config,
        )


def test_model_tree_tamper_fails_closed(tmp_path: Path) -> None:
    candidates, references, config, data = _selection_fixture(
        tmp_path,
        first_passed=True,
        second_passed=True,
    )
    (data[0]["model_root"] / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(PilotSelectionError, match="differs from inventory"):
        select_pilot_learning_rate(
            candidates,
            reference_manifest_path=references,
            evaluation_config_path=config,
        )


def test_exact_tie_break_prefers_lower_learning_rate(tmp_path: Path) -> None:
    candidates, references, config, _data = _selection_fixture(
        tmp_path,
        first_passed=True,
        second_passed=True,
        first_score=0.9,
        second_score=0.9,
        first_loss=0.1,
        second_loss=0.1,
    )
    report = select_pilot_learning_rate(
        candidates,
        reference_manifest_path=references,
        evaluation_config_path=config,
    )
    assert report["release_winner"]["candidate_id"] == "lr_a"


def test_report_validation_and_writer_are_immutable(tmp_path: Path) -> None:
    candidates, references, config, _data = _selection_fixture(
        tmp_path,
        first_passed=True,
        second_passed=True,
    )
    report = select_pilot_learning_rate(
        candidates,
        reference_manifest_path=references,
        evaluation_config_path=config,
    )
    assert validate_pilot_selection_report(report) == report
    output = tmp_path / "selection.json"
    write_pilot_selection_report(report, output)
    with pytest.raises(PilotSelectionError, match="overwrite"):
        write_pilot_selection_report(report, output)
    tampered = deepcopy(report)
    tampered["score_policy"]["weights"]["safety_content"] = 0.59
    with pytest.raises(PilotSelectionError):
        validate_pilot_selection_report(tampered)


def test_frozen_report_accepts_windows_absolute_paths_under_posix_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Path(__file__).parents[1]
    report = json.loads(
        (
            project / "artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"
        ).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        "scriber_polishing.pilot_selection.Path",
        PurePosixPath,
    )

    assert validate_pilot_selection_report(report) == report


def test_frozen_report_accepts_posix_absolute_paths_under_windows_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Path(__file__).parents[1]
    report = json.loads(
        (
            project / "artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"
        ).read_text(encoding="utf-8")
    )
    paths_by_candidate: dict[str, str] = {}
    for candidate in report["inputs"]["candidates"]:
        candidate_id = str(candidate["candidate_id"])
        path = f"/outputs/pilot/{candidate_id}/final-model"
        candidate["model_tree"]["path"] = path
        paths_by_candidate[candidate_id] = path
    selected = report["release_winner"] or report["remediation_parent"]
    if selected is not None:
        selected["model_tree"]["path"] = paths_by_candidate[str(selected["candidate_id"])]
    monkeypatch.setattr(
        "scriber_polishing.pilot_selection.Path",
        PureWindowsPath,
    )

    assert validate_pilot_selection_report(report) == report


def test_frozen_report_accepts_windows_unc_absolute_paths_under_posix_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Path(__file__).parents[1]
    report = json.loads(
        (
            project / "artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"
        ).read_text(encoding="utf-8")
    )
    paths_by_candidate: dict[str, str] = {}
    for candidate in report["inputs"]["candidates"]:
        candidate_id = str(candidate["candidate_id"])
        path = rf"\\scriber-storage\private-runs\{candidate_id}\final-model"
        candidate["model_tree"]["path"] = path
        paths_by_candidate[candidate_id] = path
    selected = report["release_winner"] or report["remediation_parent"]
    if selected is not None:
        selected["model_tree"]["path"] = paths_by_candidate[str(selected["candidate_id"])]
    monkeypatch.setattr(
        "scriber_polishing.pilot_selection.Path",
        PurePosixPath,
    )

    assert validate_pilot_selection_report(report) == report


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "relative/model",
        r"C:drive-relative\model",
        "/outputs/../escape",
        r"C:\outputs\..\escape",
        r"\root-relative\model",
    ],
)
def test_frozen_report_rejects_relative_drive_relative_and_traversing_paths(
    unsafe_path: str,
) -> None:
    project = Path(__file__).parents[1]
    report = json.loads(
        (
            project / "artifacts/evaluation/pilot_compare_v1/selection/pilot_learning_rate.config656558.v1.json"
        ).read_text(encoding="utf-8")
    )
    for candidate in report["inputs"]["candidates"]:
        candidate["model_tree"]["path"] = unsafe_path
    selected = report["release_winner"] or report["remediation_parent"]
    if selected is not None:
        selected["model_tree"]["path"] = unsafe_path

    with pytest.raises(PilotSelectionError, match="model-tree path must be absolute"):
        validate_pilot_selection_report(report)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        r"C:\absolute\model.safetensors",
        r"\\server\share\model.safetensors",
        "C:drive-relative.safetensors",
    ],
)
def test_model_inventory_rejects_windows_absolute_unc_and_drive_relative_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    candidates, references, config, data = _selection_fixture(
        tmp_path,
        first_passed=True,
        second_passed=True,
    )
    training = deepcopy(data[0]["training"])
    final_model = training["final_model"]
    inventory = final_model["inventory"]
    inventory["files"][0]["path"] = unsafe_path
    inventory["tree_sha256"] = _tree_hash(inventory["files"])
    _write_json(
        data[0]["model_root"] / "checkpoint-inventory.json",
        inventory,
    )
    _write_json(data[0]["training_path"], training)

    with pytest.raises(PilotSelectionError, match="inventory contains unsafe path"):
        select_pilot_learning_rate(
            candidates,
            reference_manifest_path=references,
            evaluation_config_path=config,
        )


def test_cli_builds_same_fail_closed_report(tmp_path: Path) -> None:
    candidates, references, config, _data = _selection_fixture(
        tmp_path,
        first_passed=False,
        second_passed=False,
    )
    script = Path(__file__).parents[1] / "scripts" / "select_pilot_learning_rate.py"
    output = tmp_path / "cli-selection.json"
    command = [
        sys.executable,
        str(script),
        "--reference-manifest",
        str(references),
        "--evaluation-config",
        str(config),
    ]
    for candidate in candidates:
        command.extend(
            [
                "--candidate",
                candidate.candidate_id,
                str(candidate.training_report),
                str(candidate.model_tree),
                str(candidate.prediction_manifest),
                str(candidate.evaluation_report),
            ]
        )
        for part in candidate.prediction_part_manifests:
            command.extend(
                [
                    "--prediction-part-manifest",
                    candidate.candidate_id,
                    str(part),
                ]
            )
    command.extend(["--output", str(output)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "no_release_winner"
    assert report["release_winner"] is None

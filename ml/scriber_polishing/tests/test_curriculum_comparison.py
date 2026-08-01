from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.curriculum_comparison import (
    compare_curriculum,
    validate_curriculum_comparison_config,
    validate_curriculum_comparison_report,
    write_curriculum_comparison_report,
)
from scriber_polishing.evaluation_reference_builder import write_evaluation_references
from scriber_polishing.metrics import EvaluationCase
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed": 270014,
        "bootstrap_resamples": 100,
        "confidence_level": 0.95,
        "minimum_composite_delta": 0.005,
        "maximum_eval_loss_ratio": 1.02,
        "weights": {
            "safety_content": 0.60,
            "task_quality": 0.25,
            "structure": 0.10,
            "eval_loss": 0.05,
        },
        "require_no_new_critical_errors": True,
        "require_metric_non_regression": True,
    }


def _cases(*, exact: bool, count: int = 4) -> tuple[EvaluationCase, ...]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    plain = render_plain_text(document)
    return tuple(
        EvaluationCase(
            case_id=f"dev_case_{index:02d}",
            source_text="bitte bis 31.07.2026 antworten",
            target_sst=target_sst,
            prediction_sst=target_sst if exact else "kein gueltiges SST",
            target_plain_text=plain,
            target_ast=document_to_dict(document),
            protected_values=("31.07.2026",),
            slice_tags=("curriculum",),
        )
        for index in range(count)
    )


def _inputs(count: int = 4) -> dict[str, object]:
    return {
        "references_sha256": "1" * 64,
        "control_predictions_sha256": "2" * 64,
        "staged_predictions_sha256": "3" * 64,
        "control_training_report_sha256": "4" * 64,
        "staged_training_report_sha256": "5" * 64,
        "case_count": count,
    }


def _reference(case_id: str) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": "regression",
        "source_text": "bitte bis 31.07.2026 antworten",
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
                    "text": "Die Frist endet am 31.07.2026.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["31.07.2026"],
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
        "protected_spans": ["31.07.2026"],
        "slice_tags": ["curriculum"],
        "is_identity": False,
    }


def _training_report(*, mode: str, plan_sha256: str, eval_loss: float) -> dict[str, object]:
    return {
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "revision",
        "run_kind": "pilot",
        "seed": 270013,
        "method": "full_sft",
        "optimizer": "adamw_torch_fused",
        "learning_rate": 0.00002,
        "max_sequence_length": 512,
        "micro_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "effective_batch_size": 16,
        "max_steps": 433,
        "schedule": {"max_steps": 433},
        "early_stopping": False,
        "train_examples_loaded": 7000,
        "train_examples_used": 6926,
        "validation_examples_loaded": 700,
        "validation_examples_used": 700,
        "oversize_rejections": {"train": [], "validation": []},
        "bindings": {"dataset": {"sha256": "dataset"}},
        "eval_metrics": {"eval_loss": eval_loss},
        "training_completed": True,
        "curriculum_order": {
            "mode": mode,
            "plan_sha256": plan_sha256,
            "algorithm": "phase_grouped_seeded_sha256_v1",
            "phase_rules_version": "scriber_curriculum_phase_v1",
            "seed": 270013,
            "epochs": 1,
            "record_count": 7000,
            "phase_counts": {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1},
            "source_sha256": "a" * 64,
            "ai_gold_train_sha256": "b" * 64,
        },
    }


def test_strictly_better_staged_candidate_is_adopted_deterministically() -> None:
    kwargs = {
        "control_cases": _cases(exact=False),
        "staged_cases": _cases(exact=True),
        "control_eval_loss": 1.0,
        "staged_eval_loss": 0.99,
        "config": _config(),
        "config_sha256": "a" * 64,
        "inputs": _inputs(),
    }

    first = compare_curriculum(**kwargs)
    second = compare_curriculum(**kwargs)

    assert first == second
    assert first["decision"] == "ADOPT_STAGED"
    assert all(first["checks"].values())
    assert first["bootstrap"]["lower"] > 0
    assert first["hard_safety"]["passed"] is True


def test_no_gain_or_new_safety_regression_keeps_shuffled_control() -> None:
    identical = compare_curriculum(
        _cases(exact=True),
        _cases(exact=True),
        control_eval_loss=1.0,
        staged_eval_loss=1.0,
        config=_config(),
        config_sha256="a" * 64,
        inputs=_inputs(),
    )
    assert identical["decision"] == "KEEP_SHUFFLED"
    assert identical["checks"]["minimum_composite_delta"] is False
    assert identical["checks"]["bootstrap_lower_bound"] is False

    regressed = compare_curriculum(
        _cases(exact=True),
        _cases(exact=False),
        control_eval_loss=1.0,
        staged_eval_loss=0.9,
        config=_config(),
        config_sha256="a" * 64,
        inputs=_inputs(),
    )
    assert regressed["decision"] == "KEEP_SHUFFLED"
    assert regressed["checks"]["hard_safety"] is False


def test_report_semantics_and_immutable_output_are_fail_closed(tmp_path: Path) -> None:
    report = compare_curriculum(
        _cases(exact=False),
        _cases(exact=True),
        control_eval_loss=1.0,
        staged_eval_loss=0.99,
        config=_config(),
        config_sha256="a" * 64,
        inputs=_inputs(),
    )
    destination = tmp_path / "comparison.json"
    write_curriculum_comparison_report(report, destination)
    write_curriculum_comparison_report(report, destination)

    tampered = copy.deepcopy(report)
    tampered["candidates"]["staged"]["composite"] -= 0.1
    with pytest.raises(ValueError, match="composite is inconsistent"):
        validate_curriculum_comparison_report(tampered)
    with pytest.raises(ValueError, match="composite is inconsistent|overwrite"):
        write_curriculum_comparison_report(tampered, destination)


def test_config_and_case_pairing_reject_ambiguous_inputs() -> None:
    invalid = _config()
    invalid["weights"] = {
        "safety_content": 0.60,
        "task_quality": 0.25,
        "structure": 0.10,
        "eval_loss": 0.10,
    }
    with pytest.raises(ValueError, match="sum to exactly 1"):
        validate_curriculum_comparison_config(invalid)
    non_finite = _config()
    non_finite["minimum_composite_delta"] = float("nan")
    with pytest.raises(ValueError, match="finite numbers"):
        validate_curriculum_comparison_config(non_finite)

    staged = list(_cases(exact=True))
    staged.reverse()
    with pytest.raises(ValueError, match="identical ordered IDs"):
        compare_curriculum(
            _cases(exact=False),
            staged,
            control_eval_loss=1.0,
            staged_eval_loss=0.99,
            config=_config(),
            config_sha256="a" * 64,
            inputs=_inputs(),
        )


def test_comparison_cli_requires_matched_training_evidence_and_writes_decision(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    references_path = tmp_path / "references.jsonl"
    control_predictions_path = tmp_path / "control.jsonl"
    staged_predictions_path = tmp_path / "staged.jsonl"
    control_report_path = tmp_path / "control.report.json"
    staged_report_path = tmp_path / "staged.report.json"
    config_path = tmp_path / "config.yaml"
    output_path = tmp_path / "comparison.json"
    references = [_reference(f"dev_case_{index:02d}") for index in range(4)]
    write_evaluation_references(references, references_path)
    target_sst = references[0]["target_sst"]
    control_predictions_path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "case_id": reference["case_id"],
                    "prediction_sst": "kein gueltiges SST",
                },
                sort_keys=True,
            )
            + "\n"
            for reference in references
        ),
        encoding="utf-8",
    )
    staged_predictions_path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "case_id": reference["case_id"],
                    "prediction_sst": target_sst,
                },
                sort_keys=True,
            )
            + "\n"
            for reference in references
        ),
        encoding="utf-8",
    )
    control_report_path.write_text(
        json.dumps(
            _training_report(
                mode="shuffled",
                plan_sha256="c" * 64,
                eval_loss=1.0,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    staged_report_path.write_text(
        json.dumps(
            _training_report(
                mode="staged",
                plan_sha256="d" * 64,
                eval_loss=0.99,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    config_path.write_text(yaml.safe_dump(_config(), sort_keys=True), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "compare_curriculum.py"),
            "--references",
            str(references_path),
            "--control-predictions",
            str(control_predictions_path),
            "--staged-predictions",
            str(staged_predictions_path),
            "--control-training-report",
            str(control_report_path),
            "--staged-training-report",
            str(staged_report_path),
            "--config",
            str(config_path),
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "decision=ADOPT_STAGED" in completed.stdout
    assert json.loads(output_path.read_text(encoding="utf-8"))["decision"] == "ADOPT_STAGED"

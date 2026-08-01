import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_gate import evaluate_candidate_gate, load_evaluation_config
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _config(path: Path) -> dict[str, object]:
    config = {
        "schema_version": 2,
        "seed": 270020,
        "candidates": ["candidate_under_test"],
        "automatic_evaluation": {
            "hard_gates": [
                {
                    "id": "output_only_compliance",
                    "slice": "all",
                    "metric": "output_only_compliance_rate",
                    "operator": "gte",
                    "threshold": 1.0,
                    "minimum_cases": 1,
                }
            ],
            "minimums": [
                {
                    "id": "challenge_parse",
                    "slice": "split:challenge",
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
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config


def _reference() -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": "eval_case_001",
        "split": "challenge",
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
                }
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
        "slice_tags": ["dates"],
        "is_identity": False,
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_candidate_gate_is_config_bound_fail_closed_and_preserves_slice_reports(tmp_path: Path) -> None:
    config_path = tmp_path / "evaluation.yaml"
    config = _config(config_path)
    loaded = load_evaluation_config(config_path)
    assert loaded == config

    metrics = {
        "slice_reports": {
            "all": {"total": 1, "output_only_compliance_rate": 0.0},
            "split:challenge": {"total": 1, "sst_parse_rate": 0.0},
        }
    }
    gate = evaluate_candidate_gate(metrics, loaded, candidate="candidate_under_test")

    assert gate["candidate"] == "candidate_under_test"
    assert gate["passed"] is False
    assert gate["status"] == "failed"
    assert {item["id"] for item in gate["failed_checks"]} == {
        "output_only_compliance",
        "challenge_parse",
    }
    assert gate["hard_gates"]["output_only_compliance"]["actual"] == 0.0
    assert gate["minimums"]["challenge_parse"]["actual"] == 0.0

    references_path = tmp_path / "references.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    output_path = tmp_path / "report.json"
    reference = _reference()
    _write_jsonl(references_path, [reference])
    _write_jsonl(
        predictions_path,
        [{"schema_version": 1, "case_id": reference["case_id"], "prediction_sst": "Keine SST-Ausgabe"}],
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "evaluate_predictions.py"),
            "--references",
            str(references_path),
            "--predictions",
            str(predictions_path),
            "--candidate",
            "reporting_name",
            "--gate-candidate",
            "candidate_under_test",
            "--config",
            str(config_path),
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["candidate"] == "reporting_name"
    assert report["gate_candidate"] == "candidate_under_test"
    assert report["evaluation_config_sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert report["gate"]["candidate"] == "candidate_under_test"
    assert report["gate"]["passed"] is False
    assert report["metrics"]["slice_reports"]["dates"]["total"] == 1
    assert report["metrics"]["slice_reports"]["split:challenge"]["total"] == 1


def test_repository_judge_thresholds_match_the_frozen_release_policy() -> None:
    config = load_evaluation_config(Path(__file__).parents[1] / "configs" / "evaluation.yaml")

    assert config["judge_aggregation"]["acceptable_rate_minimums"] == {
        "test": 0.97,
        "challenge": 0.95,
    }
    assert config["judge_aggregation"]["rubric_minimums"] == {
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
    }

    weakened = deepcopy(config)
    weakened["judge_aggregation"]["rubric_minimums"]["semantic_fidelity"] = 4.49
    with pytest.raises(ValueError, match="evaluation config failed schema"):
        evaluate_candidate_gate({}, weakened, candidate="final")


def test_semantic_hard_gate_fails_closed_without_attached_semantic_plan_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path / "evaluation.yaml")
    config["automatic_evaluation"]["hard_gates"].append(
        {
            "id": "semantic_omissions",
            "slice": "all",
            "metric": "semantic_hard_check_counts.semantic_omission",
            "operator": "lte",
            "threshold": 0,
            "minimum_cases": 1,
        }
    )

    gate = evaluate_candidate_gate(
        {
            "slice_reports": {
                "all": {"total": 1, "output_only_compliance_rate": 1.0},
                "split:challenge": {"total": 1, "sst_parse_rate": 1.0},
            }
        },
        config,
        candidate="candidate_under_test",
    )

    assert gate["passed"] is False
    assert gate["hard_gates"]["semantic_omissions"]["reason"] == "metric_not_evaluated"

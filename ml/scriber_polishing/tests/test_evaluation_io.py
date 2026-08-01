import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_io import (
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _reference(case_id: str = "eval_case_001") -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": "test",
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
        "slice_tags": ["business_mail", "dates"],
        "is_identity": False,
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_frozen_reference_and_candidate_jsonl_join_only_on_exact_complete_case_ids(tmp_path: Path) -> None:
    references_path = tmp_path / "references.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(references_path, [_reference()])
    _write_jsonl(
        predictions_path,
        [
            {
                "schema_version": 1,
                "case_id": "eval_case_001",
                "prediction_sst": "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]",
            }
        ],
    )

    references = load_evaluation_references(references_path)
    predictions = load_candidate_predictions(predictions_path)
    cases = join_evaluation_cases(references, predictions)

    assert len(cases) == 1
    assert cases[0].case_id == "eval_case_001"
    assert cases[0].slice_tags == ("business_mail", "dates", "split:test")
    assert cases[0].protected_values == ("31.07.2026",)


def test_evaluation_jsonl_rejects_unknown_fields_duplicates_blanks_and_incomplete_coverage(
    tmp_path: Path,
) -> None:
    references_path = tmp_path / "references.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    invalid = _reference()
    invalid["candidate_model"] = "must_not_be_accepted"
    _write_jsonl(references_path, [invalid])

    with pytest.raises(ValueError, match="reference schema"):
        load_evaluation_references(references_path)

    _write_jsonl(references_path, [_reference(), _reference()])
    with pytest.raises(ValueError, match="duplicate reference case_id"):
        load_evaluation_references(references_path)

    references_path.write_text(
        json.dumps(_reference(), ensure_ascii=False) + "\n\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="blank JSONL line"):
        load_evaluation_references(references_path)

    _write_jsonl(references_path, [_reference(), _reference("eval_case_002")])
    _write_jsonl(
        predictions_path,
        [{"schema_version": 1, "case_id": "eval_case_001", "prediction_sst": "[DOC]\n[P]Text.[/P]\n[/DOC]"}],
    )
    with pytest.raises(ValueError, match="missing candidate prediction"):
        join_evaluation_cases(
            load_evaluation_references(references_path),
            load_candidate_predictions(predictions_path),
        )


def test_frozen_reference_schema_rejects_an_ast_that_disagrees_with_sst(tmp_path: Path) -> None:
    references_path = tmp_path / "references.jsonl"
    reference = _reference()
    reference["target_ast"] = document_to_dict(parse_sst("[DOC]\n[P]Anderer Inhalt.[/P]\n[/DOC]"))
    _write_jsonl(references_path, [reference])

    with pytest.raises(ValueError, match="target_ast differs"):
        load_evaluation_references(references_path)

    identity = _reference()
    identity["is_identity"] = True
    _write_jsonl(references_path, [identity])
    with pytest.raises(ValueError, match="identity reference"):
        load_evaluation_references(references_path)


def test_frozen_reference_accepts_render_equivalent_implicit_plain_inlines(tmp_path: Path) -> None:
    references_path = tmp_path / "references.jsonl"
    reference = _reference()
    target_ast = reference["target_ast"]
    assert isinstance(target_ast, dict)
    blocks = target_ast["blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    block["inlines"] = []
    _write_jsonl(references_path, [reference])

    references = load_evaluation_references(references_path)

    assert references == (reference,)


def test_evaluation_cli_writes_a_hash_bound_slice_level_report(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    references_path = tmp_path / "references.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    report_path = tmp_path / "report.json"
    config_path = tmp_path / "evaluation.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "seed": 270020,
                "candidates": ["candidate_under_test"],
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
    reference = _reference()
    _write_jsonl(references_path, [reference])
    _write_jsonl(
        predictions_path,
        [
            {
                "schema_version": 1,
                "case_id": reference["case_id"],
                "prediction_sst": reference["target_sst"],
            }
        ],
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "evaluate_predictions.py"),
            "--references",
            str(references_path),
            "--predictions",
            str(predictions_path),
            "--candidate",
            "candidate_under_test",
            "--config",
            str(config_path),
            "--output",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["candidate"] == "candidate_under_test"
    assert len(report["reference_sha256"]) == 64
    assert len(report["prediction_sha256"]) == 64
    assert report["metrics"]["hard_gate_passed"] is True
    assert report["metrics"]["slice_reports"]["dates"]["total"] == 1

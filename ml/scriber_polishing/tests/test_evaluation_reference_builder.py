from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_io import load_evaluation_references
from scriber_polishing.evaluation_reference_builder import (
    build_evaluation_references,
    write_evaluation_references,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _pair(example_id: str, *, split: str, identity: bool = False) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    target_plain_text = render_plain_text(document)
    return {
        "example_id": example_id,
        "split": split,
        "source_text": target_plain_text if identity else "bitte bis 31.07.2026 antworten",
        "target_sst": target_sst,
        "target_plain_text": target_plain_text,
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
        "risk_tags": ["business-mail", "Dates"],
        "domain": "general_business",
        "document_type": "Geschäftsbrief",
        "address_mode": "formal_sie",
        "corruption_profile": "identity" if identity else "punctuation_case",
        "operations": [] if identity else ["lowercase_sentence_start"],
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_builds_closed_hash_bound_ai_gold_references_without_mutating_source_records(
    tmp_path: Path,
) -> None:
    rows = [
        _pair("gold_test_001", split="test"),
        _pair("gold_test_002", split="test", identity=True),
    ]
    original = json.loads(json.dumps(rows))

    references = build_evaluation_references(rows, output_split="ai_gold_test")

    assert rows == original
    assert [item["case_id"] for item in references] == ["gold_test_001", "gold_test_002"]
    assert {item["split"] for item in references} == {"ai_gold_test"}
    assert references[0]["slice_tags"] == [
        "business_mail",
        "dates",
        "domain:general_business",
        "document_type:geschaftsbrief",
        "operation:lowercase_sentence_start",
        "profile:punctuation_case",
    ]
    assert references[0]["is_identity"] is False
    assert references[0]["address_mode"] == "formal_sie"
    assert references[1]["is_identity"] is True

    destination = tmp_path / "references.jsonl"
    manifest = write_evaluation_references(references, destination)
    assert manifest["record_count"] == 2
    assert manifest["records_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert load_evaluation_references(destination) == tuple(references)


def test_reference_builder_rejects_wrong_input_split_and_refuses_changed_overwrite(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="input split"):
        build_evaluation_references(
            [_pair("gold_test_001", split="challenge")],
            output_split="ai_gold_test",
        )

    destination = tmp_path / "references.jsonl"
    first = build_evaluation_references(
        [_pair("gold_test_001", split="test")],
        output_split="ai_gold_test",
    )
    write_evaluation_references(first, destination)
    changed = build_evaluation_references(
        [_pair("gold_test_002", split="test")],
        output_split="ai_gold_test",
    )
    with pytest.raises(ValueError, match="overwrite"):
        write_evaluation_references(changed, destination)


def test_reference_builder_cli_verifies_exact_source_hash_before_opening_holdout(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    source = tmp_path / "ai_gold_test.sealed.jsonl"
    output = tmp_path / "ai_gold_test.references.jsonl"
    manifest = tmp_path / "ai_gold_test.references.manifest.json"
    _write_jsonl(source, [_pair("gold_test_001", split="test")])
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_evaluation_references.py"),
            "--input",
            str(source),
            "--expected-input-sha256",
            source_sha256,
            "--output-split",
            "ai_gold_test",
            "--output",
            str(output),
            "--manifest",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "evaluation_references=pass records=1" in completed.stdout
    assert json.loads(manifest.read_text(encoding="utf-8"))["input_sha256"] == source_sha256
    assert len(load_evaluation_references(output)) == 1

    rejected_output = tmp_path / "rejected.jsonl"
    rejected = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_evaluation_references.py"),
            "--input",
            str(source),
            "--expected-input-sha256",
            "0" * 64,
            "--output-split",
            "ai_gold_test",
            "--output",
            str(rejected_output),
            "--manifest",
            str(tmp_path / "rejected.manifest.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "input SHA-256" in rejected.stderr
    assert not rejected_output.exists()

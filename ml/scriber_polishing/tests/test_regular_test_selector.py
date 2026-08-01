from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_io import load_evaluation_references
from scriber_polishing.regular_test_selector import (
    select_regular_test_records,
    validate_regular_test_selection_manifest,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _pair(
    example_id: str,
    *,
    parent: str,
    domain: str,
    profile: str,
) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "example_id": example_id,
        "parent_canonical_document_id": parent,
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
        "risk_tags": ["dates"],
        "domain": domain,
        "document_type": "Geschäftsbrief",
        "address_mode": "formal_sie",
        "corruption_profile": profile,
        "operations": ["punctuation_error"],
    }


def _selection_fixture() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    regular = [
        _pair(
            f"regular_{domain}_{profile}_{index}",
            parent=f"parent_{domain}_{profile}_{index}",
            domain=domain,
            profile=profile,
        )
        for domain in ("business", "technical")
        for profile in ("grammar", "punctuation")
        for index in range(3)
    ]
    gold = [
        _pair(
            f"gold_{domain}_{profile}",
            parent=f"gold_parent_{domain}",
            domain=domain,
            profile=profile,
        )
        for domain in ("business", "technical")
        for profile in ("grammar", "punctuation")
    ]
    return [*regular, *gold], gold


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_selects_seeded_balanced_regular_cases_without_ai_gold_ids_or_parent_groups() -> None:
    source, gold = _selection_fixture()

    first = select_regular_test_records(source, gold, target_count=8, seed=1_100_1000)
    second = select_regular_test_records(
        list(reversed(source)),
        list(reversed(gold)),
        target_count=8,
        seed=1_100_1000,
    )

    assert [row["example_id"] for row in first.records] == [row["example_id"] for row in second.records]
    assert len(first.records) == 8
    assert first.audit["selected_ai_gold_id_overlap_count"] == 0
    assert first.audit["selected_ai_gold_parent_overlap_count"] == 0
    assert first.audit["parent_exclusion_mode"] == "full"
    assert first.audit["domain_counts"] == {"business": 4, "technical": 4}
    assert first.audit["corruption_profile_counts"] == {
        "grammar": 4,
        "punctuation": 4,
    }
    assert set(first.audit["domain_profile_counts"].values()) == {2}


def test_regular_reference_cli_hash_binds_inputs_validates_references_and_refuses_changed_overwrite(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    source, gold = _selection_fixture()
    source_path = tmp_path / "test.jsonl"
    gold_path = tmp_path / "ai_gold_test.sealed.jsonl"
    output_path = tmp_path / "regular_test_1000.references.jsonl"
    manifest_path = tmp_path / "regular_test_1000.references.manifest.json"
    _write_jsonl(source_path, source)
    _write_jsonl(gold_path, gold)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    gold_sha256 = hashlib.sha256(gold_path.read_bytes()).hexdigest()

    command = [
        sys.executable,
        str(project_root / "scripts" / "build_regular_test_references.py"),
        "--input",
        str(source_path),
        "--expected-input-sha256",
        source_sha256,
        "--ai-gold-test",
        str(gold_path),
        "--expected-ai-gold-sha256",
        gold_sha256,
        "--expected-ai-gold-count",
        "4",
        "--target-count",
        "8",
        "--seed",
        "11001000",
        "--output",
        str(output_path),
        "--manifest",
        str(manifest_path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert "regular_test_selection=pass records=8" in completed.stdout
    references = load_evaluation_references(output_path)
    manifest = validate_regular_test_selection_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert len(references) == 8
    assert manifest["source"]["records_sha256"] == source_sha256
    assert manifest["exclusion"]["ai_gold_test_records_sha256"] == gold_sha256
    assert manifest["output"]["records_sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert manifest["validation"]["validated_record_count"] == 8
    tampered = copy.deepcopy(manifest)
    tampered["exclusion"]["selected_ai_gold_id_overlap_count"] = 1
    with pytest.raises(ValueError, match="manifest failed schema"):
        validate_regular_test_selection_manifest(tampered)

    changed = subprocess.run(
        [*command[: command.index("--seed") + 1], "11001001", *command[command.index("--seed") + 2 :]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert changed.returncode != 0
    assert "overwrite" in changed.stderr

    rejected_output = tmp_path / "rejected.references.jsonl"
    rejected_manifest = tmp_path / "rejected.references.manifest.json"
    rejected_command = command.copy()
    rejected_command[rejected_command.index(source_sha256)] = "0" * 64
    rejected_command[rejected_command.index(str(output_path))] = str(rejected_output)
    rejected_command[rejected_command.index(str(manifest_path))] = str(rejected_manifest)
    rejected = subprocess.run(
        rejected_command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "input SHA-256 mismatch" in rejected.stderr
    assert not rejected_output.exists()
    assert not rejected_manifest.exists()


def test_regular_reference_cli_rejects_a_selected_case_that_fails_the_existing_reference_validator(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    source, gold = _selection_fixture()
    invalid_ast = document_to_dict(parse_sst("[DOC]\n[P]Anderer Inhalt.[/P]\n[/DOC]"))
    for row in source:
        if str(row["example_id"]).startswith("regular_"):
            row["target_ast"] = invalid_ast
    source_path = tmp_path / "test.jsonl"
    gold_path = tmp_path / "ai_gold_test.sealed.jsonl"
    output_path = tmp_path / "regular.references.jsonl"
    manifest_path = tmp_path / "regular.references.manifest.json"
    _write_jsonl(source_path, source)
    _write_jsonl(gold_path, gold)

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "build_regular_test_references.py"),
            "--input",
            str(source_path),
            "--expected-input-sha256",
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "--ai-gold-test",
            str(gold_path),
            "--expected-ai-gold-sha256",
            hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            "--expected-ai-gold-count",
            "4",
            "--target-count",
            "8",
            "--seed",
            "11001000",
            "--output",
            str(output_path),
            "--manifest",
            str(manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "target_ast differs from target_sst" in completed.stderr
    assert not output_path.exists()
    assert not manifest_path.exists()

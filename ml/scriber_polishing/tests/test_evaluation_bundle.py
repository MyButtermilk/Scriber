from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_bundle import (
    EvaluationBundleError,
    EvaluationPackagePart,
    combine_prediction_packages,
    combine_reference_packages,
    scope_evaluation_package,
    validate_evaluation_bundle_manifest,
)
from scriber_polishing.evaluation_io import (
    load_candidate_predictions,
    load_evaluation_references,
)
from scriber_polishing.evaluation_reference_builder import write_evaluation_references
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _reference(case_id: str, split: str) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": split,
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
        "slice_tags": [f"split_{split}"],
        "is_identity": False,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_reference_part(
    root: Path,
    name: str,
    records: list[dict[str, object]],
) -> EvaluationPackagePart:
    records_path = root / f"{name}.references.jsonl"
    manifest_path = root / f"{name}.references.manifest.json"
    manifest = write_evaluation_references(records, records_path)
    manifest["output_filename"] = records_path.name
    _write_json(manifest_path, manifest)
    return EvaluationPackagePart(records_path, manifest_path)


def _prediction_payload(records: list[dict[str, object]]) -> bytes:
    return "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def _write_prediction_part(
    root: Path,
    name: str,
    case_ids: list[str],
    *,
    candidate: str = "pilot",
) -> EvaluationPackagePart:
    records_path = root / f"{name}.predictions.jsonl"
    manifest_path = root / f"{name}.predictions.manifest.json"
    records = [
        {
            "schema_version": 1,
            "case_id": case_id,
            "prediction_sst": "[DOC]\n[P]Bitte bis 31.07.2026 antworten.[/P]\n[/DOC]",
        }
        for case_id in case_ids
    ]
    payload = _prediction_payload(records)
    records_path.write_bytes(payload)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "candidate": candidate,
            "record_count": len(records),
            "valid_sst_count": len(records),
            "restoration_error_count": 0,
            "prediction_sha256": hashlib.sha256(payload).hexdigest(),
            "total_generation_seconds": 0.1 * len(records),
            "total_input_tokens": 10 * len(records),
            "total_output_tokens": 12 * len(records),
        },
    )
    return EvaluationPackagePart(records_path, manifest_path)


def _write_regular_selection_reference_part(
    root: Path,
    name: str,
    records: list[dict[str, object]],
) -> EvaluationPackagePart:
    part = _write_reference_part(root, name, records)
    payload_sha256 = hashlib.sha256(part.records_path.read_bytes()).hexdigest()
    case_ids_sha256 = hashlib.sha256(
        (
            json.dumps(
                [record["case_id"] for record in records],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    count = len(records)
    _write_json(
        part.manifest_path,
        {
            "schema_version": 1,
            "kind": "scriber_regular_test_selection",
            "selection_algorithm": "domain_profile_parent_round_robin_sha256_v1",
            "seed": 270041,
            "target_count": count,
            "source": {
                "filename": "source.references.jsonl",
                "records_sha256": "a" * 64,
                "record_count": count + 1,
                "split": "test",
            },
            "exclusion": {
                "ai_gold_test_filename": "ai-gold.references.jsonl",
                "ai_gold_test_records_sha256": "b" * 64,
                "ai_gold_test_count": 1,
                "ai_gold_ids_matched_in_source_count": 1,
                "ai_gold_parent_group_count": 1,
                "eligible_after_id_exclusion": count,
                "eligible_after_parent_exclusion": count,
                "parent_exclusion_mode": "full",
                "selected_ai_gold_id_overlap_count": 0,
                "selected_ai_gold_parent_overlap_count": 0,
            },
            "output": {
                "filename": part.records_path.name,
                "records_sha256": payload_sha256,
                "case_ids_sha256": case_ids_sha256,
                "record_count": count,
                "split_counts": {"test": count},
            },
            "coverage": {
                "unique_parent_count": count,
                "domain_counts": {"business": count},
                "corruption_profile_counts": {"clean": count},
                "domain_profile_counts": {"business:clean": count},
                "operation_counts": {"punctuation": count},
                "domain_balance_delta": 0,
                "corruption_profile_balance_delta": 0,
            },
            "validation": {
                "reference_validator": ("scriber_polishing.evaluation_io.validate_evaluation_reference"),
                "validated_record_count": count,
            },
        },
    )
    return part


def _reference_bundle(
    tmp_path: Path,
) -> tuple[EvaluationPackagePart, EvaluationPackagePart, Path, Path]:
    first = _write_reference_part(
        tmp_path,
        "first",
        [_reference("case_a", "test"), _reference("case_b", "test")],
    )
    second = _write_reference_part(
        tmp_path,
        "second",
        [_reference("case_c", "challenge")],
    )
    output = tmp_path / "all.references.jsonl"
    manifest = tmp_path / "all.references.manifest.json"
    combine_reference_packages(
        [first, second],
        destination=output,
        manifest_destination=manifest,
    )
    return first, second, output, manifest


def test_combines_references_in_declared_order_with_hash_bound_manifest(
    tmp_path: Path,
) -> None:
    first, second, output, manifest_path = _reference_bundle(tmp_path)

    rows = load_evaluation_references(output)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [row["case_id"] for row in rows] == ["case_a", "case_b", "case_c"]
    assert manifest["split_counts"] == {"challenge": 1, "test": 2}
    assert manifest["record_count"] == 3
    assert manifest["records_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert [item["position"] for item in manifest["inputs"]] == [0, 1]
    assert manifest["inputs"][0]["manifest_sha256"] == hashlib.sha256(first.manifest_path.read_bytes()).hexdigest()
    assert manifest["inputs"][1]["records_sha256"] == hashlib.sha256(second.records_path.read_bytes()).hexdigest()
    assert validate_evaluation_bundle_manifest(manifest) == manifest


def test_combines_regular_test_selection_reference_manifest(tmp_path: Path) -> None:
    ai_gold = _write_reference_part(
        tmp_path,
        "ai_gold",
        [_reference("case_a", "challenge")],
    )
    regular = _write_regular_selection_reference_part(
        tmp_path,
        "regular",
        [_reference("case_b", "test"), _reference("case_c", "test")],
    )

    output = tmp_path / "combined.references.jsonl"
    manifest_path = tmp_path / "combined.references.manifest.json"
    manifest = combine_reference_packages(
        [ai_gold, regular],
        destination=output,
        manifest_destination=manifest_path,
    )

    assert manifest["record_count"] == 3
    assert manifest["split_counts"] == {"challenge": 1, "test": 2}
    assert manifest["inputs"][1]["manifest_sha256"] == hashlib.sha256(regular.manifest_path.read_bytes()).hexdigest()


def test_combines_one_candidate_and_requires_exact_join_coverage(tmp_path: Path) -> None:
    _, _, references, reference_manifest = _reference_bundle(tmp_path)
    first = _write_prediction_part(tmp_path, "pred_first", ["case_a", "case_b"])
    second = _write_prediction_part(tmp_path, "pred_second", ["case_c"])
    output = tmp_path / "all.predictions.jsonl"
    manifest_path = tmp_path / "all.predictions.manifest.json"

    manifest = combine_prediction_packages(
        [second, first],
        candidate="pilot",
        references=references,
        reference_manifest=reference_manifest,
        destination=output,
        manifest_destination=manifest_path,
    )

    rows = load_candidate_predictions(output)
    assert [row["case_id"] for row in rows] == ["case_c", "case_a", "case_b"]
    assert manifest["candidate"] == "pilot"
    assert manifest["prediction_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["exact_case_coverage"] is True
    assert manifest["reference"]["record_count"] == 3

    incomplete = _write_prediction_part(tmp_path, "incomplete", ["case_a", "case_b"])
    with pytest.raises(EvaluationBundleError, match="coverage.*missing=case_c"):
        combine_prediction_packages(
            [incomplete],
            candidate="pilot",
            references=references,
            reference_manifest=reference_manifest,
            destination=tmp_path / "incomplete.bundle.jsonl",
            manifest_destination=tmp_path / "incomplete.bundle.manifest.json",
        )


def test_combines_legacy_standard_prediction_manifests_with_case_count(
    tmp_path: Path,
) -> None:
    _, _, references, reference_manifest = _reference_bundle(tmp_path)
    legacy = _write_prediction_part(
        tmp_path,
        "legacy_standard",
        ["case_a", "case_b", "case_c"],
        candidate="deterministic_minimal",
    )
    legacy_manifest = json.loads(legacy.manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["case_count"] = legacy_manifest.pop("record_count")
    legacy_manifest["producer"] = "scriber_polishing.standard_predictions.v1"
    _write_json(legacy.manifest_path, legacy_manifest)

    output = tmp_path / "legacy.bundle.jsonl"
    manifest_path = tmp_path / "legacy.bundle.manifest.json"
    manifest = combine_prediction_packages(
        [legacy],
        candidate="deterministic_minimal",
        references=references,
        reference_manifest=reference_manifest,
        destination=output,
        manifest_destination=manifest_path,
    )

    assert manifest["record_count"] == 3
    assert manifest["candidate"] == "deterministic_minimal"
    assert manifest["inputs"][0]["manifest_sha256"] == hashlib.sha256(legacy.manifest_path.read_bytes()).hexdigest()

    conflicting = json.loads(legacy.manifest_path.read_text(encoding="utf-8"))
    conflicting["prediction_count"] = 4
    _write_json(legacy.manifest_path, conflicting)
    with pytest.raises(EvaluationBundleError, match="conflicting record counts"):
        combine_prediction_packages(
            [legacy],
            candidate="deterministic_minimal",
            references=references,
            reference_manifest=reference_manifest,
            destination=tmp_path / "conflicting.bundle.jsonl",
            manifest_destination=tmp_path / "conflicting.bundle.manifest.json",
        )


def test_scopes_reference_and_prediction_case_ids_deterministically(
    tmp_path: Path,
) -> None:
    reference = _write_reference_part(
        tmp_path,
        "critical",
        [_reference("case_a", "challenge"), _reference("case_b", "challenge")],
    )
    prediction = _write_prediction_part(
        tmp_path,
        "critical",
        ["case_a", "case_b"],
        candidate="final",
    )
    scoped_reference = tmp_path / "scoped.references.jsonl"
    scoped_reference_manifest = tmp_path / "scoped.references.manifest.json"
    scoped_prediction = tmp_path / "scoped.predictions.jsonl"
    scoped_prediction_manifest = tmp_path / "scoped.predictions.manifest.json"

    reference_result = scope_evaluation_package(
        reference,
        namespace="criticalreg",
        prediction=False,
        split_override="regression",
        add_slice_tags=("critical_regression",),
        destination=scoped_reference,
        manifest_destination=scoped_reference_manifest,
    )
    prediction_result = scope_evaluation_package(
        prediction,
        namespace="criticalreg",
        prediction=True,
        candidate="final",
        destination=scoped_prediction,
        manifest_destination=scoped_prediction_manifest,
    )

    reference_rows = load_evaluation_references(scoped_reference)
    prediction_rows = load_candidate_predictions(scoped_prediction)
    expected_ids = [
        f"criticalreg_{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:40]}" for case_id in ("case_a", "case_b")
    ]
    assert [row["case_id"] for row in reference_rows] == expected_ids
    assert [row["case_id"] for row in prediction_rows] == expected_ids
    assert {row["split"] for row in reference_rows} == {"regression"}
    assert all("critical_regression" in row["slice_tags"] for row in reference_rows)
    assert reference_result["case_id_transform"] == "namespace_sha256_prefix40_v1"
    assert reference_result["reference_metadata_transform"] == {
        "split_override": "regression",
        "added_slice_tags": ["critical_regression"],
    }
    assert prediction_result["candidate"] == "final"
    assert (
        prediction_result["source"]["records_sha256"]
        == hashlib.sha256(prediction.records_path.read_bytes()).hexdigest()
    )


def test_scoped_package_rejects_invalid_namespace_and_candidate_state(
    tmp_path: Path,
) -> None:
    reference = _write_reference_part(
        tmp_path,
        "critical",
        [_reference("case_a", "challenge")],
    )
    with pytest.raises(EvaluationBundleError, match="namespace"):
        scope_evaluation_package(
            reference,
            namespace="Critical-Reg",
            prediction=False,
            destination=tmp_path / "invalid.references.jsonl",
            manifest_destination=tmp_path / "invalid.references.manifest.json",
        )
    with pytest.raises(EvaluationBundleError, match="must not declare"):
        scope_evaluation_package(
            reference,
            namespace="criticalreg",
            prediction=False,
            candidate="final",
            destination=tmp_path / "invalid.references.jsonl",
            manifest_destination=tmp_path / "invalid.references.manifest.json",
        )
    prediction = _write_prediction_part(
        tmp_path,
        "critical",
        ["case_a"],
        candidate="final",
    )
    with pytest.raises(EvaluationBundleError, match="cannot change"):
        scope_evaluation_package(
            prediction,
            namespace="criticalreg",
            prediction=True,
            candidate="final",
            split_override="regression",
            destination=tmp_path / "invalid.predictions.jsonl",
            manifest_destination=tmp_path / "invalid.predictions.manifest.json",
        )


def test_rejects_duplicates_candidate_mismatch_and_noncanonical_jsonl(
    tmp_path: Path,
) -> None:
    first = _write_reference_part(
        tmp_path,
        "duplicate_first",
        [_reference("case_a", "test")],
    )
    second = _write_reference_part(
        tmp_path,
        "duplicate_second",
        [_reference("case_a", "challenge")],
    )
    with pytest.raises(EvaluationBundleError, match="duplicate reference case_id"):
        combine_reference_packages(
            [first, second],
            destination=tmp_path / "duplicate.jsonl",
            manifest_destination=tmp_path / "duplicate.manifest.json",
        )

    _, _, references, reference_manifest = _reference_bundle(tmp_path)
    wrong_candidate = _write_prediction_part(
        tmp_path,
        "wrong_candidate",
        ["case_a", "case_b", "case_c"],
        candidate="final",
    )
    with pytest.raises(EvaluationBundleError, match="candidate 'final'.*'pilot'"):
        combine_prediction_packages(
            [wrong_candidate],
            candidate="pilot",
            references=references,
            reference_manifest=reference_manifest,
            destination=tmp_path / "wrong.predictions.jsonl",
            manifest_destination=tmp_path / "wrong.predictions.manifest.json",
        )

    noncanonical = _write_prediction_part(
        tmp_path,
        "noncanonical",
        ["case_a", "case_b", "case_c"],
    )
    parsed = [json.loads(line) for line in noncanonical.records_path.read_text(encoding="utf-8").splitlines()]
    noncanonical.records_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in parsed),
        encoding="utf-8",
    )
    manifest = json.loads(noncanonical.manifest_path.read_text(encoding="utf-8"))
    manifest["prediction_sha256"] = hashlib.sha256(noncanonical.records_path.read_bytes()).hexdigest()
    _write_json(noncanonical.manifest_path, manifest)
    with pytest.raises(EvaluationBundleError, match="noncanonical"):
        combine_prediction_packages(
            [noncanonical],
            candidate="pilot",
            references=references,
            reference_manifest=reference_manifest,
            destination=tmp_path / "noncanonical.bundle.jsonl",
            manifest_destination=tmp_path / "noncanonical.bundle.manifest.json",
        )


def test_rejects_blank_framing_and_reference_or_prediction_manifest_mismatch(
    tmp_path: Path,
) -> None:
    reference_part = _write_reference_part(
        tmp_path,
        "reference_hash",
        [_reference("case_a", "test")],
    )
    reference_manifest = json.loads(reference_part.manifest_path.read_text(encoding="utf-8"))
    reference_manifest["records_sha256"] = "0" * 64
    _write_json(reference_part.manifest_path, reference_manifest)
    with pytest.raises(EvaluationBundleError, match="hash differs from manifest"):
        combine_reference_packages(
            [reference_part],
            destination=tmp_path / "hash.jsonl",
            manifest_destination=tmp_path / "hash.manifest.json",
        )

    _, _, references, combined_reference_manifest = _reference_bundle(tmp_path)
    prediction_part = _write_prediction_part(
        tmp_path,
        "prediction_count",
        ["case_a", "case_b", "case_c"],
    )
    prediction_manifest = json.loads(prediction_part.manifest_path.read_text(encoding="utf-8"))
    prediction_manifest["record_count"] = 4
    _write_json(prediction_part.manifest_path, prediction_manifest)
    with pytest.raises(EvaluationBundleError, match="row count differs"):
        combine_prediction_packages(
            [prediction_part],
            candidate="pilot",
            references=references,
            reference_manifest=combined_reference_manifest,
            destination=tmp_path / "count.jsonl",
            manifest_destination=tmp_path / "count.manifest.json",
        )

    blank = _write_prediction_part(
        tmp_path,
        "blank",
        ["case_a", "case_b", "case_c"],
    )
    blank.records_path.write_bytes(blank.records_path.read_bytes() + b"\n")
    blank_manifest = json.loads(blank.manifest_path.read_text(encoding="utf-8"))
    blank_manifest["prediction_sha256"] = hashlib.sha256(blank.records_path.read_bytes()).hexdigest()
    _write_json(blank.manifest_path, blank_manifest)
    with pytest.raises(EvaluationBundleError, match="blank line"):
        combine_prediction_packages(
            [blank],
            candidate="pilot",
            references=references,
            reference_manifest=combined_reference_manifest,
            destination=tmp_path / "blank.jsonl",
            manifest_destination=tmp_path / "blank.manifest.json",
        )


def test_refuses_different_overwrite_and_cli_preserves_input_order(
    tmp_path: Path,
) -> None:
    first = _write_reference_part(
        tmp_path,
        "cli_first",
        [_reference("case_a", "test")],
    )
    second = _write_reference_part(
        tmp_path,
        "cli_second",
        [_reference("case_b", "challenge")],
    )
    output = tmp_path / "cli.references.jsonl"
    manifest = tmp_path / "cli.references.manifest.json"
    project_root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "combine_evaluation_packages.py"),
            "references",
            "--input",
            str(second.records_path),
            str(second.manifest_path),
            "--input",
            str(first.records_path),
            str(first.manifest_path),
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
    assert "evaluation_bundle=pass" in completed.stdout
    assert [row["case_id"] for row in load_evaluation_references(output)] == ["case_b", "case_a"]
    original_payload = output.read_bytes()
    with pytest.raises(EvaluationBundleError, match="overwrite different"):
        combine_reference_packages(
            [first, second],
            destination=output,
            manifest_destination=manifest,
        )
    assert output.read_bytes() == original_payload

    tampered = copy.deepcopy(json.loads(manifest.read_text(encoding="utf-8")))
    tampered["record_count"] += 1
    with pytest.raises(EvaluationBundleError, match="input counts differ"):
        validate_evaluation_bundle_manifest(tampered)

    reusable_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    reusable_manifest["case_ids_sha256"] = "0" * 64
    _write_json(manifest, reusable_manifest)
    with pytest.raises(EvaluationBundleError, match="case-ID hash differs"):
        combine_reference_packages(
            [EvaluationPackagePart(output, manifest)],
            destination=tmp_path / "recombined.jsonl",
            manifest_destination=tmp_path / "recombined.manifest.json",
        )

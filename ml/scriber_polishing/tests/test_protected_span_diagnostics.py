from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.evaluation_reference_builder import write_evaluation_references
from scriber_polishing.protected_span_diagnostics import (
    ProtectedSpanDiagnosticError,
    build_protected_span_diagnostic_report,
    validate_protected_span_diagnostic_report,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


class _Tokenizer:
    all_special_tokens: list[str] = []

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> list[int]:
        assert tokenize is True
        assert add_generation_prompt is True
        return list(range(len(messages[0]["content"].split()) + 2))

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return list(range(len(text)))

    def convert_ids_to_tokens(self, token_ids: list[int]) -> list[str]:
        return [str(value) for value in token_ids]

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens is True
        if token_ids == list(range(len("<unused0>"))):
            return "<unused0>"
        return "".join(str(value) for value in token_ids)

    def get_vocab(self) -> dict[str, int]:
        return {"<unused0>": 6, "<unused1>": 7}


def _canonical_jsonl(rows: list[dict[str, object]]) -> bytes:
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def _reference(case_id: str, source: str, target_sst: str) -> dict[str, object]:
    document = parse_sst(target_sst)
    protected_date = re.search(r"\d{2}\.\d{2}\.\d{4}", source)
    assert protected_date is not None
    protected_value = protected_date.group(0)
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": "test",
        "source_text": source,
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": render_plain_text(document),
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [protected_value],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Rückmeldung wird dokumentiert.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f3",
                    "order": 3,
                    "text": "Es wird keine weitere Aussage ergänzt.",
                    "polarity": "negative",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f4",
                    "order": 4,
                    "text": "Der Wortlaut bleibt unverändert.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": [],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Termin"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [protected_value],
        "slice_tags": ["domain:general_business", "profile:grammar"],
        "is_identity": False,
    }


def _fixture_files(tmp_path: Path) -> dict[str, Path]:
    references = [
        _reference(
            "repair_v1_01_family_100000_variant_01_pair_01",
            "Bitte am 31.07.2026 antworten.",
            "[DOC]\n[P]Bitte am 31.07.2026 antworten.[/P]\n[/DOC]",
        ),
        _reference(
            "repair_v1_01_family_100001_variant_01_pair_01",
            "Bitte am 01.08.2026 antworten.",
            "[DOC]\n[P]Bitte am 01.08.2026 antworten.[/P]\n[/DOC]",
        ),
    ]
    references_path = tmp_path / "references.jsonl"
    reference_manifest = write_evaluation_references(references, references_path)
    predictions = [
        {
            "schema_version": 1,
            "case_id": references[0]["case_id"],
            "prediction_sst": (
                "[DOC]\n[P]Bitte am SCRIBER_PROTECTED_0000 antworten.[/P]\n[/DOC]"
            ),
        },
        {
            "schema_version": 1,
            "case_id": references[1]["case_id"],
            "prediction_sst": (
                "[DOC]\n[P]Bitte am 01.08.2026 antworten.[/P]\n[/DOC]"
            ),
        },
    ]
    predictions_path = tmp_path / "predictions.jsonl"
    prediction_payload = _canonical_jsonl(predictions)
    predictions_path.write_bytes(prediction_payload)
    prediction_manifest_path = tmp_path / "prediction.manifest.json"
    prediction_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": "pilot",
                "record_count": 2,
                "valid_sst_count": 1,
                "restoration_error_count": 1,
                "prediction_sha256": hashlib.sha256(prediction_payload).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    evaluation_path = tmp_path / "evaluation.json"
    evaluation_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate": "pilot",
                "reference_sha256": reference_manifest["records_sha256"],
                "prediction_sha256": hashlib.sha256(prediction_payload).hexdigest(),
                "metrics": {
                    "case_results": [
                        {
                            "case_id": references[0]["case_id"],
                            "protected_spans_exact": False,
                            "sst_valid": True,
                        },
                        {
                            "case_id": references[1]["case_id"],
                            "protected_spans_exact": True,
                            "sst_valid": True,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    training_path = tmp_path / "train.jsonl"
    training_path.write_bytes(
        _canonical_jsonl(
            [
                {
                    "example_id": "repair_v1_01_family_000001_variant_01_pair_01",
                    "source_text": "Bitte am 02.08.2026 antworten.",
                    "target_sst": (
                        "[DOC]\n[P]Bitte am 02.08.2026 antworten.[/P]\n[/DOC]"
                    ),
                },
                {
                    "example_id": "repair_v1_01_family_000002_variant_01_pair_01",
                    "source_text": (
                        "Bitte am 03.08.2026 antworten und am 03.08.2026 erinnern."
                    ),
                    "target_sst": (
                        "[DOC]\n[P]Bitte am 03.08.2026 antworten und am "
                        "03.08.2026 erinnern.[/P]\n[/DOC]"
                    ),
                },
                {
                    "example_id": "repair_v1_01_family_000003_variant_01_pair_01",
                    "source_text": "Bitte am 04.08.2026 antworten.",
                    "target_sst": (
                        "[DOC]\n[P]Bitte am 04.08.2026 antworten und den "
                        "04.08.2026 vormerken.[/P]\n[/DOC]"
                    ),
                },
                {
                    "example_id": "repair_v1_01_family_000004_variant_01_pair_01",
                    "source_text": "Bitte am 05.08.2026 antworten.",
                    "target_sst": "[DOC]\n[P]Bitte morgen antworten.[/P]\n[/DOC]",
                },
            ]
        )
    )
    return {
        "references": references_path,
        "predictions": predictions_path,
        "prediction_manifest": prediction_manifest_path,
        "evaluation": evaluation_path,
        "training": training_path,
        "validation": training_path,
    }


def test_reconciles_manifest_and_classifies_malformed_marker(tmp_path: Path) -> None:
    paths = _fixture_files(tmp_path)
    report = build_protected_span_diagnostic_report(
        references_path=paths["references"],
        predictions_path=paths["predictions"],
        prediction_manifest_path=paths["prediction_manifest"],
        evaluation_report_path=paths["evaluation"],
        training_path=paths["training"],
        validation_path=paths["validation"],
        tokenizer=_Tokenizer(),
    )

    assert report["counts"] == {
        "evaluation_cases": 2,
        "cases_with_automatic_spans": 2,
        "protected_metric_failures": 1,
        "inferred_restoration_failures": 1,
        "non_restoration_protected_failures": 0,
        "manifest_restoration_errors": 1,
        "manifest_reconciled": True,
    }
    assert report["failure_modes"]["case_counts"] == {
        "malformed_delimiters": 1
    }
    assert report["failure_modes"]["span_counts"] == {
        "malformed_delimiters": 1
    }
    assert report["training_exposure"]["literal_marker_record_count"] == 0
    assert report["training_exposure"]["paired_placeholder_eligible_record_count"] == 2
    assert report["maskability_by_split"]["train"] == {
        "record_count": 4,
        "automatic_span_count": 5,
        "automatic_span_records": 4,
        "no_automatic_span_records": 0,
        "uniquely_maskable_records": 1,
        "ambiguous_records": 2,
        "ambiguous_repeated_value_records": 1,
        "ambiguous_extra_target_occurrence_records": 1,
        "unmaskable_records": 1,
        "exact_count_preserving_records": 2,
    }
    assert report["by_family"][0]["restoration_failure_rate"] == 0.5
    assert validate_protected_span_diagnostic_report(report) == report


def test_fails_closed_when_manifest_aggregate_does_not_reconcile(
    tmp_path: Path,
) -> None:
    paths = _fixture_files(tmp_path)
    manifest = json.loads(paths["prediction_manifest"].read_text(encoding="utf-8"))
    manifest["restoration_error_count"] = 0
    paths["prediction_manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        ProtectedSpanDiagnosticError,
        match="do not reconcile",
    ):
        build_protected_span_diagnostic_report(
            references_path=paths["references"],
            predictions_path=paths["predictions"],
            prediction_manifest_path=paths["prediction_manifest"],
            evaluation_report_path=paths["evaluation"],
            training_path=paths["training"],
            validation_path=paths["validation"],
            tokenizer=_Tokenizer(),
        )


def test_rejects_prediction_hash_mismatch(tmp_path: Path) -> None:
    paths = _fixture_files(tmp_path)
    paths["predictions"].write_bytes(b" " + paths["predictions"].read_bytes())

    with pytest.raises(ProtectedSpanDiagnosticError, match="hash mismatch"):
        build_protected_span_diagnostic_report(
            references_path=paths["references"],
            predictions_path=paths["predictions"],
            prediction_manifest_path=paths["prediction_manifest"],
            evaluation_report_path=paths["evaluation"],
            training_path=paths["training"],
            validation_path=paths["validation"],
            tokenizer=_Tokenizer(),
        )

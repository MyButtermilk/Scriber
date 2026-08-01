"""Strict JSONL I/O for immutable references and exact candidate joins."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_from_dict
from .metrics import EvaluationCase
from .schemas import validate_semantic_plan
from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import render_plain_text

_PROJECT_ROOT = Path(__file__).parents[2]
_REFERENCE_SCHEMA = _PROJECT_ROOT / "contracts" / "evaluation_reference_schema.json"
_PREDICTION_SCHEMA = _PROJECT_ROOT / "contracts" / "evaluation_prediction_schema.json"


@cache
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _jsonl_objects(path: Path, label: str) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank JSONL line in {path} at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON in {path} at {line_number}: {error.msg}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} JSONL row must be an object in {path} at {line_number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} JSONL file is empty: {path}")
    return tuple(rows)


def _schema_record(record: Mapping[str, Any], schema: Path, label: str) -> dict[str, Any]:
    errors = sorted(_validator(schema).iter_errors(dict(record)), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"{label} schema failed: {errors[0].message}")
    return dict(record)


def validate_evaluation_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed reference schema and its AST/SST/plain-text equivalence."""

    validated = _schema_record(record, _REFERENCE_SCHEMA, "reference")
    try:
        parse_sst(validated["target_sst"])
    except (SSTParseError, ValueError) as error:
        raise ValueError(f"reference target_sst is invalid: {error}") from error
    try:
        ast_document = document_from_dict(validated["target_ast"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"reference target_ast is invalid: {error}") from error
    if render_sst(ast_document) != validated["target_sst"]:
        raise ValueError("reference target_ast differs from target_sst")
    if render_plain_text(ast_document) != validated["target_plain_text"]:
        raise ValueError("reference target_plain_text differs from target_ast")
    if validated["is_identity"] and validated["source_text"] != validated["target_plain_text"]:
        raise ValueError("identity reference source_text must exactly equal target_plain_text")
    try:
        validate_semantic_plan(validated["semantic_plan"])
    except ValueError as error:
        raise ValueError(f"reference semantic_plan is invalid: {error}") from error
    for protected in validated["protected_spans"]:
        if protected not in validated["target_plain_text"]:
            raise ValueError(f"reference protected span is absent from target_plain_text: {protected!r}")
    return validated


def validate_candidate_prediction(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate candidate transport fields without requiring valid SST output."""

    return _schema_record(record, _PREDICTION_SCHEMA, "candidate prediction")


def _unique_by_case_id(records: Sequence[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        case_id = str(record["case_id"])
        if case_id in indexed:
            raise ValueError(f"duplicate {label} case_id: {case_id}")
        indexed[case_id] = record
    return indexed


def load_evaluation_references(path: Path) -> tuple[dict[str, Any], ...]:
    rows = tuple(validate_evaluation_reference(row) for row in _jsonl_objects(path, "reference"))
    _unique_by_case_id(rows, "reference")
    return rows


def load_candidate_predictions(path: Path) -> tuple[dict[str, Any], ...]:
    rows = tuple(validate_candidate_prediction(row) for row in _jsonl_objects(path, "candidate prediction"))
    _unique_by_case_id(rows, "candidate prediction")
    return rows


def join_evaluation_cases(
    references: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> tuple[EvaluationCase, ...]:
    """Require one and only one prediction for every frozen reference."""

    validated_references = tuple(validate_evaluation_reference(record) for record in references)
    validated_predictions = tuple(validate_candidate_prediction(record) for record in predictions)
    references_by_id = _unique_by_case_id(validated_references, "reference")
    predictions_by_id = _unique_by_case_id(validated_predictions, "candidate prediction")
    missing = sorted(set(references_by_id).difference(predictions_by_id))
    if missing:
        raise ValueError(f"missing candidate prediction for reference: {missing[0]}")
    extra = sorted(set(predictions_by_id).difference(references_by_id))
    if extra:
        raise ValueError(f"candidate prediction has no frozen reference: {extra[0]}")

    cases: list[EvaluationCase] = []
    for reference in validated_references:
        prediction = predictions_by_id[reference["case_id"]]
        slice_tags = tuple(dict.fromkeys([*reference["slice_tags"], f"split:{reference['split']}"]))
        cases.append(
            EvaluationCase(
                case_id=reference["case_id"],
                source_text=reference["source_text"],
                target_sst=reference["target_sst"],
                prediction_sst=prediction["prediction_sst"],
                protected_values=tuple(reference["protected_spans"]),
                target_plain_text=reference["target_plain_text"],
                target_ast=reference["target_ast"],
                slice_tags=slice_tags,
                is_identity=reference["is_identity"],
            )
        )
    return tuple(cases)

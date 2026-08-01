"""Hash-bound diagnostics for protected-placeholder generation failures."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from statistics import mean
from typing import Any

from jsonschema import Draft202012Validator

from .evaluation_io import (
    load_candidate_predictions,
    load_evaluation_references,
)
from .protected_spans import ProtectedSpan, protect_spans
from .tokenize import POLISHING_INSTRUCTION

_PROJECT_ROOT = Path(__file__).parents[2]
_REPORT_SCHEMA = _PROJECT_ROOT / "contracts" / "protected_span_diagnostic_report_schema.json"
_MARKER_CORE = re.compile(r"SCRIBER_PROTECTED_(\d{4})")
_VERSIONED_CASE_PREFIX = re.compile(r"^[^_]+_v\d+_\d+_")
_SEED_SUFFIX = re.compile(r"_\d{6}_variant_\d+_pair_\d+$")


class ProtectedSpanDiagnosticError(ValueError):
    """Diagnostic inputs or their cross-file bindings were inconsistent."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stable_bytes(path: str | Path, label: str) -> tuple[Path, bytes]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ProtectedSpanDiagnosticError(f"{label} must not be a symlink")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ProtectedSpanDiagnosticError(f"{label} must be a regular file")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ProtectedSpanDiagnosticError(f"{label} changed while it was read")
    return resolved, payload


def _load_json(path: str | Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    resolved, payload = _stable_bytes(path, label)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtectedSpanDiagnosticError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ProtectedSpanDiagnosticError(f"{label} must contain a JSON object")
    return resolved, payload, value


def _load_training(path: str | Path) -> tuple[Path, bytes, tuple[dict[str, Any], ...]]:
    resolved, payload = _stable_bytes(path, "training data")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtectedSpanDiagnosticError("training data is not UTF-8") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProtectedSpanDiagnosticError(
                f"training data line {line_number} is invalid JSON"
            ) from error
        if not isinstance(row, dict):
            raise ProtectedSpanDiagnosticError(
                f"training data line {line_number} must be an object"
            )
        for field in ("example_id", "source_text", "target_sst"):
            if not isinstance(row.get(field), str):
                raise ProtectedSpanDiagnosticError(
                    f"training data line {line_number} requires text field {field}"
                )
        rows.append(row)
    if not rows:
        raise ProtectedSpanDiagnosticError("training data is empty")
    return resolved, payload, tuple(rows)


def _family(case_id: str) -> str:
    normalized = re.sub(r"_+", "_", case_id)
    stem = _SEED_SUFFIX.sub("", normalized)
    stripped = _VERSIONED_CASE_PREFIX.sub("", stem)
    return stripped or stem


def _domain(reference: Mapping[str, Any]) -> str:
    for tag in reference.get("slice_tags", ()):
        if isinstance(tag, str) and tag.startswith("domain:"):
            return tag.removeprefix("domain:")
    return "unknown"


def _profile(reference: Mapping[str, Any]) -> str:
    for tag in reference.get("slice_tags", ()):
        if isinstance(tag, str) and tag.startswith("profile:"):
            return tag.removeprefix("profile:")
    return "unknown"


def _span_category(value: str) -> str:
    checks = (
        ("url", r"https?://\S+"),
        ("email", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("ecli", r"ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"),
        ("legal_case_number", r"Az\.\s*.+"),
        ("legal_citation", r"§§?\s*.+"),
        ("date", r"\d{1,2}\.\d{1,2}\.\d{4}"),
        ("time", r"\d{1,2}:\d{2}\s*Uhr"),
        (
            "unit",
            r"\d+(?:[.,]\d+)?\s*(?:kWh/\(m²·a\)|€/m²|m²|m³|kWh|kW|MWh|MW|km/h|m/s)",
        ),
        ("currency_eur", r"\d{1,3}(?:\.\d{3})*,\d{2}\s*€"),
        ("percentage", r"\d+(?:,\d+)?\s*%"),
        ("model_name", r"Modell\s+[A-Za-z]+\d+"),
        ("speaker_label", r"(?:Sprecher(?:in)?|Speaker)\s*:"),
        ("timestamp", r"\[\d{2}:\d{2}:\d{2}\]"),
    )
    for category, pattern in checks:
        if re.fullmatch(pattern, value):
            return category
    return "unknown"


def _distribution(values: Sequence[int]) -> dict[str, int | float | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "p50": None,
            "p95": None,
            "maximum": None,
            "mean": None,
        }
    ordered = sorted(values)

    def percentile(fraction: float) -> int:
        index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction) + 0.999999) - 1))
        return ordered[index]

    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "maximum": ordered[-1],
        "mean": mean(ordered),
    }


def _prompt_token_ids(tokenizer: Any, text: str) -> list[int]:
    messages = [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{text}"}]
    token_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
    )
    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()
    if token_ids and isinstance(token_ids[0], list):
        token_ids = token_ids[0]
    return [int(token_id) for token_id in token_ids]


def _marker_tokens(tokenizer: Any) -> tuple[list[int], list[str]]:
    marker = "⟦SCRIBER_PROTECTED_0000⟧"
    token_ids = tokenizer.encode(marker, add_special_tokens=False)
    return (
        [int(token_id) for token_id in token_ids],
        [str(token) for token in tokenizer.convert_ids_to_tokens(token_ids)],
    )


def _metric_case_map(evaluation_report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    metrics = evaluation_report.get("metrics")
    rows = metrics.get("case_results") if isinstance(metrics, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise ProtectedSpanDiagnosticError(
            "evaluation report requires non-empty metrics.case_results"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("case_id"), str):
            raise ProtectedSpanDiagnosticError("evaluation case result is malformed")
        case_id = str(row["case_id"])
        if case_id in result:
            raise ProtectedSpanDiagnosticError(f"duplicate evaluation case_id: {case_id}")
        if not isinstance(row.get("protected_spans_exact"), bool):
            raise ProtectedSpanDiagnosticError(
                f"evaluation case {case_id} lacks protected_spans_exact"
            )
        result[case_id] = row
    return result


def _index_diagnostics(
    spans: Sequence[ProtectedSpan],
    prediction: str,
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    core_sequence = _MARKER_CORE.findall(prediction)
    core_counts = Counter(core_sequence)
    expected_indices = [f"{index:04d}" for index in range(len(spans))]
    expected_set = set(expected_indices)
    span_rows: list[dict[str, Any]] = []
    modes: set[str] = set()
    for index, span in enumerate(spans):
        core = expected_indices[index]
        exact_count = prediction.count(span.placeholder)
        core_count = core_counts[core]
        if exact_count > 1 or core_count > 1:
            status = "duplicated"
            modes.add("duplicated")
        elif exact_count == 1:
            status = "exact"
        elif core_count:
            status = "malformed_delimiters"
            modes.add("malformed_delimiters")
        else:
            status = "deleted"
            modes.add("deleted")
        span_rows.append(
            {
                "index": index,
                "category": _span_category(span.value),
                "value_sha256": _sha256(span.value.encode("utf-8")),
                "exact_placeholder_count": exact_count,
                "marker_core_count": core_count,
                "status": status,
            }
        )
    filtered_sequence = [index for index in core_sequence if index in expected_set]
    if (
        len(filtered_sequence) == len(expected_indices)
        and Counter(filtered_sequence) == Counter(expected_indices)
        and filtered_sequence != expected_indices
    ):
        modes.add("reordered")
    unknown = sorted(index for index in core_counts if index not in expected_set)
    if unknown:
        modes.add("unknown_index")
    return span_rows, modes, unknown


def _training_exposure(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    family_counts: dict[str, Counter[str]] = defaultdict(Counter)
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    category_records: dict[str, set[str]] = defaultdict(set)
    records_with_spans = 0
    span_count = 0
    eligible = 0
    literal_marker_records = 0
    maximum_spans = 0
    for row in rows:
        source = str(row["source_text"])
        target = str(row["target_sst"])
        family = _family(str(row["example_id"]))
        protected = protect_spans(source)
        family_counts[family]["records"] += 1
        if "SCRIBER_PROTECTED" in source or "SCRIBER_PROTECTED" in target:
            literal_marker_records += 1
        if not protected.spans:
            continue
        records_with_spans += 1
        maximum_spans = max(maximum_spans, len(protected.spans))
        family_counts[family]["records_with_automatic_spans"] += 1
        family_counts[family]["automatic_span_count"] += len(protected.spans)
        span_count += len(protected.spans)
        source_value_counts = Counter(span.value for span in protected.spans)
        if all(target.count(value) == count for value, count in source_value_counts.items()):
            eligible += 1
        for span in protected.spans:
            category = _span_category(span.value)
            category_counts[category]["automatic_span_count"] += 1
            category_records[category].add(str(row["example_id"]))
    normalized_categories = {
        category: {
            "records_with_category": len(category_records[category]),
            "automatic_span_count": int(counts["automatic_span_count"]),
        }
        for category, counts in category_counts.items()
    }
    return (
        {
            "record_count": len(rows),
            "records_with_automatic_spans": records_with_spans,
            "automatic_span_count": span_count,
            "literal_marker_record_count": literal_marker_records,
            "paired_placeholder_eligible_record_count": eligible,
            "maximum_automatic_spans_per_record": maximum_spans,
        },
        {
            family: {key: int(value) for key, value in counts.items()}
            for family, counts in family_counts.items()
        },
        normalized_categories,
    )


def _maskability(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    counts["record_count"] = len(rows)
    for row in rows:
        protected = protect_spans(str(row["source_text"]))
        if not protected.spans:
            counts["no_automatic_span_records"] += 1
            continue
        counts["automatic_span_records"] += 1
        counts["automatic_span_count"] += len(protected.spans)
        source_counts = Counter(span.value for span in protected.spans)
        target = str(row["target_sst"])
        target_counts = {
            value: target.count(value)
            for value in source_counts
        }
        if any(target_counts[value] < occurrences for value, occurrences in source_counts.items()):
            counts["unmaskable_records"] += 1
            continue
        if any(target_counts[value] > occurrences for value, occurrences in source_counts.items()):
            counts["ambiguous_extra_target_occurrence_records"] += 1
            counts["ambiguous_records"] += 1
            continue
        if any(occurrences > 1 for occurrences in source_counts.values()):
            counts["ambiguous_repeated_value_records"] += 1
            counts["ambiguous_records"] += 1
            counts["exact_count_preserving_records"] += 1
            continue
        counts["uniquely_maskable_records"] += 1
        counts["exact_count_preserving_records"] += 1
    for key in (
        "automatic_span_count",
        "automatic_span_records",
        "no_automatic_span_records",
        "uniquely_maskable_records",
        "ambiguous_records",
        "ambiguous_repeated_value_records",
        "ambiguous_extra_target_occurrence_records",
        "unmaskable_records",
        "exact_count_preserving_records",
    ):
        counts[key] += 0
    return dict(counts)


def _unused_token_probe(
    tokenizer: Any,
    datasets: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    vocabulary = tokenizer.get_vocab()
    numeric_unused = sorted(
        int(token[7:-1])
        for token in vocabulary
        if token.startswith("<unused")
        and token.endswith(">")
        and token[7:-1].isdigit()
    )
    contiguous = 0
    for expected, actual in enumerate(numeric_unused):
        if expected != actual:
            break
        contiguous += 1
    probe = "<unused0>"
    probe_ids = [int(value) for value in tokenizer.encode(probe, add_special_tokens=False)]
    collisions = sum(
        probe in str(row["source_text"]) or probe in str(row["target_sst"])
        for rows in datasets
        for row in rows
    )
    return {
        "probe": probe,
        "token_ids": probe_ids,
        "token_count": len(probe_ids),
        "decoded_skip_special_tokens": str(
            tokenizer.decode(probe_ids, skip_special_tokens=True)
        ),
        "is_special_token": probe in set(tokenizer.all_special_tokens),
        "numeric_unused_token_count": len(numeric_unused),
        "contiguous_numeric_unused_from_zero": contiguous,
        "literal_probe_collision_records": collisions,
    }


def _rate_rows(
    denominator: Mapping[str, int],
    numerator: Mapping[str, int],
    training: Mapping[str, Mapping[str, int]],
    *,
    key_name: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(set(denominator) | set(numerator) | set(training)):
        eligible = int(denominator.get(key, 0))
        failures = int(numerator.get(key, 0))
        row = {
            key_name: key,
            "reference_cases_with_automatic_spans": eligible,
            "restoration_failure_cases": failures,
            "restoration_failure_rate": failures / eligible if eligible else None,
        }
        row.update(training.get(key, {}))
        rows.append(row)
    return rows


@cache
def _report_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(_REPORT_SCHEMA.read_text(encoding="utf-8"))
    )


def validate_protected_span_diagnostic_report(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the closed diagnostic report schema."""

    materialized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    errors = sorted(
        _report_validator().iter_errors(materialized),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ProtectedSpanDiagnosticError(
            f"diagnostic report schema failed: {errors[0].message}"
        )
    return materialized


def build_protected_span_diagnostic_report(
    *,
    references_path: str | Path,
    predictions_path: str | Path,
    prediction_manifest_path: str | Path,
    evaluation_report_path: str | Path,
    training_path: str | Path,
    validation_path: str | Path,
    tokenizer: Any,
) -> dict[str, Any]:
    """Reconcile restoration errors and aggregate their reproducible causes."""

    reference_file, reference_payload = _stable_bytes(references_path, "references")
    prediction_file, prediction_payload = _stable_bytes(predictions_path, "predictions")
    references = load_evaluation_references(reference_file)
    predictions = load_candidate_predictions(prediction_file)
    manifest_file, manifest_payload, manifest = _load_json(
        prediction_manifest_path,
        "prediction manifest",
    )
    evaluation_file, evaluation_payload, evaluation = _load_json(
        evaluation_report_path,
        "evaluation report",
    )
    training_file, training_payload, training_rows = _load_training(training_path)
    validation_file, validation_payload, validation_rows = _load_training(
        validation_path
    )

    prediction_hash = _sha256(prediction_payload)
    reference_hash = _sha256(reference_payload)
    if manifest.get("prediction_sha256") != prediction_hash:
        raise ProtectedSpanDiagnosticError("prediction manifest hash mismatch")
    if manifest.get("record_count") != len(predictions):
        raise ProtectedSpanDiagnosticError("prediction manifest count mismatch")
    if evaluation.get("prediction_sha256") != prediction_hash:
        raise ProtectedSpanDiagnosticError("evaluation report prediction hash mismatch")
    if evaluation.get("reference_sha256") != reference_hash:
        raise ProtectedSpanDiagnosticError("evaluation report reference hash mismatch")

    reference_map = {row["case_id"]: row for row in references}
    prediction_map = {row["case_id"]: row for row in predictions}
    metric_map = _metric_case_map(evaluation)
    expected_ids = set(reference_map)
    if set(prediction_map) != expected_ids or set(metric_map) != expected_ids:
        raise ProtectedSpanDiagnosticError("case coverage differs across inputs")

    training_summary, training_families, training_categories = _training_exposure(
        training_rows
    )
    family_denominator: Counter[str] = Counter()
    family_failures: Counter[str] = Counter()
    category_denominator: Counter[str] = Counter()
    category_failures: Counter[str] = Counter()
    prompt_tokens_all: list[int] = []
    prompt_tokens_failed: list[int] = []
    raw_prompt_tokens_failed: list[int] = []
    metric_failures = 0
    inferred_restoration_failures = 0
    non_restoration_failures = 0
    failure_case_modes: Counter[str] = Counter()
    failure_span_modes: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []

    for case_id in sorted(expected_ids):
        reference = reference_map[case_id]
        prediction = str(prediction_map[case_id]["prediction_sst"])
        metric = metric_map[case_id]
        protected = protect_spans(str(reference["source_text"]))
        family = _family(case_id)
        categories = sorted({_span_category(span.value) for span in protected.spans})
        if protected.spans:
            family_denominator[family] += 1
            for category in categories:
                category_denominator[category] += 1
            prompt_tokens_all.append(len(_prompt_token_ids(tokenizer, protected.text)))
        if bool(metric["protected_spans_exact"]):
            continue
        metric_failures += 1
        if not protected.spans:
            non_restoration_failures += 1
            continue

        inferred_restoration_failures += 1
        family_failures[family] += 1
        for category in categories:
            category_failures[category] += 1
        protected_tokens = len(_prompt_token_ids(tokenizer, protected.text))
        raw_tokens = len(_prompt_token_ids(tokenizer, str(reference["source_text"])))
        prompt_tokens_failed.append(protected_tokens)
        raw_prompt_tokens_failed.append(raw_tokens)
        span_rows, modes, unknown = _index_diagnostics(
            protected.spans,
            prediction,
        )
        for span_row in span_rows:
            failure_span_modes[span_row["status"]] += 1
        for mode in modes:
            failure_case_modes[mode] += 1
        if not bool(metric.get("sst_valid")):
            modes.add("invalid_sst")
            failure_case_modes["invalid_sst"] += 1
        if not prediction.rstrip().endswith("[/DOC]"):
            modes.add("truncated_output")
            failure_case_modes["truncated_output"] += 1
        cases.append(
            {
                "case_id": case_id,
                "family": family,
                "domain": _domain(reference),
                "profile": _profile(reference),
                "source_characters": len(str(reference["source_text"])),
                "raw_prompt_tokens": raw_tokens,
                "protected_prompt_tokens": protected_tokens,
                "expected_placeholder_count": len(protected.spans),
                "failure_modes": sorted(modes),
                "unknown_marker_indices": unknown,
                "spans": span_rows,
            }
        )

    manifest_restoration_errors = manifest.get("restoration_error_count")
    if (
        isinstance(manifest_restoration_errors, bool)
        or not isinstance(manifest_restoration_errors, int)
        or manifest_restoration_errors < 0
    ):
        raise ProtectedSpanDiagnosticError(
            "prediction manifest requires restoration_error_count"
        )
    reconciled = manifest_restoration_errors == inferred_restoration_failures
    if not reconciled:
        raise ProtectedSpanDiagnosticError(
            "inferred restoration failures do not reconcile with prediction manifest"
        )

    marker_ids, marker_tokens = _marker_tokens(tokenizer)
    family_rows = _rate_rows(
        family_denominator,
        family_failures,
        training_families,
        key_name="family",
    )
    category_rows = _rate_rows(
        category_denominator,
        category_failures,
        training_categories,
        key_name="category",
    )
    report = {
        "schema_version": 1,
        "kind": "scriber_protected_span_diagnostic",
        "candidate": str(manifest.get("candidate", evaluation.get("candidate", "unknown"))),
        "inputs": {
            "references": {
                "filename": reference_file.name,
                "sha256": reference_hash,
                "record_count": len(references),
            },
            "predictions": {
                "filename": prediction_file.name,
                "sha256": prediction_hash,
                "record_count": len(predictions),
            },
            "prediction_manifest": {
                "filename": manifest_file.name,
                "sha256": _sha256(manifest_payload),
            },
            "evaluation_report": {
                "filename": evaluation_file.name,
                "sha256": _sha256(evaluation_payload),
            },
            "training_data": {
                "filename": training_file.name,
                "sha256": _sha256(training_payload),
                "record_count": len(training_rows),
            },
            "validation_data": {
                "filename": validation_file.name,
                "sha256": _sha256(validation_payload),
                "record_count": len(validation_rows),
            },
        },
        "counts": {
            "evaluation_cases": len(references),
            "cases_with_automatic_spans": sum(family_denominator.values()),
            "protected_metric_failures": metric_failures,
            "inferred_restoration_failures": inferred_restoration_failures,
            "non_restoration_protected_failures": non_restoration_failures,
            "manifest_restoration_errors": manifest_restoration_errors,
            "manifest_reconciled": reconciled,
        },
        "failure_modes": {
            "case_counts": dict(sorted(failure_case_modes.items())),
            "span_counts": dict(sorted(failure_span_modes.items())),
        },
        "lengths": {
            "protected_prompt_tokens_all_automatic_cases": _distribution(
                prompt_tokens_all
            ),
            "protected_prompt_tokens_restoration_failures": _distribution(
                prompt_tokens_failed
            ),
            "raw_prompt_tokens_restoration_failures": _distribution(
                raw_prompt_tokens_failed
            ),
            "placeholder_token_delta_restoration_failures": _distribution(
                [
                    protected - raw
                    for protected, raw in zip(
                        prompt_tokens_failed,
                        raw_prompt_tokens_failed,
                        strict=True,
                    )
                ]
            ),
        },
        "tokenization": {
            "canonical_marker": "⟦SCRIBER_PROTECTED_0000⟧",
            "canonical_marker_token_count": len(marker_ids),
            "canonical_marker_token_ids": marker_ids,
            "canonical_marker_tokens": marker_tokens,
            "unused_token_probe": _unused_token_probe(
                tokenizer,
                (training_rows, validation_rows),
            ),
        },
        "training_exposure": training_summary,
        "maskability_by_split": {
            "train": _maskability(training_rows),
            "validation": _maskability(validation_rows),
        },
        "by_family": family_rows,
        "by_span_category": category_rows,
        "cases": cases,
        "limitations": [
            "Successful predictions contain restored values, so marker order before restoration is not retained.",
            "Restoration failures are assigned by exact reconciliation of automatic-span metric failures with the manifest aggregate.",
            "The protected-span metric also covers explicit dataset values that are not automatic inference placeholders.",
        ],
    }
    return validate_protected_span_diagnostic_report(report)


def write_protected_span_diagnostic_report(
    report: Mapping[str, Any],
    destination: str | Path,
) -> None:
    """Write a deterministic report without replacing different evidence."""

    validated = validate_protected_span_diagnostic_report(report)
    payload = (
        json.dumps(
            validated,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path = Path(destination).resolve()
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ProtectedSpanDiagnosticError(
                f"refusing to overwrite different diagnostic report: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists():
        raise ProtectedSpanDiagnosticError(
            f"stale temporary diagnostic report: {temporary}"
        )
    temporary.write_bytes(payload)
    temporary.replace(path)


__all__ = [
    "ProtectedSpanDiagnosticError",
    "build_protected_span_diagnostic_report",
    "validate_protected_span_diagnostic_report",
    "write_protected_span_diagnostic_report",
]

"""Hash-bound, non-holdout regression suite for fresh private Hub reloads."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .sst_parser import SSTParseError, parse_sst
from .sst_renderer import render_sst
from .target_renderer import (
    render_html,
    render_html_clipboard,
    render_markdown,
    render_plain_text,
)

_SCHEMA = Path(__file__).parents[2] / "contracts" / "hub_regression_case_schema.json"
_RENDERERS: dict[str, Callable[[Any], str]] = {
    "sst": render_sst,
    "plain_text": render_plain_text,
    "markdown": render_markdown,
    "html": render_html,
    "html_clipboard": render_html_clipboard,
}


class HubRegressionError(RuntimeError):
    """The fresh-reload regression suite or one model result is unsafe."""


def _canonical_line(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA.read_text(encoding="utf-8")))


def _policy(value: Mapping[str, object]) -> dict[str, object]:
    expected = {
        "file",
        "minimum_cases",
        "ai_gold_smoke_minimum",
        "required_categories",
        "max_new_tokens",
    }
    if set(value) != expected:
        raise HubRegressionError("Hub regression policy must use the closed v1 field set")
    path = value.get("file")
    minimum = value.get("minimum_cases")
    ai_gold_minimum = value.get("ai_gold_smoke_minimum")
    categories = value.get("required_categories")
    max_new_tokens = value.get("max_new_tokens")
    if path != "examples/regression_sample.jsonl":
        raise HubRegressionError("Hub regression suite path is fixed")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 100:
        raise HubRegressionError("Hub regression policy requires at least 100 cases")
    if (
        isinstance(ai_gold_minimum, bool)
        or not isinstance(ai_gold_minimum, int)
        or ai_gold_minimum < 1
        or ai_gold_minimum > minimum
    ):
        raise HubRegressionError("Hub regression policy has an invalid AI-Gold smoke minimum")
    if (
        not isinstance(categories, list)
        or len(categories) < 10
        or len(categories) != len(set(categories))
        or "ai_gold_smoke" not in categories
        or not all(isinstance(item, str) and item for item in categories)
    ):
        raise HubRegressionError("Hub regression policy lacks the mandatory category coverage")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 128:
        raise HubRegressionError("Hub regression max_new_tokens is invalid")
    return {
        "file": path,
        "minimum_cases": minimum,
        "ai_gold_smoke_minimum": ai_gold_minimum,
        "required_categories": list(categories),
        "max_new_tokens": max_new_tokens,
    }


def validate_hub_regression_policy(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Validate and copy the closed >=100-case Hub regression policy."""

    return _policy(value)


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise HubRegressionError(f"{label} must be a regular JSONL file")
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.rstrip("\r\n")
            if not line:
                raise HubRegressionError(f"{label} has a blank line at {line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise HubRegressionError(f"{label} has invalid JSON at {line_number}") from error
            if not isinstance(record, dict):
                raise HubRegressionError(f"{label} record {line_number} is not an object")
            records.append(record)
    if not records:
        raise HubRegressionError(f"{label} is empty")
    return records


def _rendered_hashes(prediction_sst: str) -> tuple[dict[str, str], str]:
    try:
        document = parse_sst(prediction_sst)
    except (SSTParseError, ValueError) as error:
        raise HubRegressionError("Hub regression prediction is not valid SST") from error
    rendered = {name: renderer(document) for name, renderer in _RENDERERS.items()}
    if rendered["sst"] != prediction_sst:
        raise HubRegressionError("Hub regression prediction is not canonical SST")
    html = rendered["html"].casefold()
    if "<script" in html or "javascript:" in html or " onerror=" in html or " onclick=" in html:
        raise HubRegressionError("Hub regression HTML renderer produced unsafe markup")
    return {name: _sha256_text(text) for name, text in rendered.items()}, rendered["plain_text"]


def _suite_evidence(
    path: Path,
    cases: list[dict[str, Any]],
    policy: Mapping[str, object],
) -> dict[str, object]:
    validated_policy = _policy(policy)
    counts = Counter(str(case["category"]) for case in cases)
    required = list(validated_policy["required_categories"])
    missing = [category for category in required if counts[category] <= 0]
    if len(cases) < int(validated_policy["minimum_cases"]):
        raise HubRegressionError("Hub regression suite has fewer than 100 required cases")
    if missing:
        raise HubRegressionError(f"Hub regression suite is missing required category: {missing[0]}")
    ai_gold_count = sum(case["source_kind"] == "ai_gold_smoke" for case in cases)
    if ai_gold_count < int(validated_policy["ai_gold_smoke_minimum"]):
        raise HubRegressionError("Hub regression suite has too few AI-Gold smoke cases")
    return {
        "file": str(validated_policy["file"]),
        "cases_sha256": _sha256_bytes(path.read_bytes()),
        "case_count": len(cases),
        "minimum_case_count": int(validated_policy["minimum_cases"]),
        "category_counts": {category: counts[category] for category in sorted(counts)},
        "source_partition_counts": dict(
            sorted(Counter(str(case["source_partition"]) for case in cases).items())
        ),
        "required_categories": required,
        "ai_gold_smoke_count": ai_gold_count,
        "ai_gold_smoke_minimum": int(validated_policy["ai_gold_smoke_minimum"]),
        "private_data": False,
        "holdout_data": False,
    }


def load_hub_regression_suite(
    path: str | Path,
    policy: Mapping[str, object],
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    """Validate one immutable >=100-case non-holdout suite and return its binding."""

    suite = Path(path).resolve()
    records = _load_jsonl(suite, "Hub regression suite")
    case_ids: set[str] = set()
    cases: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        errors = sorted(
            _validator().iter_errors(record),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
        if errors:
            location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
            message = errors[0].message
            if record.get("holdout") is not False:
                message = "holdout data is forbidden"
            raise HubRegressionError(
                f"Hub regression case {index} failed schema at {location}: {message}"
            )
        if record["case_id"] in case_ids:
            raise HubRegressionError("Hub regression suite contains duplicate case IDs")
        if (
            record["source_kind"] == "ai_gold_smoke"
        ) != (record["category"] == "ai_gold_smoke"):
            raise HubRegressionError("AI-Gold smoke identity differs from its category")
        if record["source_kind"] == "ai_gold_smoke":
            if record["source_partition"] not in {
                "ai_gold_train",
                "ai_gold_validation",
            }:
                raise HubRegressionError(
                    "AI-Gold smoke case is not from a non-holdout partition"
                )
        elif record["source_partition"] != "synthetic_non_holdout":
            raise HubRegressionError(
                "synthetic regression case has an unsafe source partition"
            )
        case_ids.add(record["case_id"])
        cases.append(record)
    return cases, _suite_evidence(suite, cases, policy)


def materialize_hub_regression_suite(
    sources_path: str | Path,
    predictions_path: str | Path,
    output_path: str | Path,
    *,
    policy: Mapping[str, object],
) -> dict[str, object]:
    """Bind deterministic local model predictions to safe regression inputs."""

    output = Path(output_path).resolve()
    if output.exists():
        raise HubRegressionError("refusing to overwrite an immutable Hub regression suite")
    sources = _load_jsonl(Path(sources_path).resolve(), "Hub regression sources")
    predictions = _load_jsonl(
        Path(predictions_path).resolve(),
        "Hub regression predictions",
    )
    prediction_by_id: dict[str, str] = {}
    for record in predictions:
        fields = set(record)
        if fields not in (
            {"case_id", "prediction_sst"},
            {"schema_version", "case_id", "prediction_sst"},
        ) or ("schema_version" in record and record["schema_version"] != 1):
            raise HubRegressionError("Hub regression prediction has an unexpected shape")
        case_id = record.get("case_id")
        prediction = record.get("prediction_sst")
        if (
            not isinstance(case_id, str)
            or not isinstance(prediction, str)
            or not prediction
            or case_id in prediction_by_id
        ):
            raise HubRegressionError("Hub regression prediction identity is invalid")
        prediction_by_id[case_id] = prediction
    materialized: list[dict[str, Any]] = []
    for source in sources:
        expected_source_fields = {
            "schema_version",
            "case_id",
            "source_kind",
            "source_partition",
            "category",
            "source_text",
            "protected_spans",
            "private_data",
            "holdout",
        }
        if set(source) != expected_source_fields:
            raise HubRegressionError("Hub regression source has an unexpected shape")
        if source.get("private_data") is not False:
            raise HubRegressionError("Hub regression sources may not contain private data")
        if source.get("holdout") is not False:
            raise HubRegressionError("Hub regression sources may not use holdout data")
        case_id = source.get("case_id")
        prediction = prediction_by_id.get(str(case_id))
        if prediction is None:
            raise HubRegressionError("Hub regression prediction coverage is incomplete")
        renderer_hashes, plain_text = _rendered_hashes(prediction)
        protected_spans = source.get("protected_spans")
        if not isinstance(protected_spans, list) or any(
            not isinstance(span, str) or span not in plain_text for span in protected_spans
        ):
            raise HubRegressionError("Hub regression golden output changed a protected span")
        materialized.append(
            {
                **source,
                "expected": {
                    "sst_sha256": _sha256_text(prediction),
                    "renderer_sha256": renderer_hashes,
                },
            }
        )
    if set(prediction_by_id) != {str(source.get("case_id")) for source in sources}:
        raise HubRegressionError("Hub regression predictions contain unbound extra cases")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(_canonical_line(record) for record in materialized)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(payload)
    try:
        _, evidence = load_hub_regression_suite(temporary, policy)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(output)
    return evidence


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[rank - 1]


def run_hub_regression_suite(
    suite_path: str | Path,
    policy: Mapping[str, object],
    *,
    predictor: Callable[[dict[str, Any]], Mapping[str, object]],
) -> dict[str, object]:
    """Run every bound case and recheck SST, protected spans, and renderers."""

    cases, evidence = load_hub_regression_suite(suite_path, policy)
    latency_ms: list[float] = []
    protected_checks = 0
    renderer_checks = {name: 0 for name in sorted(_RENDERERS)}
    for case in cases:
        result = predictor(case)
        prediction = result.get("prediction_sst")
        generation_seconds = result.get("generation_seconds")
        if (
            result.get("valid_sst") is not True
            or result.get("restoration_error") is not None
            or not isinstance(prediction, str)
            or not prediction
        ):
            raise HubRegressionError(
                f"Hub regression model output is invalid for {case['case_id']}"
            )
        if (
            isinstance(generation_seconds, bool)
            or not isinstance(generation_seconds, int | float)
            or not math.isfinite(float(generation_seconds))
            or generation_seconds < 0
        ):
            raise HubRegressionError("Hub regression latency evidence is invalid")
        latency_ms.append(float(generation_seconds) * 1000)
        expected = case["expected"]
        if _sha256_text(prediction) != expected["sst_sha256"]:
            raise HubRegressionError(
                f"Hub regression output hash changed for {case['case_id']}"
            )
        hashes, plain_text = _rendered_hashes(prediction)
        if hashes != expected["renderer_sha256"]:
            raise HubRegressionError(
                f"Hub regression renderer hash changed for {case['case_id']}"
            )
        for span in case["protected_spans"]:
            if span not in plain_text:
                raise HubRegressionError(
                    f"Hub regression protected span changed for {case['case_id']}"
                )
            protected_checks += 1
        for name in renderer_checks:
            renderer_checks[name] += 1
    return {
        "status": "passed",
        **evidence,
        "exact_output_hash_matches": len(cases),
        "protected_span_checks": protected_checks,
        "sst_round_trip_checks": len(cases),
        "renderer_checks": renderer_checks,
        "latency_ms": {
            "sample_count": len(latency_ms),
            "p50": _percentile(latency_ms, 50),
            "p95": _percentile(latency_ms, 95),
            "p99": _percentile(latency_ms, 99),
            "maximum": max(latency_ms),
        },
    }

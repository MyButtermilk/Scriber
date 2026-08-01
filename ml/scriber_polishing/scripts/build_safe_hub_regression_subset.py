"""Select only reproducible safe outputs for the non-holdout Hub reload suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_regression import (  # noqa: E402
    HubRegressionError,
    _rendered_hashes,
)


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


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                raise ValueError(f"{label} has a blank line at {line_number}")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError(f"{label} row {line_number} is not an object")
            rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _batch(value: str) -> tuple[Path, Path, Path]:
    parts = value.split("=", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError("--batch must be SOURCES=PREDICTIONS=RESUME_EVIDENCE")
    return Path(parts[0]), Path(parts[1]), Path(parts[2])


def _map(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in result:
            raise ValueError(f"{label} has an invalid or duplicate case ID")
        result[case_id] = row
    return result


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", action="append", required=True)
    parser.add_argument("--minimum-cases", type=int, default=100)
    parser.add_argument("--minimum-ai-gold", type=int, default=10)
    parser.add_argument("--sources-output", type=Path, required=True)
    parser.add_argument("--predictions-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.minimum_cases < 100 or args.minimum_ai_gold < 1:
        raise ValueError("Hub reload subset thresholds are below the fixed safety minimum")

    accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejection_counts: Counter[str] = Counter()
    seen: set[str] = set()
    input_bindings: list[dict[str, Any]] = []
    for batch_index, raw_batch in enumerate(args.batch):
        sources_path, predictions_path, evidence_path = _batch(raw_batch)
        sources = _map(_load_jsonl(sources_path, "Hub regression sources"), "sources")
        predictions = _map(
            _load_jsonl(predictions_path, "Hub regression predictions"),
            "predictions",
        )
        journal = _load_json(evidence_path, "Hub regression resume evidence")
        entries = journal.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Hub regression resume evidence has no entries")
        evidence: dict[str, Mapping[str, Any]] = {}
        journal_predictions: dict[str, Mapping[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("Hub regression resume evidence has an invalid entry")
            case_id = entry.get("case_id")
            item_evidence = entry.get("evidence")
            item_prediction = entry.get("prediction")
            if (
                not isinstance(case_id, str)
                or case_id in evidence
                or not isinstance(item_evidence, Mapping)
                or not isinstance(item_prediction, Mapping)
            ):
                raise ValueError("Hub regression resume evidence identity is invalid")
            evidence[case_id] = item_evidence
            journal_predictions[case_id] = item_prediction
        if set(sources) != set(predictions) or set(sources) != set(evidence):
            raise ValueError("Hub regression batch coverage differs across its three inputs")
        if seen.intersection(sources):
            raise ValueError("Hub regression batches overlap")
        seen.update(sources)
        for case_id in sorted(sources):
            source = sources[case_id]
            prediction = predictions[case_id]
            if prediction != journal_predictions[case_id]:
                raise ValueError("final prediction differs from preserved resume evidence")
            item_evidence = evidence[case_id]
            prediction_sst = prediction.get("prediction_sst")
            reason: str | None = None
            if item_evidence.get("valid_sst") is not True:
                reason = "invalid_sst"
            elif item_evidence.get("restoration_error") is not False:
                reason = "protected_span_restoration_error"
            elif not isinstance(prediction_sst, str) or not prediction_sst:
                reason = "empty_prediction"
            else:
                try:
                    _, plain_text = _rendered_hashes(prediction_sst)
                except HubRegressionError:
                    reason = "noncanonical_or_unsafe_sst"
                else:
                    spans = source.get("protected_spans")
                    if not isinstance(spans, list) or any(
                        not isinstance(span, str) or span not in plain_text
                        for span in spans
                    ):
                        reason = "protected_span_missing"
            if reason is not None:
                rejection_counts[reason] += 1
                continue
            accepted.append((source, prediction))
        input_bindings.append(
            {
                "batch_index": batch_index,
                "source_count": len(sources),
                "sources_sha256": "sha256:"
                + hashlib.sha256(sources_path.read_bytes()).hexdigest(),
                "predictions_sha256": "sha256:"
                + hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
                "resume_evidence_sha256": "sha256:"
                + hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            }
        )

    accepted.sort(key=lambda pair: str(pair[0]["case_id"]))
    category_counts = Counter(str(source["category"]) for source, _ in accepted)
    ai_gold_count = sum(
        source.get("source_kind") == "ai_gold_smoke" for source, _ in accepted
    )
    if len(accepted) < args.minimum_cases:
        raise ValueError(
            f"safe Hub regression subset has {len(accepted)}/{args.minimum_cases} cases"
        )
    if ai_gold_count < args.minimum_ai_gold:
        raise ValueError(
            f"safe Hub regression subset has {ai_gold_count}/{args.minimum_ai_gold} AI-Gold cases"
        )
    if any(category_counts[category] < 1 for category in (
        "ai_gold_smoke",
        "protected_span",
        "business_post",
        "tax_law",
        "paragraphs",
        "headings",
        "lists",
        "units",
        "legal_citations",
        "address_pronouns",
        "identity",
    )):
        raise ValueError("safe Hub regression subset lacks a required category")

    sources_payload = b"".join(_canonical_line(source) for source, _ in accepted)
    predictions_payload = b"".join(
        _canonical_line(prediction) for _, prediction in accepted
    )
    report = {
        "schema_version": 1,
        "kind": "scriber_safe_hub_regression_subset",
        "status": "prepared",
        "input_case_count": len(seen),
        "accepted_case_count": len(accepted),
        "rejected_case_count": len(seen) - len(accepted),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "ai_gold_smoke_count": ai_gold_count,
        "private_data": False,
        "holdout_data": False,
        "content_manually_reviewed": False,
        "sources_sha256": "sha256:" + hashlib.sha256(sources_payload).hexdigest(),
        "predictions_sha256": "sha256:"
        + hashlib.sha256(predictions_payload).hexdigest(),
        "inputs": input_bindings,
    }
    _write_new(args.sources_output, sources_payload)
    _write_new(args.predictions_output, predictions_payload)
    _write_new(args.report_output, _canonical_line(report))
    print(
        "safe_hub_regression_subset=complete "
        f"accepted={len(accepted)} rejected={len(seen) - len(accepted)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

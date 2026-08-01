"""Select and freeze a hash-bound regular synthetic test set outside AI-Gold families."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_io import validate_evaluation_reference  # noqa: E402
from scriber_polishing.evaluation_reference_builder import (  # noqa: E402
    build_evaluation_references,
)
from scriber_polishing.regular_test_selector import (  # noqa: E402
    REFERENCE_VALIDATOR,
    SELECTION_ALGORITHM,
    select_regular_test_records,
    validate_regular_test_selection_manifest,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _expected_hash(value: str, label: str) -> str:
    normalized = value.removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return normalized


def _load_jsonl(payload: bytes, label: str) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error
    if not text or not text.endswith("\n"):
        raise ValueError(f"{label} must end with a newline")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ValueError(f"{label} contains blank line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} row {line_number} must be an object")
        rows.append(value)
    return rows


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    else:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return (serialized + "\n").encode("utf-8")


def _canonical_jsonl(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(record) for record in records)


def _preflight(path: Path, payload: bytes, label: str) -> bool:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite different {label}: {path}")
        return False
    if path.with_suffix(path.suffix + ".tmp").exists():
        raise ValueError(f"temporary {label} path already exists: {path}.tmp")
    return True


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _manifest(
    *,
    source_path: Path,
    source_sha256: str,
    gold_path: Path,
    gold_sha256: str,
    output_path: Path,
    output_payload: bytes,
    references: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
    seed: int,
    target_count: int,
) -> dict[str, Any]:
    case_ids = [str(reference["case_id"]) for reference in references]
    value = {
        "schema_version": 1,
        "kind": "scriber_regular_test_selection",
        "selection_algorithm": SELECTION_ALGORITHM,
        "seed": seed,
        "target_count": target_count,
        "source": {
            "filename": source_path.name,
            "records_sha256": source_sha256,
            "record_count": audit["source_record_count"],
            "split": "test",
        },
        "exclusion": {
            "ai_gold_test_filename": gold_path.name,
            "ai_gold_test_records_sha256": gold_sha256,
            "ai_gold_test_count": audit["ai_gold_test_count"],
            "ai_gold_ids_matched_in_source_count": audit["ai_gold_ids_matched_in_source_count"],
            "ai_gold_parent_group_count": audit["ai_gold_parent_group_count"],
            "eligible_after_id_exclusion": audit["eligible_after_id_exclusion"],
            "eligible_after_parent_exclusion": audit["eligible_after_parent_exclusion"],
            "parent_exclusion_mode": audit["parent_exclusion_mode"],
            "selected_ai_gold_id_overlap_count": audit["selected_ai_gold_id_overlap_count"],
            "selected_ai_gold_parent_overlap_count": audit["selected_ai_gold_parent_overlap_count"],
        },
        "output": {
            "filename": output_path.name,
            "records_sha256": _sha256(output_payload),
            "case_ids_sha256": _sha256(_canonical_json(case_ids)),
            "record_count": len(references),
            "split_counts": {"test": len(references)},
        },
        "coverage": {
            "unique_parent_count": audit["unique_parent_count"],
            "domain_counts": audit["domain_counts"],
            "corruption_profile_counts": audit["corruption_profile_counts"],
            "domain_profile_counts": audit["domain_profile_counts"],
            "operation_counts": audit["operation_counts"],
            "domain_balance_delta": audit["domain_balance_delta"],
            "corruption_profile_balance_delta": audit["corruption_profile_balance_delta"],
        },
        "validation": {
            "reference_validator": REFERENCE_VALIDATOR,
            "validated_record_count": len(references),
        },
    }
    return validate_regular_test_selection_manifest(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--ai-gold-test", type=Path, required=True)
    parser.add_argument("--expected-ai-gold-sha256", required=True)
    parser.add_argument("--expected-ai-gold-count", type=int, default=1_000)
    parser.add_argument("--target-count", type=int, default=1_000)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.manifest.resolve():
            raise ValueError("--output and --manifest must be different paths")
        expected_source_sha256 = _expected_hash(
            args.expected_input_sha256,
            "--expected-input-sha256",
        )
        expected_gold_sha256 = _expected_hash(
            args.expected_ai_gold_sha256,
            "--expected-ai-gold-sha256",
        )
        source_payload = args.input.read_bytes()
        source_sha256 = _sha256(source_payload)
        if source_sha256 != expected_source_sha256:
            raise ValueError(f"input SHA-256 mismatch: expected {expected_source_sha256}, got {source_sha256}")
        gold_payload = args.ai_gold_test.read_bytes()
        gold_sha256 = _sha256(gold_payload)
        if gold_sha256 != expected_gold_sha256:
            raise ValueError(f"AI-Gold test SHA-256 mismatch: expected {expected_gold_sha256}, got {gold_sha256}")
        source_rows = _load_jsonl(source_payload, "test source")
        gold_rows = _load_jsonl(gold_payload, "AI-Gold test exclusion")
        if len(gold_rows) != args.expected_ai_gold_count:
            raise ValueError(
                f"AI-Gold test exclusion has {len(gold_rows)} records; {args.expected_ai_gold_count} required"
            )
        selection = select_regular_test_records(
            source_rows,
            gold_rows,
            target_count=args.target_count,
            seed=args.seed,
        )
        references = [
            validate_evaluation_reference(reference)
            for reference in build_evaluation_references(
                selection.records,
                output_split="test",
            )
        ]
        output_payload = _canonical_jsonl(references)
        manifest = _manifest(
            source_path=args.input,
            source_sha256=source_sha256,
            gold_path=args.ai_gold_test,
            gold_sha256=gold_sha256,
            output_path=args.output,
            output_payload=output_payload,
            references=references,
            audit=selection.audit,
            seed=args.seed,
            target_count=args.target_count,
        )
        manifest_payload = _canonical_json(manifest, pretty=True)
        write_output = _preflight(args.output, output_payload, "regular test references")
        write_manifest = _preflight(
            args.manifest,
            manifest_payload,
            "regular test selection manifest",
        )
        if _sha256(args.input.read_bytes()) != source_sha256:
            raise ValueError("input changed before regular test publication")
        if _sha256(args.ai_gold_test.read_bytes()) != gold_sha256:
            raise ValueError("AI-Gold test exclusion changed before publication")
        if write_output:
            _write(args.output, output_payload)
        if write_manifest:
            _write(args.manifest, manifest_payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"regular_test_selection=failed reason={error}", file=sys.stderr)
        return 2
    print(f"regular_test_selection=pass records={len(references)} sha256={manifest['output']['records_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

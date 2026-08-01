"""Validate one exact Terra session and emit the compact V2/V1 promotion report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.compact_terra_judge import (  # noqa: E402
    aggregate_compact_terra_verdicts,
    public_cases_jsonl_bytes,
    validate_compact_terra_verdicts,
    verdicts_jsonl_bytes,
)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _load_jsonl(path: Path, label: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank {label} line at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        rows.append(value)
    return tuple(rows)


def _load_object(path: Path, label: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-cases", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--verdicts", type=Path, required=True)
    parser.add_argument("--canonical-verdicts-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = [
        args.public_cases.resolve(),
        args.private_mapping.resolve(),
        args.verdicts.resolve(),
        args.canonical_verdicts_output.resolve(),
        args.output.resolve(),
    ]
    if len(set(paths)) != len(paths):
        raise ValueError("all compact Terra input and output paths must be distinct")

    public_cases = _load_jsonl(args.public_cases, "public case")
    if args.public_cases.read_bytes() != public_cases_jsonl_bytes(public_cases):
        raise ValueError("public compact Terra cases must use canonical JSONL")
    private_mapping = _load_object(args.private_mapping, "private mapping")
    if args.private_mapping.read_bytes() != _canonical_json_bytes(private_mapping):
        raise ValueError("private compact Terra mapping must use canonical JSON")
    raw_verdicts = _load_jsonl(args.verdicts, "Terra verdict")
    validated_verdicts = validate_compact_terra_verdicts(
        raw_verdicts,
        public_cases=public_cases,
        private_mapping=private_mapping,
    )
    report = aggregate_compact_terra_verdicts(
        validated_verdicts,
        public_cases=public_cases,
        private_mapping=private_mapping,
    )
    _write_atomic(args.canonical_verdicts_output, verdicts_jsonl_bytes(validated_verdicts))
    _write_atomic(args.output, _canonical_json_bytes(report))
    summary = {
        "schema_version": 1,
        "kind": "scriber_v2_compact_terra_aggregate_summary",
        "decision": report["gate"]["decision"],
        "judge_model_family": report["judge"]["model_family"],
        "judge_session_sha256": report["judge"]["session_sha256"],
        "prompt_sha256": report["judge"]["prompt_sha256"],
        "verdict_count": report["coverage"]["verdict_count"],
        "verdicts_sha256": report["artifacts"]["verdicts_sha256"],
        "evidence_class": report["evidence_class"],
        "publication_grade": report["publication_grade"],
    }
    sys.stdout.buffer.write(_canonical_json_bytes(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

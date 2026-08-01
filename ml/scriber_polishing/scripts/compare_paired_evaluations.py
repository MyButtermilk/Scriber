"""Compare base and fine-tuned evaluation reports with a seeded paired bootstrap."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.paired_significance import (  # noqa: E402
    PAIRABLE_CASE_METRICS,
    compare_paired_evaluations,
)


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value, hashlib.sha256(payload).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"output must be a regular file when it already exists: {path}")
        if path.read_bytes() != payload:
            raise ValueError(f"paired significance report already exists with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-report", type=Path, required=True)
    parser.add_argument("--fine-tuned-report", type=Path, required=True)
    parser.add_argument("--metric", choices=sorted(PAIRABLE_CASE_METRICS), default="normalized_exact_match")
    parser.add_argument("--seed", type=int, default=270020)
    parser.add_argument("--resamples", type=int, default=10_000)
    parser.add_argument("--confidence-level", type=float, default=0.95)
    parser.add_argument("--minimum-delta", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    input_paths = {args.base_report.resolve(), args.fine_tuned_report.resolve()}
    if args.output.resolve() in input_paths:
        raise ValueError("--output must not overwrite an input evaluation report")

    base_report, base_hash = _read_json(args.base_report, label="base report")
    fine_tuned_report, fine_tuned_hash = _read_json(args.fine_tuned_report, label="fine-tuned report")
    report = compare_paired_evaluations(
        base_report,
        fine_tuned_report,
        base_report_sha256=base_hash,
        fine_tuned_report_sha256=fine_tuned_hash,
        metric=args.metric,
        seed=args.seed,
        resamples=args.resamples,
        confidence_level=args.confidence_level,
        minimum_delta=args.minimum_delta,
    )
    _write_immutable(args.output, report)
    print(
        f"paired_significance=complete decision={report['decision']} metric={args.metric} "
        f"mean_delta={report['paired_delta']['mean']:.6f} output={args.output}"
    )
    return 0 if report["bootstrap"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Combine both complete pairwise judge reports into one fail-closed release decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.judge_aggregation import (  # noqa: E402
    aggregate_judge_campaign_reports,
)


def _load_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"judge comparison report must be a JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comparison-aggregation",
        type=Path,
        action="append",
        required=True,
        help="Provide exactly the base and rule-baseline aggregation reports.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.comparison_aggregation) != 2:
        raise ValueError("exactly two --comparison-aggregation values are required")
    if _path_identity(args.output) in {_path_identity(path) for path in args.comparison_aggregation}:
        raise ValueError("judge campaign output must not overwrite a comparison input")

    reports = [_load_report(path) for path in args.comparison_aggregation]
    aggregation_hashes: dict[str, str] = {}
    for path, report in zip(args.comparison_aggregation, reports, strict=True):
        comparison_kind = report.get("comparison_kind")
        if not isinstance(comparison_kind, str) or not comparison_kind:
            raise ValueError(f"judge comparison report lacks comparison_kind: {path}")
        if comparison_kind in aggregation_hashes:
            raise ValueError(f"duplicate judge comparison report: {comparison_kind}")
        aggregation_hashes[comparison_kind] = hashlib.sha256(path.read_bytes()).hexdigest()

    result = aggregate_judge_campaign_reports(
        reports,
        aggregation_sha256s=aggregation_hashes,
    )
    _write_json(args.output, result)
    print(
        f"judge_campaign={result['status']} "
        f"comparisons={len(result['comparisons'])} "
        f"release_gate_passed={str(result['release_gate_passed']).lower()}"
    )
    return 0 if result["release_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

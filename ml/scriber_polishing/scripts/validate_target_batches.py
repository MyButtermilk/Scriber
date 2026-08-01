"""Validate all local canonical target batches and write a redacted JSON summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.target_batch_validator import TargetBatchValidationError, validate_target_batches  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT / "data" / "targets",
        help="directory containing target_writer_* batches",
    )
    parser.add_argument("--output", type=Path, required=True, help="redacted JSON summary destination")
    args = parser.parse_args()
    try:
        summary = validate_target_batches(args.root)
    except TargetBatchValidationError as error:
        print(error, file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

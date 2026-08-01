"""Validate all local AI seed batches and write a redacted JSON summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.seed_batch_validator import SeedBatchValidationError, validate_seed_batches  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=PROJECT_ROOT / "data" / "seeds", help="directory containing generator_* batches"
    )
    parser.add_argument("--output", type=Path, required=True, help="redacted JSON summary destination")
    args = parser.parse_args()
    try:
        summary = validate_seed_batches(args.root)
    except SeedBatchValidationError as error:
        print(error, file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create deterministic local canonical-document variants from explicit roots."""

from __future__ import annotations

import argparse
from pathlib import Path

from scriber_polishing.canonical_expander import VARIANT_COUNT, expand_roots


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-root",
        type=Path,
        nargs="+",
        required=True,
        help="One or more explicit roots containing plans.jsonl files.",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        nargs="+",
        required=True,
        help="One or more explicit roots containing targets.jsonl files.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Destination JSONL path.")
    parser.add_argument("--manifest", type=Path, required=True, help="Destination checksum manifest path.")
    parser.add_argument(
        "--approval",
        type=Path,
        help="Optional critic-aggregation approval; filters and annotates accepted seeds.",
    )
    parser.add_argument(
        "--minimum-count",
        type=int,
        default=14_700,
        help="Fail unless at least this many records were written (default: 14700 = 2100 seeds × 7 variants).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.minimum_count < VARIANT_COUNT:
        raise SystemExit(f"--minimum-count must be at least {VARIANT_COUNT}")
    result = expand_roots(
        args.seed_root,
        args.target_root,
        args.output,
        args.manifest,
        minimum_count=args.minimum_count,
        approval=args.approval,
    )
    print(f"Wrote {result['record_count']} canonical variants; checksum {result['sha256']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

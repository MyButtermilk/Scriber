"""Freeze the aggregate-only AI-Verified Gold Set from approved JSONL records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.gold_builder import (  # noqa: E402
    DEFAULT_AI_GOLD_SPLIT_TARGETS,
    freeze_ai_verified_gold,
)
from scriber_polishing.split_dataset import Split  # noqa: E402


def _load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line:
                raise ValueError(f"blank JSONL line in {path} at {line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"non-object JSONL line in {path} at {line_number}")
            rows.append(value)
    if not rows:
        raise ValueError("AI-Gold input files contain no records")
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--sealed-output-dir", type=Path)
    parser.add_argument("--seed", type=int, default=270_004)
    parser.add_argument(
        "--train-count",
        type=int,
        default=DEFAULT_AI_GOLD_SPLIT_TARGETS[Split.TRAIN],
    )
    parser.add_argument(
        "--validation-count",
        type=int,
        default=DEFAULT_AI_GOLD_SPLIT_TARGETS[Split.VALIDATION],
    )
    parser.add_argument(
        "--test-count",
        type=int,
        default=DEFAULT_AI_GOLD_SPLIT_TARGETS[Split.TEST],
    )
    parser.add_argument(
        "--challenge-count",
        type=int,
        default=DEFAULT_AI_GOLD_SPLIT_TARGETS[Split.CHALLENGE],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    targets = {
        Split.TRAIN: args.train_count,
        Split.VALIDATION: args.validation_count,
        Split.TEST: args.test_count,
        Split.CHALLENGE: args.challenge_count,
    }
    try:
        result = freeze_ai_verified_gold(
            _load_jsonl(args.input),
            seed=args.seed,
            manifest_path=args.manifest_output,
            sealed_output_dir=args.sealed_output_dir,
            split_targets=targets,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"ai_gold_freeze=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"ai_gold_freeze=pass total={result.manifest['total_count']} "
        f"manifest_sha256={result.manifest_sha256} "
        f"sealed_outputs={str(bool(result.sealed_files)).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

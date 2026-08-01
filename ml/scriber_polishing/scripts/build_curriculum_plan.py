"""Build one immutable, hash-bound shuffled or staged curriculum order plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import (  # noqa: E402
    ai_gold_train_ids,
    build_curriculum_plan,
    load_hash_bound_jsonl,
    write_curriculum_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--expected-train-sha256", required=True)
    parser.add_argument("--ai-gold-train", type=Path, required=True)
    parser.add_argument("--expected-ai-gold-sha256", required=True)
    parser.add_argument("--mode", choices=("shuffled", "staged"), required=True)
    parser.add_argument("--epochs", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        train, train_binding = load_hash_bound_jsonl(
            args.train,
            expected_sha256=args.expected_train_sha256,
            expected_split="train",
            label="curriculum train source",
        )
        ai_gold, ai_gold_binding = load_hash_bound_jsonl(
            args.ai_gold_train,
            expected_sha256=args.expected_ai_gold_sha256,
            expected_split="train",
            label="AI-Gold train source",
        )
        plan = build_curriculum_plan(
            train,
            source_binding=train_binding,
            ai_gold_ids=ai_gold_train_ids(ai_gold),
            ai_gold_binding=ai_gold_binding,
            mode=args.mode,
            epochs=args.epochs,
            seed=args.seed,
        )
        manifest = write_curriculum_plan(
            plan,
            destination=args.output,
            manifest_destination=args.manifest,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"curriculum_plan=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"curriculum_plan=pass mode={manifest['mode']} "
        f"records={manifest['record_count']} sha256={manifest['plan_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

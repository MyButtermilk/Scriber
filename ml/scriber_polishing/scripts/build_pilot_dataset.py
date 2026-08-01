"""Select reproducible pilot subsets from the frozen full dataset."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.pilot_sampler import build_pilot_subsets  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for split in ("train", "validation", "test", "challenge"):
        parser.add_argument(f"--{split}", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=270_011)
    parser.add_argument("--train-count", type=int, default=5_000)
    parser.add_argument("--validation-count", type=int, default=500)
    parser.add_argument("--test-count", type=int, default=500)
    parser.add_argument("--challenge-count", type=int, default=250)
    args = parser.parse_args(argv)
    try:
        manifest = build_pilot_subsets(
            {
                split: getattr(args, split)
                for split in ("train", "validation", "test", "challenge")
            },
            output_root=args.output_root,
            counts={
                split: getattr(args, f"{split}_count")
                for split in ("train", "validation", "test", "challenge")
            },
            seed=args.seed,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"pilot_dataset=failed reason={error}", file=sys.stderr)
        return 2
    print(
        "pilot_dataset=pass "
        + " ".join(
            f"{split}={manifest['split_counts'][split]}"
            for split in ("train", "validation", "test", "challenge")
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

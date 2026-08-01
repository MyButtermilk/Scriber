"""Build balanced validation-only references for curriculum or checkpoint decisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import (  # noqa: E402
    ai_gold_train_ids,
    load_hash_bound_jsonl,
)
from scriber_polishing.curriculum_dev import (  # noqa: E402
    build_curriculum_dev_references,
    exclusion_ids_from_manifest,
    load_curriculum_dev_manifest,
    write_curriculum_dev_references,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--ai-gold-train", type=Path, required=True)
    parser.add_argument("--expected-ai-gold-sha256", required=True)
    parser.add_argument(
        "--purpose",
        choices=("curriculum", "checkpoint_selection"),
        required=True,
    )
    parser.add_argument("--per-phase", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--exclude-manifest",
        action="append",
        default=[],
        nargs=2,
        metavar=("PATH", "SHA256"),
        help="Prior curriculum-development manifest whose selected IDs must be excluded.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validation, validation_binding = load_hash_bound_jsonl(
            args.input,
            expected_sha256=args.expected_input_sha256,
            expected_split="validation",
            label="curriculum development source",
        )
        ai_gold, ai_gold_binding = load_hash_bound_jsonl(
            args.ai_gold_train,
            expected_sha256=args.expected_ai_gold_sha256,
            expected_split="train",
            label="AI-Gold train source",
        )
        exclusions: set[str] = set()
        for raw_path, expected_sha256 in args.exclude_manifest:
            exclusions.update(
                exclusion_ids_from_manifest(
                    load_curriculum_dev_manifest(
                        Path(raw_path),
                        expected_sha256=expected_sha256,
                    )
                )
            )
        references, pending_manifest = build_curriculum_dev_references(
            validation,
            source_binding=validation_binding,
            ai_gold_ids=ai_gold_train_ids(ai_gold),
            ai_gold_binding=ai_gold_binding,
            purpose=args.purpose,
            per_phase=args.per_phase,
            seed=args.seed,
            excluded_example_ids=frozenset(exclusions),
        )
        manifest = write_curriculum_dev_references(
            references,
            pending_manifest,
            references_destination=args.output,
            manifest_destination=args.manifest,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"curriculum_dev=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"curriculum_dev=pass purpose={manifest['purpose']} "
        f"records={manifest['references']['record_count']} "
        f"sha256={manifest['references']['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

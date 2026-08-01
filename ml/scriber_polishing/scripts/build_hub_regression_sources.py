"""Select the immutable private-Hub reload suite from non-holdout records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_regression_sources import (  # noqa: E402
    HubRegressionSourceError,
    build_hub_regression_sources,
    write_hub_regression_sources,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--synthetic",
        type=Path,
        action="append",
        required=True,
        help="Repeat for non-holdout train or validation JSONL files.",
    )
    parser.add_argument(
        "--ai-gold",
        type=Path,
        action="append",
        required=True,
        help="Repeat for AI-Gold train or validation JSONL files.",
    )
    parser.add_argument("--seed", type=int, default=270043)
    parser.add_argument("--per-category", type=int, default=10)
    parser.add_argument("--sources-output", type=Path, required=True)
    parser.add_argument("--references-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build = build_hub_regression_sources(
            synthetic_paths=args.synthetic,
            ai_gold_paths=args.ai_gold,
            seed=args.seed,
            per_category=args.per_category,
        )
        write_hub_regression_sources(
            build,
            sources_output=args.sources_output,
            references_output=args.references_output,
            report_output=args.report_output,
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        HubRegressionSourceError,
    ) as error:
        print(f"hub_regression_sources=failed reason={error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": build.evidence["status"],
                "case_count": build.evidence["case_count"],
                "source_sha256": build.evidence["source_sha256"],
                "reference_sha256": build.evidence["reference_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

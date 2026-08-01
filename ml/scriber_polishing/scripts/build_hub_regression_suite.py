"""Materialize the immutable non-holdout regression suite for Hub reloads."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hub_publication import load_publication_config  # noqa: E402
from scriber_polishing.hub_regression import (  # noqa: E402
    HubRegressionError,
    materialize_hub_regression_suite,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "publication.yaml",
    )
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_publication_config(args.config)
        policy = config["artifacts"]["model"]["reload"]["regression"]
        evidence = materialize_hub_regression_suite(
            args.sources,
            args.predictions,
            args.output,
            policy=policy,
        )
    except (OSError, ValueError, HubRegressionError) as error:
        print(f"hub_regression_suite=failed reason={error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared", **evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

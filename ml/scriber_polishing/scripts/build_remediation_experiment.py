"""Build a static matched remediation A/B bundle without loading or training a model."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.remediation_experiment import (  # noqa: E402
    build_remediation_experiment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    bundle = build_remediation_experiment(
        args.config,
        output_dir=args.output_dir,
    )
    manifest = bundle.manifest
    print(
        "remediation_bundle=complete "
        f"experiment_id={manifest['experiment_id']} "
        f"selected_examples={manifest['selection']['selected_examples']} "
        f"update_steps={manifest['schedule']['update_steps']} "
        f"comparison_pair_sha256={manifest['comparison_pair_sha256']} "
        "holdout_inputs=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

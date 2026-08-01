"""Compare completed shuffled/staged remediation arms against their frozen bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.remediation_comparison import (  # noqa: E402
    RemediationArmArtifacts,
    RemediationComparisonError,
    compare_remediation_experiment,
    write_remediation_comparison_report,
)


def _arm(
    args: argparse.Namespace,
    mode: str,
) -> RemediationArmArtifacts:
    return RemediationArmArtifacts(
        training_report=getattr(args, f"{mode}_training_report"),
        predictions=getattr(args, f"{mode}_predictions"),
        prediction_manifest=getattr(
            args,
            f"{mode}_prediction_manifest",
        ),
        evaluation_report=getattr(
            args,
            f"{mode}_evaluation_report",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    for mode in ("shuffled", "staged"):
        parser.add_argument(
            f"--{mode}-training-report",
            type=Path,
            required=True,
        )
        parser.add_argument(
            f"--{mode}-predictions",
            type=Path,
            required=True,
        )
        parser.add_argument(
            f"--{mode}-prediction-manifest",
            type=Path,
            required=True,
        )
        parser.add_argument(
            f"--{mode}-evaluation-report",
            type=Path,
            required=True,
        )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare_remediation_experiment(
            args.bundle_manifest,
            references=args.references,
            shuffled=_arm(args, "shuffled"),
            staged=_arm(args, "staged"),
        )
        write_remediation_comparison_report(report, args.output)
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RemediationComparisonError,
    ) as error:
        print(
            f"remediation_comparison=failed reason={error}",
            file=sys.stderr,
        )
        return 2
    print(
        "remediation_comparison=complete "
        f"decision={report['decision']} "
        f"adoption={report['adoption']} "
        f"delta={report['composite_delta']['actual']:.8f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

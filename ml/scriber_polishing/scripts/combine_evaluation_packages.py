"""Combine hash-bound evaluation reference or prediction JSONL packages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_bundle import (  # noqa: E402
    EvaluationBundleError,
    EvaluationPackagePart,
    combine_prediction_packages,
    combine_reference_packages,
)


def _parts(values: list[list[str]]) -> tuple[EvaluationPackagePart, ...]:
    return tuple(
        EvaluationPackagePart(Path(records), Path(manifest))
        for records, manifest in values
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    references = subparsers.add_parser("references")
    references.add_argument(
        "--input",
        action="append",
        nargs=2,
        required=True,
        metavar=("JSONL", "MANIFEST"),
    )
    references.add_argument("--output", type=Path, required=True)
    references.add_argument("--manifest", type=Path, required=True)

    predictions = subparsers.add_parser("predictions")
    predictions.add_argument(
        "--input",
        action="append",
        nargs=2,
        required=True,
        metavar=("JSONL", "MANIFEST"),
    )
    predictions.add_argument("--candidate", required=True)
    predictions.add_argument("--references", type=Path, required=True)
    predictions.add_argument("--reference-manifest", type=Path, required=True)
    predictions.add_argument("--output", type=Path, required=True)
    predictions.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "references":
            manifest = combine_reference_packages(
                _parts(args.input),
                destination=args.output,
                manifest_destination=args.manifest,
            )
        else:
            manifest = combine_prediction_packages(
                _parts(args.input),
                candidate=args.candidate,
                references=args.references,
                reference_manifest=args.reference_manifest,
                destination=args.output,
                manifest_destination=args.manifest,
            )
    except (OSError, TypeError, ValueError, EvaluationBundleError) as error:
        print(f"evaluation_bundle=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"evaluation_bundle=pass kind={manifest['kind']} "
        f"records={manifest['record_count']} sha256={manifest['records_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

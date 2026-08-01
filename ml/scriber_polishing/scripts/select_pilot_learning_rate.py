"""Select one learning rate from exactly two immutable fair pilot runs.

This local evidence command does not load a model, use a GPU, or run training.
Each --candidate takes: ID TRAINING_REPORT MODEL_TREE PREDICTION_MANIFEST
EVALUATION_REPORT.  Supply every bundled prediction part manifest separately.
Protected-span diagnostics are optional, but each requires the exact split
evaluation report to which the diagnostic is bound.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.pilot_selection import (  # noqa: E402
    PilotCandidateInput,
    PilotSelectionError,
    select_pilot_learning_rate,
    write_pilot_selection_report,
)


def _group_pairs(
    pairs: list[list[str]],
    *,
    label: str,
) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for candidate_id, raw_path in pairs:
        grouped[candidate_id].append(Path(raw_path))
    if any(not paths for paths in grouped.values()):
        raise PilotSelectionError(f"{label} contains an empty candidate group")
    return dict(grouped)


def _single_pairs(
    pairs: list[list[str]],
    *,
    label: str,
) -> dict[str, Path]:
    grouped = _group_pairs(pairs, label=label)
    duplicates = sorted(candidate_id for candidate_id, paths in grouped.items() if len(paths) != 1)
    if duplicates:
        raise PilotSelectionError(
            f"{label} must occur at most once per candidate: {', '.join(duplicates)}"
        )
    return {candidate_id: paths[0] for candidate_id, paths in grouped.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-config", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        nargs=5,
        action="append",
        metavar=(
            "ID",
            "TRAINING_REPORT",
            "MODEL_TREE",
            "PREDICTION_MANIFEST",
            "EVALUATION_REPORT",
        ),
        required=True,
    )
    parser.add_argument(
        "--prediction-part-manifest",
        nargs=2,
        action="append",
        default=[],
        metavar=("ID", "PATH"),
    )
    parser.add_argument(
        "--protected-span-diagnostic",
        nargs=2,
        action="append",
        default=[],
        metavar=("ID", "PATH"),
    )
    parser.add_argument(
        "--diagnostic-evaluation-report",
        nargs=2,
        action="append",
        default=[],
        metavar=("ID", "PATH"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        part_manifests = _group_pairs(
            args.prediction_part_manifest,
            label="prediction part manifest",
        )
        diagnostics = _single_pairs(
            args.protected_span_diagnostic,
            label="protected-span diagnostic",
        )
        diagnostic_evaluations = _single_pairs(
            args.diagnostic_evaluation_report,
            label="diagnostic evaluation report",
        )
        candidate_rows: list[PilotCandidateInput] = []
        for (
            candidate_id,
            training_report,
            model_tree,
            prediction_manifest,
            evaluation_report,
        ) in args.candidate:
            candidate_rows.append(
                PilotCandidateInput(
                    candidate_id=candidate_id,
                    training_report=Path(training_report),
                    model_tree=Path(model_tree),
                    prediction_manifest=Path(prediction_manifest),
                    prediction_part_manifests=tuple(
                        part_manifests.get(candidate_id, ())
                    ),
                    evaluation_report=Path(evaluation_report),
                    protected_span_diagnostic=diagnostics.get(candidate_id),
                    diagnostic_evaluation_report=diagnostic_evaluations.get(
                        candidate_id
                    ),
                )
            )
        declared_ids = {candidate.candidate_id for candidate in candidate_rows}
        auxiliary_ids = (
            set(part_manifests) | set(diagnostics) | set(diagnostic_evaluations)
        )
        if auxiliary_ids - declared_ids:
            unknown = ", ".join(sorted(auxiliary_ids - declared_ids))
            raise PilotSelectionError(
                f"auxiliary inputs reference unknown candidates: {unknown}"
            )
        if set(diagnostics) != set(diagnostic_evaluations):
            raise PilotSelectionError(
                "diagnostic and diagnostic-evaluation candidate sets must match"
            )
        report = select_pilot_learning_rate(
            candidate_rows,
            reference_manifest_path=args.reference_manifest,
            evaluation_config_path=args.evaluation_config,
        )
        write_pilot_selection_report(report, args.output)
    except (
        OSError,
        UnicodeDecodeError,
        PilotSelectionError,
    ) as error:
        print(f"pilot_selection=failed error={error}", file=sys.stderr)
        return 2

    release = report["release_winner"]
    remediation = report["remediation_parent"]
    release_id = release["candidate_id"] if release is not None else "none"
    remediation_id = remediation["candidate_id"] if remediation is not None else "none"
    print(
        "pilot_selection=complete "
        f"status={report['status']} "
        f"release_winner={release_id} "
        f"remediation_parent={remediation_id} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Materialize two diagnostic-only, blind Sol follow-up packets from partial Terra evidence."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.sol_followup import (  # noqa: E402
    SolFollowupPackagePaths,
    audit_sol_followup_package,
    build_sol_followup_artifacts,
    write_sol_followup_artifacts,
)


def _prompt_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        nargs=7,
        action="append",
        required=True,
        metavar=(
            "COMPARISON",
            "VERDICTS",
            "RESUME_MANIFEST",
            "BUNDLE",
            "PRIVATE_MAPPING",
            "BUNDLE_MANIFEST",
            "MATERIALIZATION_PLAN",
        ),
        help="Supply the two canonical partial-Terra packages exactly once.",
    )
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        print("sol_followup=blocked reason=existing_output", file=sys.stderr)
        return 2
    try:
        packages = [
            audit_sol_followup_package(
                SolFollowupPackagePaths(
                    comparison_kind=comparison_kind,
                    verdicts=Path(verdicts),
                    resume_manifest=Path(resume_manifest),
                    bundle=Path(bundle),
                    private_mapping=Path(private_mapping),
                    bundle_manifest=Path(bundle_manifest),
                    materialization_plan=Path(materialization_plan),
                )
            )
            for (
                comparison_kind,
                verdicts,
                resume_manifest,
                bundle,
                private_mapping,
                bundle_manifest,
                materialization_plan,
            ) in args.package
        ]
        artifacts = build_sol_followup_artifacts(packages, prompt_hash=_prompt_hash(args.prompt))
        write_sol_followup_artifacts(args.output_dir, artifacts)
    except Exception as error:
        print(f"sol_followup=blocked reason={type(error).__name__}", file=sys.stderr)
        return 2
    selected_public_case_count = sum(
        selection.selection_manifest["selection"]["selected_public_case_count"] for selection in artifacts.selections
    )
    print(
        "sol_followup=diagnostic_only "
        f"packages={len(artifacts.selections)} "
        f"selected_public_cases={selected_public_case_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

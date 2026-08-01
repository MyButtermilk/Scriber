"""Prepare a read-only, fail-closed Git/GitHub handoff report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.git_handoff import (  # noqa: E402
    DEFAULT_SCOPE,
    GitHandoffError,
    build_git_handoff,
    write_git_handoff,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Any path inside the Git worktree.",
    )
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--include-repository-policy-files",
        action="store_true",
        help="Also audit changed .gitignore/.gitattributes files.",
    )
    parser.add_argument(
        "--pr-evidence-manifest",
        type=Path,
        help=(
            "Optional schema_version=1 JSON bindings for the eight PR sections; "
            "every binding must declare path, sha256, and JSON pointer."
        ),
    )
    parser.add_argument(
        "--require-complete-pr-evidence",
        action="store_true",
        help="Return a failing exit code while any PR section lacks bound evidence.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        report = build_git_handoff(
            args.repo_root,
            scope=args.scope,
            include_repository_policy_files=args.include_repository_policy_files,
            pr_evidence_manifest=args.pr_evidence_manifest,
        )
        report_hash = write_git_handoff(report, args.output)
    except (GitHandoffError, OSError, ValueError) as error:
        print(f"git_handoff=ERROR reason={error}", file=sys.stderr)
        return 2

    readiness = report["readiness"]
    print(
        "git_handoff="
        + ("SAFE_TO_STAGE" if readiness["inventory_safe_to_stage"] else "BLOCKED")
        + f" included={report['inventory']['included_count']}"
        + f" excluded={report['inventory']['excluded_count']}"
        + f" credential_findings={report['credential_scan']['finding_count']}"
        + f" pr_evidence_complete={report['pull_request_draft']['complete']}"
        + f" report_sha256={report_hash}"
    )
    if not readiness["inventory_safe_to_stage"]:
        return 2
    if args.require_complete_pr_evidence and not report["pull_request_draft"]["complete"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

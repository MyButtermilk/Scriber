"""Create a leakage-safe, non-release audit for two incomplete Terra judge stages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.terra_interim_audit import (  # noqa: E402
    TerraPackagePaths,
    audit_terra_package,
    build_terra_interim_artifacts,
    write_terra_interim_artifacts,
)


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
            "STAGE_PACKET",
            "STAGE_PACKET_MANIFEST",
            "BUNDLE",
            "PRIVATE_MAPPING",
            "BUNDLE_MANIFEST",
        ),
        help="Supply both canonical pairwise comparison packages exactly once.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise ValueError("refusing to overwrite an existing Terra interim audit directory")
    try:
        packages = [
            audit_terra_package(
                TerraPackagePaths(
                    comparison_kind=comparison_kind,
                    verdicts=Path(verdicts),
                    stage_packet=Path(stage_packet),
                    stage_packet_manifest=Path(stage_packet_manifest),
                    bundle=Path(bundle),
                    private_mapping=Path(private_mapping),
                    bundle_manifest=Path(bundle_manifest),
                )
            )
            for (
                comparison_kind,
                verdicts,
                stage_packet,
                stage_packet_manifest,
                bundle,
                private_mapping,
                bundle_manifest,
            ) in args.package
        ]
        artifacts = build_terra_interim_artifacts(packages)
        write_terra_interim_artifacts(args.output_dir, artifacts)
    except Exception as error:
        print(f"terra_interim_audit=blocked reason={type(error).__name__}", file=sys.stderr)
        return 2
    report = artifacts.audit_report
    print(
        "terra_interim_audit=in_progress "
        f"packages={len(report['packages'])} "
        f"backlog_records={report['backlog']['record_count']}"
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

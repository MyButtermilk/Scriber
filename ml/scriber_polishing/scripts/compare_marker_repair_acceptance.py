"""Accept one lexical marker repair only after every frozen hard gate passes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.marker_repair_acceptance import (  # noqa: E402
    MarkerRepairAcceptanceError,
    compare_marker_repair_acceptance,
    write_marker_repair_acceptance_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--cloud-output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare_marker_repair_acceptance(
            bundle_dir=args.bundle_dir,
            cloud_output_dir=args.cloud_output_dir,
        )
        write_marker_repair_acceptance_report(report, args.output)
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        MarkerRepairAcceptanceError,
    ) as error:
        print(
            f"marker_repair_acceptance=failed reason={error}",
            file=sys.stderr,
        )
        return 2
    print(
        "marker_repair_acceptance=complete "
        f"decision={report['decision']} "
        f"new_critical_errors="
        f"{report['safety']['new_critical_errors']['actual']}"
    )
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

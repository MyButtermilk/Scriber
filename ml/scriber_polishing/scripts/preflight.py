from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.preflight import collect_preflight, write_preflight  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the local Scriber polishing training environment.")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "preflight.json",
    )
    args = parser.parse_args()
    report = collect_preflight(project_root=PROJECT_ROOT)
    write_preflight(report, args.output)
    print(f"preflight={report['status']} output={args.output}")
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())

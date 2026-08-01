"""Validate and immutably finalize a recovered marker-repair cloud output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.marker_repair_recovery import (  # noqa: E402
    MarkerRepairRecoveryError,
    finalize_marker_repair_recovery,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-root", type=Path, required=True)
    parser.add_argument(
        "--evaluation-gate-exit",
        type=int,
        choices=(0, 2),
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        finalize_marker_repair_recovery(
            args.recovery_root,
            evaluation_gate_exit=args.evaluation_gate_exit,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        MarkerRepairRecoveryError,
    ) as error:
        print(
            f"marker_repair_recovery=failed reason={error}",
            file=sys.stderr,
        )
        return 2
    print(
        f"marker_repair_recovery=complete training_steps=146 evaluation_cases=200 gate_exit={args.evaluation_gate_exit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

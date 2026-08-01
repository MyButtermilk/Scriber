#!/usr/bin/env python3
"""Verify Attempt-13 completion from small metadata and write its model binding."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_training_postflight import (  # noqa: E402
    V2TrainingPostflightError,
    canonical_json_bytes,
    verify_v2_training_postflight,
)


def _write_exclusive(path: Path, value: object) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(canonical_json_bytes(value))
    except FileExistsError as error:
        raise V2TrainingPostflightError("refusing to replace an existing V2 completion report") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch-receipt", type=Path, required=True)
    parser.add_argument("--job-inspect", type=Path, required=True)
    parser.add_argument("--training-report", type=Path, required=True)
    parser.add_argument("--training-result", type=Path, required=True)
    parser.add_argument("--runtime-verification", type=Path, required=True)
    parser.add_argument("--packet-verification", type=Path, required=True)
    parser.add_argument("--parent-binding", type=Path, required=True)
    parser.add_argument("--final-inventory", type=Path, required=True)
    parser.add_argument("--reload-verification", type=Path, required=True)
    parser.add_argument("--bucket-inventory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify_v2_training_postflight(
        launch_receipt=args.launch_receipt,
        terminal_job=args.job_inspect,
        training_report=args.training_report,
        training_result=args.training_result,
        runtime_verification=args.runtime_verification,
        packet_verification=args.packet_verification,
        parent_binding=args.parent_binding,
        final_inventory=args.final_inventory,
        reload_verification=args.reload_verification,
        remote_inventory=args.bucket_inventory,
    )
    _write_exclusive(args.report, result)
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

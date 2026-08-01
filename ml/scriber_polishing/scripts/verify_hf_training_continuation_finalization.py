"""Verify an immutable Attempt-10 finalization packet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_training_continuation_finalization import (  # noqa: E402
    _write_new_json,
    verify_hf_continuation_finalization_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument(
        "--expected-packet-tree-sha256",
        required=True,
    )
    parser.add_argument(
        "--expected-source-contract-sha256",
        required=True,
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    result = verify_hf_continuation_finalization_packet(
        args.packet_root,
        expected_packet_tree_sha256=args.expected_packet_tree_sha256,
        expected_source_contract_sha256=(args.expected_source_contract_sha256),
    )
    _write_new_json(args.report.expanduser().resolve(), result)
    print(result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

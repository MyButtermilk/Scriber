"""Verify an immutable no-training continuation-recovery packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import canonical_json_bytes  # noqa: E402
from scriber_polishing.hf_training_continuation_recovery import (  # noqa: E402
    verify_hf_continuation_recovery_packet,
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

    result = verify_hf_continuation_recovery_packet(
        args.packet_root,
        expected_packet_tree_sha256=args.expected_packet_tree_sha256,
        expected_source_contract_sha256=(args.expected_source_contract_sha256),
    )
    destination = args.report.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite recovery packet report: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(result)
    destination.write_bytes(payload)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

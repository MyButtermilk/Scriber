"""Materialize the immutable HF checkpoint-254 recovery packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_training_continuation_recovery import (  # noqa: E402
    build_hf_continuation_recovery_launch_contract,
    build_hf_continuation_recovery_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--continuation-bundle-dir",
        type=Path,
        required=True,
    )
    args = parser.parse_args(argv)

    packet = build_hf_continuation_recovery_packet(
        output_dir=args.output_dir,
        continuation_bundle_dir=args.continuation_bundle_dir,
    )
    contract = build_hf_continuation_recovery_launch_contract(packet.output_dir)
    print(json.dumps(contract, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

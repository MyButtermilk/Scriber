"""Build and locally verify the immutable Scriber V2 Hugging Face packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_training_job import (  # noqa: E402
    V2_POLICY_PATH,
    package_v2_training_job,
    verify_v2_training_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=V2_POLICY_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    packet = package_v2_training_job(
        repository_root=args.repository_root,
        dataset_root=args.dataset_root,
        policy_path=args.policy,
        output_dir=args.output,
    )
    verification = verify_v2_training_packet(
        args.output,
        expected_packet_tree_sha256=str(packet["packet_tree_sha256"]),
        expected_manifest_sha256=str(packet["manifest_sha256"]),
        expected_launch_contract_sha256=str(packet["launch_contract_sha256"]),
    )
    print(json.dumps({**packet, "packet_dir": str(packet["packet_dir"]), "verification": verification}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

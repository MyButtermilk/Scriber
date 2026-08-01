"""Fail-closed in-job runner for the immutable Scriber V2 training packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_training_job import run_v2_training_job  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--parent-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path("/workspace/scriber-v2-hardcase-replay-v1"),
    )
    parser.add_argument("--expected-packet-tree-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-launch-contract-sha256", required=True)
    parser.add_argument("--claim-owner", required=True)
    args = parser.parse_args(argv)
    result = run_v2_training_job(
        packet_root=args.packet_root,
        remote_parent_root=args.parent_root,
        output_root=args.output_root,
        workspace_root=args.workspace_root,
        expected_packet_tree_sha256=args.expected_packet_tree_sha256,
        expected_manifest_sha256=args.expected_manifest_sha256,
        expected_launch_contract_sha256=args.expected_launch_contract_sha256,
        owner_token=args.claim_owner,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_retry import (
    build_hf_final_evaluation_retry_launch_contract,
    build_hf_final_evaluation_retry_packet,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Prepare one stale-lock recovery plus batch-durable replacement evaluation without launching it")
    )
    parser.add_argument("--primary-packet-root", required=True, type=Path)
    parser.add_argument("--amendment-packet-root", required=True, type=Path)
    parser.add_argument("--expected-primary-packet-tree-sha256", required=True)
    parser.add_argument("--expected-amendment-packet-tree-sha256", required=True)
    parser.add_argument("--output-snapshot-root", required=True, type=Path)
    parser.add_argument("--failed-amendment-evidence", required=True, type=Path)
    parser.add_argument("--no-active-jobs-evidence", required=True, type=Path)
    parser.add_argument("--stale-writer-lock-observation", required=True, type=Path)
    parser.add_argument("--stale-owner-json", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--launch-contract", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.launch_contract.exists() or args.launch_contract.is_symlink():
        raise ValueError(f"refusing to overwrite launch contract: {args.launch_contract}")
    packet = build_hf_final_evaluation_retry_packet(
        primary_packet_root=args.primary_packet_root,
        amendment_packet_root=args.amendment_packet_root,
        expected_primary_packet_tree_sha256=args.expected_primary_packet_tree_sha256,
        expected_amendment_packet_tree_sha256=(args.expected_amendment_packet_tree_sha256),
        output_snapshot_root=args.output_snapshot_root,
        failed_amendment_evidence_path=args.failed_amendment_evidence,
        no_active_jobs_evidence_path=args.no_active_jobs_evidence,
        stale_writer_lock_observation_path=args.stale_writer_lock_observation,
        stale_owner_path=args.stale_owner_json,
        authorization_path=args.authorization,
        output_dir=args.output_dir,
    )
    launch = build_hf_final_evaluation_retry_launch_contract(
        packet.output_dir,
        amendment_packet_root=args.amendment_packet_root,
        primary_packet_root=args.primary_packet_root,
    )
    args.launch_contract.parent.mkdir(parents=True, exist_ok=True)
    with args.launch_contract.open("xb") as stream:
        stream.write(canonical_json_bytes(launch))
        stream.flush()
    print(f"packet={packet.output_dir}")
    print(f"packet_tree_sha256={packet.manifest['packet_tree_sha256']}")
    print(f"maximum_total_cost_usd={launch['maximum_total_cost_usd']}")
    print(f"remaining_budget_usd={launch['remaining_budget_usd']}")
    print(f"launch_contract={args.launch_contract.resolve()}")
    print("external_job_started=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

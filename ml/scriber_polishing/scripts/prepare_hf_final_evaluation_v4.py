#!/usr/bin/env python3
"""Build or verify the immutable V4 packet and launch contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_v4 import (
    build_hf_final_evaluation_v4_launch_contract,
    build_hf_final_evaluation_v4_packet,
    verify_hf_final_evaluation_v4_packet,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("build", "verify", "launch-contract"), required=True)
    parser.add_argument("--primary-packet-root", type=Path, required=True)
    parser.add_argument("--amendment-packet-root", type=Path, required=True)
    parser.add_argument("--retry-packet-root", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--job-input-dir", type=Path)
    parser.add_argument("--post-removal-snapshot-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.mode == "build":
        if args.job_input_dir is None or args.post_removal_snapshot_root is None:
            raise ValueError("build requires job inputs and post-removal snapshot")
        packet = build_hf_final_evaluation_v4_packet(
            primary_packet_root=args.primary_packet_root,
            amendment_packet_root=args.amendment_packet_root,
            retry_packet_root=args.retry_packet_root,
            job_input_dir=args.job_input_dir,
            post_removal_snapshot_root=args.post_removal_snapshot_root,
            output_dir=args.packet_root,
        )
        result = dict(packet.manifest)
    elif args.mode == "verify":
        manifest = json.loads((args.packet_root / "job-manifest.json").read_bytes())
        result = verify_hf_final_evaluation_v4_packet(
            args.packet_root,
            retry_packet_root=args.retry_packet_root,
            amendment_packet_root=args.amendment_packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=manifest["packet_tree_sha256"],
        )
    else:
        result = build_hf_final_evaluation_v4_launch_contract(
            args.packet_root,
            retry_packet_root=args.retry_packet_root,
            amendment_packet_root=args.amendment_packet_root,
            primary_packet_root=args.primary_packet_root,
        )
    if args.output is not None:
        output = args.output.expanduser().resolve()
        if output.exists() or output.is_symlink():
            raise ValueError(f"refusing to overwrite V4 preparation output: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

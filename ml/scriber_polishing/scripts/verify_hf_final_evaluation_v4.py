#!/usr/bin/env python3
"""Verify and coordinate the fail-closed V4 final-evaluation recovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scriber_polishing.hf_final_evaluation import release_final_evaluation_writer
from scriber_polishing.hf_final_evaluation_v4 import (
    claim_final_evaluation_v4,
    record_v4_runtime_completion,
    verify_hf_final_evaluation_v4_packet,
    verify_preserved_final_ai_gold_for_v4,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "packet",
            "claim-launch",
            "preserved-lane",
            "runtime-complete",
            "release-writer",
        ),
        required=True,
    )
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--retry-packet-root", type=Path)
    parser.add_argument("--amendment-packet-root", type=Path)
    parser.add_argument("--primary-packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--external-journal-root", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--launch-id")
    parser.add_argument("--owner-token")
    args = parser.parse_args()

    if args.mode == "release-writer":
        if args.output_root is None or args.launch_id is None or args.owner_token is None:
            raise ValueError("release-writer requires output, launch, and owner")
        result = release_final_evaluation_writer(
            output_root=args.output_root,
            launch_id=args.launch_id,
            owner_token=args.owner_token,
        )
    else:
        required = {
            "packet_root": args.packet_root,
            "retry_packet_root": args.retry_packet_root,
            "amendment_packet_root": args.amendment_packet_root,
            "primary_packet_root": args.primary_packet_root,
            "expected_packet_tree_sha256": args.expected_packet_tree_sha256,
        }
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ValueError("missing V4 packet arguments: " + ", ".join(missing))
        common = {
            "packet_root": args.packet_root,
            "retry_packet_root": args.retry_packet_root,
            "amendment_packet_root": args.amendment_packet_root,
            "primary_packet_root": args.primary_packet_root,
            "expected_packet_tree_sha256": args.expected_packet_tree_sha256,
        }
        if args.mode == "packet":
            result = verify_hf_final_evaluation_v4_packet(**common)
        elif args.mode == "preserved-lane":
            if args.output_root is None:
                raise ValueError("preserved-lane requires --output-root")
            result = verify_preserved_final_ai_gold_for_v4(
                **common,
                output_root=args.output_root,
            )
        elif args.mode == "claim-launch":
            if any(
                value is None
                for value in (
                    args.output_root,
                    args.job_id,
                    args.launch_id,
                    args.owner_token,
                )
            ):
                raise ValueError("claim-launch requires output, job, launch, and owner")
            result = claim_final_evaluation_v4(
                **common,
                output_root=args.output_root,
                job_id=args.job_id,
                launch_id=args.launch_id,
                owner_token=args.owner_token,
            )
        else:
            if any(
                value is None
                for value in (
                    args.output_root,
                    args.external_journal_root,
                    args.job_id,
                    args.launch_id,
                )
            ):
                raise ValueError("runtime-complete requires output, external journal, job, and launch")
            result = record_v4_runtime_completion(
                **common,
                output_root=args.output_root,
                external_journal_root=args.external_journal_root,
                job_id=args.job_id,
                launch_id=args.launch_id,
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

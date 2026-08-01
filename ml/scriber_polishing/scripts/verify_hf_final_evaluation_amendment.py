from __future__ import annotations

import argparse
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation import release_final_evaluation_writer
from scriber_polishing.hf_final_evaluation_amendment import (
    claim_final_evaluation_amendment,
    finalize_terminal_amendment_completion,
    record_runtime_completion,
    recover_unbound_prediction_manifests,
    verify_hf_final_evaluation_amendment_packet,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify or operate one final-evaluation amendment")
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "packet",
            "claim-launch",
            "release-writer",
            "recover-unbound",
            "runtime-complete",
            "finalize-terminal",
        ),
    )
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--primary-packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--launch-id")
    parser.add_argument("--owner-token")
    parser.add_argument("--final-model", type=Path)
    parser.add_argument("--base-model", type=Path)
    parser.add_argument("--amendment-terminal-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _required(args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        raise ValueError("missing required arguments: " + ", ".join("--" + name.replace("_", "-") for name in missing))


def main() -> int:
    args = _parser().parse_args()
    if args.mode == "packet":
        _required(
            args,
            "packet_root",
            "primary_packet_root",
            "expected_packet_tree_sha256",
        )
        result = verify_hf_final_evaluation_amendment_packet(
            args.packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
        )
    elif args.mode == "claim-launch":
        _required(
            args,
            "packet_root",
            "primary_packet_root",
            "expected_packet_tree_sha256",
            "output_root",
            "job_id",
            "launch_id",
            "owner_token",
        )
        result = claim_final_evaluation_amendment(
            packet_root=args.packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            output_root=args.output_root,
            job_id=args.job_id,
            launch_id=args.launch_id,
            owner_token=args.owner_token,
        )
    elif args.mode == "release-writer":
        _required(args, "output_root", "launch_id", "owner_token")
        result = release_final_evaluation_writer(
            output_root=args.output_root,
            launch_id=args.launch_id,
            owner_token=args.owner_token,
        )
    elif args.mode == "recover-unbound":
        _required(
            args,
            "packet_root",
            "primary_packet_root",
            "expected_packet_tree_sha256",
            "output_root",
            "final_model",
            "base_model",
        )
        repairs = recover_unbound_prediction_manifests(
            packet_root=args.packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            output_root=args.output_root,
            final_model_root=args.final_model,
            base_model_root=args.base_model,
        )
        result = {
            "schema_version": 1,
            "kind": "scriber_hf_final_evaluation_unbound_prediction_recovery",
            "repairs": repairs,
        }
    elif args.mode == "runtime-complete":
        _required(
            args,
            "packet_root",
            "primary_packet_root",
            "expected_packet_tree_sha256",
            "output_root",
            "job_id",
            "launch_id",
        )
        result = record_runtime_completion(
            packet_root=args.packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            output_root=args.output_root,
            job_id=args.job_id,
            launch_id=args.launch_id,
        )
    else:
        _required(
            args,
            "packet_root",
            "primary_packet_root",
            "expected_packet_tree_sha256",
            "output_root",
            "amendment_terminal_evidence",
            "output",
        )
        result = finalize_terminal_amendment_completion(
            packet_root=args.packet_root,
            primary_packet_root=args.primary_packet_root,
            expected_packet_tree_sha256=args.expected_packet_tree_sha256,
            output_root=args.output_root,
            amendment_terminal_evidence_path=args.amendment_terminal_evidence,
            output_path=args.output,
        )
    print(canonical_json_bytes(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

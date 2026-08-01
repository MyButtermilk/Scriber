"""Prepare, but never launch, the private cumulative-1100 HF evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import canonical_json_bytes  # noqa: E402
from scriber_polishing.hf_final_evaluation import (  # noqa: E402
    build_hf_final_evaluation_launch_contract,
    build_hf_final_evaluation_packet,
)


def _job_evidence(values: list[str]) -> dict[str, object]:
    job_id, flavor, terminal_status, running_seconds, packet_tree_sha256 = values
    try:
        parsed_seconds = int(running_seconds)
    except ValueError as error:
        raise argparse.ArgumentTypeError("prior-job running seconds must be an integer") from error
    return {
        "job_id": job_id,
        "flavor": flavor,
        "terminal_status": terminal_status,
        "running_seconds": parsed_seconds,
        "packet_tree_sha256": packet_tree_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--continuation-result", type=Path, required=True)
    parser.add_argument("--continuation-job-id", required=True)
    parser.add_argument("--continuation-running-seconds", type=int, required=True)
    parser.add_argument(
        "--expected-continuation-packet-tree-sha256",
        required=True,
    )
    parser.add_argument("--remote-model-uri", required=True)
    parser.add_argument("--finalization-job-id")
    parser.add_argument("--finalization-running-seconds", type=int)
    parser.add_argument("--expected-finalization-packet-tree-sha256")
    parser.add_argument(
        "--failed-evaluation",
        action="append",
        nargs=5,
        required=True,
        metavar=("JOB_ID", "FLAVOR", "STATUS", "SECONDS", "PACKET_SHA256"),
    )
    parser.add_argument(
        "--auxiliary-diagnostic",
        action="append",
        nargs=5,
        required=True,
        metavar=("JOB_ID", "FLAVOR", "STATUS", "SECONDS", "PACKET_SHA256"),
    )
    parser.add_argument(
        "--launch-kind",
        choices=("cpu-self-verify", "gpu-evaluation"),
        required=True,
    )
    parser.add_argument(
        "--gpu-flavor",
        choices=("l4x1", "a10g-small"),
        default="l4x1",
        help="GPU flavor bound into the immutable evaluation packet (default: l4x1)",
    )
    parser.add_argument("--total-budget-usd", default="5.000")
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--launch-contract-output", type=Path, required=True)
    args = parser.parse_args(argv)

    packet_output = args.output_dir.expanduser().resolve()
    launch_output = args.launch_contract_output.expanduser().resolve()
    if packet_output.exists() or args.output_dir.is_symlink():
        raise ValueError(f"refusing to overwrite final-evaluation packet: {packet_output}")
    if launch_output.exists() or args.launch_contract_output.is_symlink():
        raise ValueError(f"refusing to overwrite final-evaluation launch contract: {launch_output}")
    if launch_output == packet_output or launch_output.is_relative_to(packet_output):
        raise ValueError("final-evaluation launch contract must be outside the immutable packet")

    packet = build_hf_final_evaluation_packet(
        continuation_result_path=args.continuation_result,
        continuation_job_id=args.continuation_job_id,
        continuation_running_seconds=args.continuation_running_seconds,
        expected_continuation_packet_tree_sha256=(args.expected_continuation_packet_tree_sha256),
        remote_model_uri=args.remote_model_uri,
        total_budget_usd=args.total_budget_usd,
        reference_root=args.reference_root,
        output_dir=args.output_dir,
        finalization_job_id=args.finalization_job_id,
        finalization_running_seconds=args.finalization_running_seconds,
        expected_finalization_packet_tree_sha256=(args.expected_finalization_packet_tree_sha256),
        failed_evaluations=[_job_evidence(values) for values in args.failed_evaluation],
        auxiliary_diagnostic_jobs=[_job_evidence(values) for values in args.auxiliary_diagnostic],
        evaluation_flavor=args.gpu_flavor,
    )
    launch = build_hf_final_evaluation_launch_contract(
        packet.output_dir,
        launch_kind=args.launch_kind,
    )
    launch_output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with launch_output.open("xb") as handle:
            handle.write(canonical_json_bytes(launch))
    except FileExistsError as error:
        raise ValueError(f"refusing to overwrite final-evaluation launch contract: {launch_output}") from error
    print(
        json.dumps(
            {
                "packet": dict(packet.manifest),
                "launch": launch,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

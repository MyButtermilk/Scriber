"""Prepare, but never upload or launch, the private five-variant HF Job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_quantization_pipeline import (  # noqa: E402
    build_hf_quantization_launch_contract,
    build_hf_quantization_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-evaluation-result", type=Path, required=True)
    parser.add_argument("--final-evaluation-manifest", type=Path, required=True)
    parser.add_argument("--training-report", type=Path, required=True)
    parser.add_argument(
        "--actual-cost-evidence",
        type=Path,
        required=True,
        help=("Fresh canonical v2 evidence produced locally by scripts/build_hf_actual_cost_evidence.py."),
    )
    parser.add_argument("--remote-model-uri", required=True)
    parser.add_argument("--model-weight-bytes", type=int, required=True)
    parser.add_argument("--llama-cpp-bundle", type=Path)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path)
    parser.add_argument(
        "--llama-cpp-materialization-result",
        type=Path,
        help=(
            "Canonical cloud-produced toolchain result; avoids downloading the "
            "just-produced toolchain payload."
        ),
    )
    parser.add_argument("--llama-cpp-materialization-result-sha256")
    parser.add_argument("--llama-cpp-materialization-job-id")
    parser.add_argument("--llama-cpp-launch-claim-commit")
    parser.add_argument("--llama-cpp-launch-claim-owner")
    parser.add_argument("--remote-llama-cpp-uri", required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--offline-wheelhouse", type=Path, required=True)
    parser.add_argument("--offline-wheelhouse-lock", type=Path, required=True)
    parser.add_argument(
        "--final-evaluation-config",
        type=Path,
        help=(
            "Exact evaluation.yaml from the verified full-model evaluation packet; "
            "required for the full-model path."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--remote-packet-uri", required=True)
    parser.add_argument("--launch-contract-output", type=Path, required=True)
    args = parser.parse_args(argv)

    packet = build_hf_quantization_packet(
        final_evaluation_result_path=args.final_evaluation_result,
        final_evaluation_manifest_path=args.final_evaluation_manifest,
        training_report_path=args.training_report,
        actual_cost_evidence_path=args.actual_cost_evidence,
        remote_model_uri=args.remote_model_uri,
        model_weight_bytes=args.model_weight_bytes,
        llama_cpp_bundle_root=args.llama_cpp_bundle,
        llama_cpp_bundle_lock=args.llama_cpp_bundle_lock,
        remote_llama_cpp_uri=args.remote_llama_cpp_uri,
        reference_root=args.reference_root,
        offline_wheelhouse_dir=args.offline_wheelhouse,
        offline_wheelhouse_lock_path=args.offline_wheelhouse_lock,
        output_dir=args.output_dir,
        final_evaluation_config_path=args.final_evaluation_config,
        llama_cpp_materialization_result_path=args.llama_cpp_materialization_result,
        llama_cpp_materialization_result_sha256=(
            args.llama_cpp_materialization_result_sha256
        ),
        llama_cpp_materialization_job_id=args.llama_cpp_materialization_job_id,
        llama_cpp_launch_claim_commit=args.llama_cpp_launch_claim_commit,
        llama_cpp_launch_claim_owner=args.llama_cpp_launch_claim_owner,
    )
    launch = build_hf_quantization_launch_contract(
        packet.output_dir,
        remote_packet_uri=args.remote_packet_uri,
    )
    if args.launch_contract_output.exists():
        raise ValueError(f"refusing to overwrite HF quantization launch contract: {args.launch_contract_output}")
    args.launch_contract_output.parent.mkdir(parents=True, exist_ok=True)
    args.launch_contract_output.write_text(
        json.dumps(launch, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
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

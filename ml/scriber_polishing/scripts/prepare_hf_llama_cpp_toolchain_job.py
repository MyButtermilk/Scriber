"""Prepare, but never upload or launch, the pinned HF llama.cpp cloud toolchain job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_llama_cpp_toolchain import (  # noqa: E402
    TOOLCHAIN_FLAVOR,
    build_hf_llama_cpp_toolchain_launch_contract,
    build_hf_llama_cpp_toolchain_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--oci-cache", type=Path, required=True)
    parser.add_argument("--actual-cost-evidence", type=Path, required=True)
    parser.add_argument("--offline-wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--remote-packet-uri", required=True)
    parser.add_argument("--launch-contract-output", type=Path, required=True)
    parser.add_argument(
        "--flavor",
        choices=(TOOLCHAIN_FLAVOR,),
        default=TOOLCHAIN_FLAVOR,
    )
    args = parser.parse_args(argv)

    packet = build_hf_llama_cpp_toolchain_packet(
        source_root=args.source_root,
        oci_cache_dir=args.oci_cache,
        actual_cost_evidence_path=args.actual_cost_evidence,
        offline_wheelhouse_dir=args.offline_wheelhouse,
        output_dir=args.output_dir,
        flavor=args.flavor,
    )
    launch = build_hf_llama_cpp_toolchain_launch_contract(
        packet.output_dir,
        remote_packet_uri=args.remote_packet_uri,
    )
    if args.launch_contract_output.exists() or args.launch_contract_output.is_symlink():
        raise ValueError(f"refusing to overwrite toolchain launch contract: {args.launch_contract_output}")
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

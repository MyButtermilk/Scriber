"""Verify or materialize the pinned private HF llama.cpp b10156 CUDA toolchain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_llama_cpp_toolchain import (  # noqa: E402
    HfLlamaCppToolchainError,
    materialize_hf_llama_cpp_toolchain,
    verify_hf_llama_cpp_toolchain_packet,
    verify_hf_llama_cpp_toolchain_result,
)


def _required(value: object, option: str) -> object:
    if value is None:
        raise HfLlamaCppToolchainError(f"{option} is required for this mode")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("packet", "materialize", "result"))
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--owner-token")
    parser.add_argument("--bundle-root", type=Path)
    args = parser.parse_args(argv)

    expected = str(
        _required(
            args.expected_packet_tree_sha256,
            "--expected-packet-tree-sha256",
        )
    )
    if args.mode == "packet":
        value = verify_hf_llama_cpp_toolchain_packet(
            _required(args.packet_root, "--packet-root"),
            expected_packet_tree_sha256=expected,
        )
    elif args.mode == "materialize":
        value = materialize_hf_llama_cpp_toolchain(
            _required(args.packet_root, "--packet-root"),
            expected_packet_tree_sha256=expected,
            owner_token=str(_required(args.owner_token, "--owner-token")),
            workspace_root=_required(args.workspace_root, "--workspace-root"),
            output_root=_required(args.output_root, "--output-root"),
        )
    else:
        value = verify_hf_llama_cpp_toolchain_result(
            _required(args.bundle_root, "--bundle-root"),
            expected_packet_tree_sha256=expected,
        )
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

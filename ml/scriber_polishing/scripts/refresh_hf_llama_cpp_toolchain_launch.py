"""Refresh only the short-lived launch authorization for an immutable toolchain packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_llama_cpp_toolchain import (  # noqa: E402
    build_refreshed_hf_llama_cpp_toolchain_launch_contract,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--actual-cost-evidence", type=Path, required=True)
    parser.add_argument("--remote-packet-uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    launch = build_refreshed_hf_llama_cpp_toolchain_launch_contract(
        args.packet_dir,
        actual_cost_evidence_path=args.actual_cost_evidence,
        remote_packet_uri=args.remote_packet_uri,
    )
    if args.output.exists() or args.output.is_symlink():
        raise ValueError(f"refusing to overwrite refreshed launch contract: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(launch, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(launch, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

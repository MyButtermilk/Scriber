"""Build and locally verify the immutable attempt15 HF conversion packet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_release_job import (  # noqa: E402
    package_v2_release_job,
    verify_v2_release_job_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--evaluation-binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    packet = package_v2_release_job(
        repository_root=args.repository_root,
        plan_path=args.plan,
        evaluation_binding_path=args.evaluation_binding,
        output_dir=args.output,
    )
    verification = verify_v2_release_job_packet(
        args.output,
        expected_packet_tree_sha256=str(packet["packet_tree_sha256"]),
        expected_manifest_sha256=str(packet["manifest_sha256"]),
        expected_launch_contract_sha256=str(packet["launch_contract_sha256"]),
    )
    print(
        json.dumps(
            {
                **packet,
                "packet_dir": str(packet["packet_dir"]),
                "verification": verification,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

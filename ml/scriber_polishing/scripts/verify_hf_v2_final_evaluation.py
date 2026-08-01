"""Verify a V2 compact-evaluation packet, remote preflight, or sealed result."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_v2_final_evaluation import (  # noqa: E402
    V2FinalEvaluationError,
    verify_v2_final_evaluation_launch_readiness,
    verify_v2_final_evaluation_packet,
    verify_v2_final_evaluation_result,
)


def _required(value: object, option: str) -> object:
    if value is None:
        raise V2FinalEvaluationError(f"{option} is required for this mode")
    return value


def _object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2FinalEvaluationError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise V2FinalEvaluationError(f"{label} must contain an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("packet", "launch-readiness", "result"), required=True)
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--expected-packet-tree-sha256")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-launch-contract-sha256")
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--jobs-snapshot", type=Path)
    parser.add_argument("--output-snapshot", type=Path)
    parser.add_argument("--remote-packet-snapshot", type=Path)
    parser.add_argument("--now-utc")
    parser.add_argument("--packet-manifest", type=Path)
    parser.add_argument("--result-root", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "packet":
        output = verify_v2_final_evaluation_packet(
            _required(args.packet_root, "--packet-root"),
            expected_packet_tree_sha256=str(
                _required(args.expected_packet_tree_sha256, "--expected-packet-tree-sha256")
            ),
            expected_manifest_sha256=str(_required(args.expected_manifest_sha256, "--expected-manifest-sha256")),
            expected_launch_contract_sha256=str(
                _required(args.expected_launch_contract_sha256, "--expected-launch-contract-sha256")
            ),
        )
    elif args.mode == "launch-readiness":
        output = verify_v2_final_evaluation_launch_readiness(
            contract_path=_required(args.contract, "--contract"),
            jobs_snapshot=_object(
                _required(args.jobs_snapshot, "--jobs-snapshot"),  # type: ignore[arg-type]
                "jobs snapshot",
            ),
            output_snapshot=_object(
                _required(args.output_snapshot, "--output-snapshot"),  # type: ignore[arg-type]
                "output snapshot",
            ),
            packet_snapshot=_object(
                _required(args.remote_packet_snapshot, "--remote-packet-snapshot"),  # type: ignore[arg-type]
                "remote packet snapshot",
            ),
            now_utc=str(_required(args.now_utc, "--now-utc")),
        )
    else:
        output = verify_v2_final_evaluation_result(
            packet_manifest_path=_required(args.packet_manifest, "--packet-manifest"),
            result_root=_required(args.result_root, "--result-root"),
            result_path=_required(args.result, "--result"),
        )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

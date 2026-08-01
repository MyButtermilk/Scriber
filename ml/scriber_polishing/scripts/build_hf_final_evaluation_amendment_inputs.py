from __future__ import annotations

import argparse
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_amendment import (
    build_amendment_authorization_evidence,
    build_terminal_predecessor_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build truthful canonical evidence for one CANCELED final-evaluation amendment"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--last-observed-running-seconds", required=True, type=int)
    parser.add_argument("--primary-packet-tree-sha256", required=True)
    parser.add_argument("--authorization-source-statement", required=True)
    parser.add_argument("--terminal-predecessor-output", required=True, type=Path)
    parser.add_argument("--authorization-output", required=True, type=Path)
    return parser


def _write_new(path: Path, value: object) -> None:
    candidate = path.expanduser()
    if candidate.is_symlink() or candidate.exists():
        raise ValueError(f"refusing to overwrite amendment evidence: {candidate}")
    destination = candidate.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(canonical_json_bytes(value))
        stream.flush()


def main() -> int:
    args = _parser().parse_args()
    predecessor = build_terminal_predecessor_evidence(
        job_id=args.job_id,
        last_observed_running_seconds=args.last_observed_running_seconds,
        packet_tree_sha256=args.primary_packet_tree_sha256,
    )
    authorization = build_amendment_authorization_evidence(
        args.authorization_source_statement,
    )
    _write_new(args.terminal_predecessor_output, predecessor)
    _write_new(args.authorization_output, authorization)
    print(f"terminal_predecessor={args.terminal_predecessor_output.resolve()}")
    print(f"authorization={args.authorization_output.resolve()}")
    print("actual_predecessor_cost_status=unknown")
    print("primary_budget_accounting=full_timeout_reservation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

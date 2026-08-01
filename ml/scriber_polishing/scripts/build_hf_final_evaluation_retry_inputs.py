from __future__ import annotations

import argparse
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_amendment import (
    verify_hf_final_evaluation_amendment_packet,
)
from scriber_polishing.hf_final_evaluation_retry import (
    build_failed_amendment_terminal_evidence,
    build_no_active_jobs_evidence,
    build_retry_authorization_evidence,
    build_stale_writer_lock_observation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build sanitized canonical evidence for the exact final-evaluation writer-lock recovery and replacement"
        )
    )
    parser.add_argument("--primary-packet-root", required=True, type=Path)
    parser.add_argument("--amendment-packet-root", required=True, type=Path)
    parser.add_argument("--expected-primary-packet-tree-sha256", required=True)
    parser.add_argument("--expected-amendment-packet-tree-sha256", required=True)
    parser.add_argument("--stale-owner-json", required=True, type=Path)
    parser.add_argument("--expected-stale-owner-sha256", required=True)
    parser.add_argument("--failed-job-observed-at-utc", required=True)
    parser.add_argument("--no-active-jobs-observed-at-utc", required=True)
    parser.add_argument("--stale-owner-observed-at-utc", required=True)
    parser.add_argument("--authorization-source-statement", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def _write_new(path: Path, value: object) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError(f"refusing to overwrite retry evidence: {path}")
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(value))
        stream.flush()


def main() -> int:
    args = _parser().parse_args()
    if args.output_dir.is_symlink() or args.output_dir.exists():
        raise ValueError(f"refusing to overwrite retry evidence directory: {args.output_dir}")
    amendment = verify_hf_final_evaluation_amendment_packet(
        args.amendment_packet_root,
        primary_packet_root=args.primary_packet_root,
        expected_packet_tree_sha256=args.expected_amendment_packet_tree_sha256,
    )
    if amendment["primary_packet"]["packet_tree_sha256"] != args.expected_primary_packet_tree_sha256:
        raise ValueError("amendment packet binds a different primary packet")
    primary_terminal = amendment["terminal_predecessor"]
    failed = build_failed_amendment_terminal_evidence(
        observed_at_utc=args.failed_job_observed_at_utc,
        primary_packet_tree_sha256=args.expected_primary_packet_tree_sha256,
        amendment_packet_tree_sha256=args.expected_amendment_packet_tree_sha256,
    )
    no_active = build_no_active_jobs_evidence(
        observed_at_utc=args.no_active_jobs_observed_at_utc,
    )
    authorization = build_retry_authorization_evidence(
        args.authorization_source_statement,
    )
    stale = build_stale_writer_lock_observation(
        owner_path=args.stale_owner_json,
        expected_owner_sha256=args.expected_stale_owner_sha256,
        observed_at_utc=args.stale_owner_observed_at_utc,
        primary_terminal_evidence=primary_terminal,
        failed_amendment_evidence=failed,
        no_active_jobs_evidence=no_active,
        output_snapshot_tree_sha256=str(amendment["output"]["snapshot"]["tree_sha256"]),
    )
    args.output_dir.mkdir(parents=True)
    outputs = {
        "failed_amendment_terminal": (
            args.output_dir / "failed-amendment-terminal.json",
            failed,
        ),
        "no_active_jobs": (args.output_dir / "no-active-jobs.json", no_active),
        "stale_writer_lock": (
            args.output_dir / "stale-writer-lock.json",
            stale,
        ),
        "authorization": (args.output_dir / "authorization.json", authorization),
    }
    for path, value in outputs.values():
        _write_new(path, value)
    for label, (path, _value) in outputs.items():
        print(f"{label}={path.resolve()}")
    print("owner_token_persisted=false")
    print("external_job_started=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

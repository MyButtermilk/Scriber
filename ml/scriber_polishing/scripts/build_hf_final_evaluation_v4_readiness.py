#!/usr/bin/env python3
"""Materialize a canonical V4 launch-readiness report without launching."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_v4 import (
    OVERALL_MAXIMUM_USD,
    OVERALL_REMAINING_USD,
    build_hf_final_evaluation_v4_launch_contract,
    verify_hf_final_evaluation_v4_packet,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_object(path: Path) -> tuple[bytes, dict[str, object]]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise ValueError(f"readiness input is not canonical JSON: {path}")
    return payload, value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-packet-root", type=Path, required=True)
    parser.add_argument("--amendment-packet-root", type=Path, required=True)
    parser.add_argument("--retry-packet-root", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--launch-contract", type=Path, required=True)
    parser.add_argument("--bash", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads((args.packet_root / "job-manifest.json").read_bytes())
    verified = verify_hf_final_evaluation_v4_packet(
        args.packet_root,
        retry_packet_root=args.retry_packet_root,
        amendment_packet_root=args.amendment_packet_root,
        primary_packet_root=args.primary_packet_root,
        expected_packet_tree_sha256=manifest["packet_tree_sha256"],
    )
    contract_payload, contract = _canonical_object(args.launch_contract)
    expected_contract = build_hf_final_evaluation_v4_launch_contract(
        args.packet_root,
        retry_packet_root=args.retry_packet_root,
        amendment_packet_root=args.amendment_packet_root,
        primary_packet_root=args.primary_packet_root,
    )
    if contract != expected_contract:
        raise ValueError("launch contract differs from the verified V4 packet")
    runner = args.packet_root / "run-final-evaluation-v4.sh"
    syntax = subprocess.run(
        [str(args.bash), "-n", str(runner)],
        check=False,
        capture_output=True,
        text=True,
    )
    if syntax.returncode != 0:
        raise ValueError(f"V4 runner syntax failed: {syntax.stderr}")
    report = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_launch_readiness",
        "status": "ready_after_immediate_live_prelaunch_recheck",
        "packet_verified": True,
        "packet_tree_sha256": verified["packet_tree_sha256"],
        "runner_sha256": verified["runner_sha256"],
        "runner_bash_syntax": "passed",
        "launch_contract_sha256": _sha256(contract_payload),
        "launch_contract_matches_packet": True,
        "final_ai_gold_action": contract["final_ai_gold_action"],
        "preserved_final_ai_gold_prediction_sha256": contract["preserved_final_ai_gold_prediction_sha256"],
        "external_journal_root": contract["external_journal_root"],
        "external_cleanup": contract["external_cleanup"],
        "overall_maximum_usd": OVERALL_MAXIMUM_USD,
        "overall_remaining_usd": OVERALL_REMAINING_USD,
        "strictly_below_overall_budget": True,
        "training_steps_executed": 0,
        "publication_allowed": False,
        "external_job_started": False,
        "remote_mutation_performed": False,
        "required_immediate_live_prelaunch_checks": [
            "no_active_hf_jobs",
            "result_tree_equals_bound_post_removal_snapshot",
            "v3_ledger_matches_bound_sha256",
            "writer_lock_absent",
            "v3_runtime_completion_absent",
            "v4_ledger_absent",
            "v4_runtime_completion_absent",
            "external_v4_journal_root_absent",
        ],
    }
    output = args.output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ValueError(f"refusing to overwrite readiness report: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(report))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build canonical V4 job inputs from verified V3 and Bucket evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_retry import (
    _launch_id,
    _validate_v3_ledger,
    build_no_active_jobs_evidence,
    verify_hf_final_evaluation_retry_packet,
)
from scriber_polishing.hf_final_evaluation_v4 import (
    V3_JOB_ID,
    V3_PACKET_TREE_SHA256,
    build_v3_terminal_evidence,
    build_v4_authorization_evidence,
    validate_post_removal_proof,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise ValueError(f"V4 source is not a canonical JSON object: {path}")
    return payload, value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-packet-root", type=Path, required=True)
    parser.add_argument("--amendment-packet-root", type=Path, required=True)
    parser.add_argument("--retry-packet-root", type=Path, required=True)
    parser.add_argument("--recovery-input-dir", type=Path, required=True)
    parser.add_argument("--post-removal-proof", type=Path, required=True)
    parser.add_argument("--v3-ledger", type=Path, required=True)
    parser.add_argument("--v3-terminal-observed-at-utc", required=True)
    parser.add_argument("--no-active-observed-at-utc", required=True)
    parser.add_argument("--authorization-statement", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    v3_manifest = verify_hf_final_evaluation_retry_packet(
        args.retry_packet_root,
        amendment_packet_root=args.amendment_packet_root,
        primary_packet_root=args.primary_packet_root,
        expected_packet_tree_sha256=V3_PACKET_TREE_SHA256,
    )
    ledger_payload, ledger = _canonical_object(args.v3_ledger)
    _validate_v3_ledger(
        ledger,
        manifest=v3_manifest,
        current_job_id=V3_JOB_ID,
        current_launch_id=_launch_id(V3_JOB_ID),
    )
    evidence_payload, evidence = _canonical_object(args.recovery_input_dir / "stale-journal-evidence.json")
    plan_payload, plan = _canonical_object(args.recovery_input_dir / "exact-removal-plan.json")
    post_payload, post = _canonical_object(args.post_removal_proof)
    validate_post_removal_proof(post, evidence=evidence, removal_plan=plan)

    terminal = build_v3_terminal_evidence(observed_at_utc=args.v3_terminal_observed_at_utc)
    no_active = build_no_active_jobs_evidence(observed_at_utc=args.no_active_observed_at_utc)
    authorization = build_v4_authorization_evidence(args.authorization_statement)
    sources = {
        "v3-terminal.json": canonical_json_bytes(terminal),
        "no-active-jobs.json": canonical_json_bytes(no_active),
        "authorization.json": canonical_json_bytes(authorization),
        "launch-ledger-v3.json": ledger_payload,
        "stale-journal-evidence.json": evidence_payload,
        "exact-removal-plan.json": plan_payload,
        "post-removal-proof.json": post_payload,
    }
    files = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
        for name, payload in sorted(sources.items())
    ]
    input_manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_job_inputs",
        "status": "validated",
        "v3_packet_tree_sha256": V3_PACKET_TREE_SHA256,
        "v3_job_id": V3_JOB_ID,
        "v3_launch_id": _launch_id(V3_JOB_ID),
        "files": files,
        "post_removal_tree_sha256": post["snapshot"]["tree_sha256"],
        "v3_ledger_sha256": _sha256(ledger_payload),
        "v3_terminal_sha256": _sha256(sources["v3-terminal.json"]),
        "authorization_sha256": _sha256(sources["authorization.json"]),
        "no_active_jobs_sha256": _sha256(sources["no-active-jobs.json"]),
        "remote_mutation_performed": False,
        "gpu_job_started": False,
    }
    target = args.output_dir.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite V4 job inputs: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        for name, payload in sources.items():
            (temporary / name).write_bytes(payload)
        (temporary / "input-manifest.json").write_bytes(canonical_json_bytes(input_manifest))
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(
        json.dumps(
            {
                "status": "validated",
                "output_dir": str(target),
                "input_manifest_sha256": _sha256(canonical_json_bytes(input_manifest)),
                "v3_ledger_sha256": input_manifest["v3_ledger_sha256"],
                "v3_terminal_sha256": input_manifest["v3_terminal_sha256"],
                "no_active_jobs_sha256": input_manifest["no_active_jobs_sha256"],
                "authorization_sha256": input_manifest["authorization_sha256"],
                "gpu_job_started": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

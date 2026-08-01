#!/usr/bin/env python3
"""Verify V4 pre/post-removal evidence without mutating Hugging Face state."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_v4 import (
    build_exact_stale_journal_removal_plan,
    validate_observed_v3_final_ai_gold_stale_journal,
    verify_exact_stale_journal_removal,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise ValueError(f"V4 recovery input is not a canonical JSON object: {path}")
    return payload, value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("pre-removal", "post-removal"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--observed-at-utc")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    evidence_payload, evidence = _canonical_object(args.input_dir / "stale-journal-evidence.json")
    plan_payload, plan = _canonical_object(args.input_dir / "exact-removal-plan.json")
    input_manifest_payload, input_manifest = _canonical_object(args.input_dir / "input-manifest.json")
    expected_files = [
        {
            "path": "stale-journal-evidence.json",
            "bytes": len(evidence_payload),
            "sha256": _sha256(evidence_payload),
        },
        {
            "path": "exact-removal-plan.json",
            "bytes": len(plan_payload),
            "sha256": _sha256(plan_payload),
        },
    ]
    if (
        input_manifest.get("kind") != "scriber_hf_final_evaluation_v4_recovery_inputs"
        or input_manifest.get("files") != expected_files
        or input_manifest.get("recursive") is not False
        or input_manifest.get("target_count") != 1
    ):
        raise ValueError("V4 recovery input manifest differs from its canonical files")

    if args.mode == "pre-removal":
        actual_evidence = validate_observed_v3_final_ai_gold_stale_journal(
            manifest_path=args.manifest,
            lane_root=args.snapshot_root / "final/ai_gold",
        )
        actual_plan = build_exact_stale_journal_removal_plan(
            snapshot_root=args.snapshot_root,
            evidence=actual_evidence,
        )
        if evidence != actual_evidence or plan != actual_plan:
            raise ValueError("V4 pre-removal inputs differ from the fresh read-only snapshot")
        result: dict[str, object] = {
            "status": "passed",
            "mode": args.mode,
            "remote_mutation_performed": False,
            "target_count": 1,
            "recursive": False,
            "target": plan["target"],
            "pre_removal_tree_sha256": plan["pre_removal_snapshot"]["tree_sha256"],
            "expected_post_removal_tree_sha256": plan["expected_post_removal_snapshot"]["tree_sha256"],
        }
    else:
        if args.observed_at_utc is None:
            raise ValueError("--observed-at-utc is required for post-removal verification")
        result = verify_exact_stale_journal_removal(
            manifest_path=args.manifest,
            snapshot_root=args.snapshot_root,
            evidence=evidence,
            removal_plan=plan,
            observed_at_utc=args.observed_at_utc,
        )
    result["input_manifest_sha256"] = _sha256(input_manifest_payload)
    result["stale_journal_evidence_sha256"] = _sha256(evidence_payload)
    result["exact_removal_plan_sha256"] = _sha256(plan_payload)
    if args.output is not None:
        output = args.output.expanduser().resolve()
        if output.exists() or output.is_symlink():
            raise ValueError(f"refusing to overwrite V4 recovery proof: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

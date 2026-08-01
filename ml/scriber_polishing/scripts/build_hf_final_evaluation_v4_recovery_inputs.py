#!/usr/bin/env python3
"""Materialize canonical read-only evidence for the exact V3 journal cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation_v4 import (
    build_exact_stale_journal_removal_plan,
    validate_observed_v3_final_ai_gold_stale_journal,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    target = args.output_dir.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite V4 recovery inputs: {target}")
    evidence = validate_observed_v3_final_ai_gold_stale_journal(
        manifest_path=args.manifest,
        lane_root=args.snapshot_root / "final/ai_gold",
    )
    plan = build_exact_stale_journal_removal_plan(
        snapshot_root=args.snapshot_root,
        evidence=evidence,
    )
    evidence_payload = canonical_json_bytes(evidence)
    plan_payload = canonical_json_bytes(plan)
    input_manifest = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_recovery_inputs",
        "status": "validated_pre_removal",
        "remote_mutation_performed": False,
        "files": [
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
        ],
        "pre_removal_tree_sha256": plan["pre_removal_snapshot"]["tree_sha256"],
        "expected_post_removal_tree_sha256": plan["expected_post_removal_snapshot"]["tree_sha256"],
        "exact_remote_target": plan["target"],
        "recursive": False,
        "target_count": 1,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        (temporary / "stale-journal-evidence.json").write_bytes(evidence_payload)
        (temporary / "exact-removal-plan.json").write_bytes(plan_payload)
        (temporary / "input-manifest.json").write_bytes(canonical_json_bytes(input_manifest))
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(
        json.dumps(
            {
                "status": "validated_pre_removal",
                "output_dir": str(target),
                "input_manifest_sha256": _sha256(canonical_json_bytes(input_manifest)),
                "stale_journal_evidence_sha256": _sha256(evidence_payload),
                "exact_removal_plan_sha256": _sha256(plan_payload),
                "remote_mutation_performed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

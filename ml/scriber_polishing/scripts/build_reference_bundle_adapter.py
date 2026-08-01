"""Bind an existing canonical evaluation-reference JSONL to the current bundle schema."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scriber_polishing.evaluation_bundle import validate_evaluation_bundle_manifest
from scriber_polishing.evaluation_io import validate_evaluation_reference


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = args.records.read_bytes()
    source_manifest_payload = args.source_manifest.read_bytes()
    rows = []
    for line in payload.decode("utf-8").splitlines():
        rows.append(validate_evaluation_reference(json.loads(line)))
    if not rows or payload != b"".join(_canonical(row) for row in rows):
        raise ValueError("reference JSONL is empty or noncanonical")
    case_ids_hash = hashlib.sha256(_canonical([row["case_id"] for row in rows])).hexdigest()
    manifest = validate_evaluation_bundle_manifest(
        {
            "schema_version": 1,
            "kind": "scriber_evaluation_reference_bundle",
            "output_filename": args.records.name,
            "record_count": len(rows),
            "records_sha256": hashlib.sha256(payload).hexdigest(),
            "case_ids_sha256": case_ids_hash,
            "split_counts": dict(sorted(Counter(str(row["split"]) for row in rows).items())),
            "inputs": [
                {
                    "position": 0,
                    "records_filename": args.records.name,
                    "records_sha256": hashlib.sha256(payload).hexdigest(),
                    "record_count": len(rows),
                    "case_ids_sha256": case_ids_hash,
                    "manifest_filename": args.source_manifest.name,
                    "manifest_sha256": hashlib.sha256(source_manifest_payload).hexdigest(),
                }
            ],
        }
    )
    if args.output.exists():
        raise ValueError(f"refusing to overwrite {args.output}")
    args.output.write_bytes(_canonical(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

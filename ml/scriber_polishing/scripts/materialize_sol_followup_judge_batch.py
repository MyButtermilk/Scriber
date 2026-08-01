"""Materialize the next bounded batch from a hash-bound Sol follow-up packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.judge_bundle import (  # noqa: E402
    materialize_judge_stage_packet,
    validate_judge_stage_packet_manifest,
    validate_judge_stage_resume_manifest,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    if raw != (_canonical_json(value) + "\n").encode("utf-8"):
        raise ValueError(f"{label} must use canonical JSON bytes: {path}")
    return value


def _load_canonical_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank JSONL line in {path} at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} row must be an object in {path} at {line_number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} file is empty: {path}")
    canonical = ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8")
    if raw != canonical:
        raise ValueError(f"{label} must use canonical JSONL bytes: {path}")
    return rows, raw


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--full-packet", type=Path, required=True)
    parser.add_argument("--full-packet-manifest", type=Path, required=True)
    parser.add_argument("--resume-manifest", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.max_cases < 1:
        raise ValueError("--max-cases must be positive")
    if args.output.resolve(strict=False) == args.output_manifest.resolve(strict=False):
        raise ValueError("batch packet and manifest outputs must be distinct")

    source_rows, source_bytes = _load_canonical_jsonl(args.source_bundle, "source judge bundle")
    full_rows, full_bytes = _load_canonical_jsonl(args.full_packet, "full Sol follow-up packet")
    full_manifest = validate_judge_stage_packet_manifest(
        _load_canonical_json(args.full_packet_manifest, "full Sol follow-up packet manifest")
    )
    resume = validate_judge_stage_resume_manifest(
        _load_canonical_json(args.resume_manifest, "Sol follow-up resume manifest")
    )
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    full_hash = hashlib.sha256(full_bytes).hexdigest()
    if full_manifest["judge_stage"] != "sol_release":
        raise ValueError("full packet is not a Sol release packet")
    if full_manifest["reviewed_bundle_sha256"] != source_hash:
        raise ValueError("full packet is bound to another source bundle")
    if full_manifest["stage_bundle_sha256"] != full_hash:
        raise ValueError("full packet bytes differ from its manifest")
    if resume["status"] != "in_progress":
        raise ValueError("Sol follow-up resume manifest is not in progress")
    if resume["judge_stage"] != "sol_release":
        raise ValueError("resume manifest is not for Sol release")
    if resume["reviewed_bundle_sha256"] != source_hash:
        raise ValueError("resume manifest is bound to another source bundle")
    if resume["stage_assignment_sha256"] != full_manifest["stage_assignment_sha256"]:
        raise ValueError("resume manifest is bound to another Sol assignment")
    if resume["prompt_hash"] != full_manifest["prompt_hash"]:
        raise ValueError("resume manifest is bound to another Sol prompt")

    assignment_ids = [row["case_id"] for row in full_rows]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise ValueError("full Sol follow-up packet contains duplicate case IDs")
    if resume["expected_count"] != len(assignment_ids):
        raise ValueError("resume expected count differs from full Sol packet")
    missing_ids = list(resume["missing_case_ids"])
    if resume["missing_count"] != len(missing_ids):
        raise ValueError("resume missing count is inconsistent")
    if not missing_ids or not set(missing_ids).issubset(assignment_ids):
        raise ValueError("resume missing IDs are empty or outside the Sol assignment")

    source_order = {row["case_id"]: index for index, row in enumerate(source_rows)}
    if not set(assignment_ids).issubset(source_order):
        raise ValueError("Sol assignment references a case outside the source bundle")
    next_ids = sorted(missing_ids, key=source_order.__getitem__)[: args.max_cases]
    packet = materialize_judge_stage_packet(
        source_rows,
        judge_stage="sol_release",
        public_case_ids=next_ids,
        assignment_public_case_ids=assignment_ids,
        assignment_sha256=full_manifest["stage_assignment_sha256"],
        prompt_hash=full_manifest["prompt_hash"],
    )
    packet_bytes = ("\n".join(_canonical_json(row) for row in packet.cases) + "\n").encode("utf-8")
    manifest_bytes = (_canonical_json(packet.manifest) + "\n").encode("utf-8")
    if hashlib.sha256(packet_bytes).hexdigest() != packet.manifest["stage_bundle_sha256"]:
        raise AssertionError("materialized batch bytes differ from their manifest")
    _write_bytes(args.output, packet_bytes)
    _write_bytes(args.output_manifest, manifest_bytes)
    print(
        f"sol_followup_batch=complete cases={len(packet.cases)} "
        f"remaining_before={len(missing_ids)} sha256={packet.manifest['stage_bundle_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Merge resumable judge verdict batches and finalize only exact stage coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.judge_aggregation import (  # noqa: E402
    judge_stage_output_manifest_sha256,
    resume_judge_stage_verdicts,
)
from scriber_polishing.judge_bundle import (  # noqa: E402
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


def _load_canonical_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
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
    if raw != ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8"):
        raise ValueError(f"{label} must use canonical JSONL bytes: {path}")
    return rows


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-packet", type=Path, required=True)
    parser.add_argument("--stage-packet-manifest", type=Path, required=True)
    parser.add_argument("--existing-verdicts", type=Path)
    parser.add_argument(
        "--verdict-fragment",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-manifest", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.existing_verdicts is None and not args.verdict_fragment:
        raise ValueError("judge finalization requires existing verdicts or at least one fragment")

    output_paths = (
        args.output,
        args.resume_manifest,
        args.output_manifest,
    )
    output_identities = {_path_identity(path) for path in output_paths}
    if len(output_identities) != len(output_paths):
        raise ValueError("judge verdict output paths must be distinct")
    protected_inputs = [
        args.stage_packet,
        args.stage_packet_manifest,
        *args.verdict_fragment,
    ]
    if args.existing_verdicts is not None and _path_identity(args.existing_verdicts) != _path_identity(args.output):
        protected_inputs.append(args.existing_verdicts)
    if output_identities.intersection({_path_identity(path) for path in protected_inputs}):
        raise ValueError("judge verdict outputs must not overwrite input artifacts")

    stage_cases = _load_canonical_jsonl(args.stage_packet, "judge stage packet")
    stage_packet_manifest = validate_judge_stage_packet_manifest(
        _load_canonical_json(
            args.stage_packet_manifest,
            "judge stage packet manifest",
        )
    )
    if stage_packet_manifest["stage_bundle_sha256"] != hashlib.sha256(args.stage_packet.read_bytes()).hexdigest():
        raise ValueError("judge stage packet bytes differ from manifest")

    verdict_batches: list[list[dict[str, Any]]] = []
    if args.existing_verdicts is not None:
        verdict_batches.append(
            _load_canonical_jsonl(
                args.existing_verdicts,
                "existing judge verdict",
            )
        )
    verdict_batches.extend(_load_canonical_jsonl(path, "judge verdict fragment") for path in args.verdict_fragment)
    result = resume_judge_stage_verdicts(
        stage_cases,
        stage_packet_manifest,
        verdict_batches,
    )
    merged_bytes = ("\n".join(_canonical_json(verdict) for verdict in result.verdicts) + "\n").encode("utf-8")
    merged_hash = hashlib.sha256(merged_bytes).hexdigest()

    output_manifest_bytes: bytes | None = None
    output_manifest_hash: str | None = None
    if result.output_manifest is not None:
        output_manifest_bytes = (_canonical_json(result.output_manifest) + "\n").encode("utf-8")
        output_manifest_hash = hashlib.sha256(output_manifest_bytes).hexdigest()
        if judge_stage_output_manifest_sha256(result.output_manifest) != output_manifest_hash:
            raise AssertionError("judge output manifest canonical hash changed")
    elif args.output_manifest.exists():
        raise ValueError("in-progress judge stage has a stale complete output manifest")

    first_verdict = result.verdicts[0]
    resume_manifest = validate_judge_stage_resume_manifest(
        {
            "schema_version": 1,
            "status": result.status,
            "judge_stage": first_verdict["judge_stage"],
            "judge_id": first_verdict["judge_id"],
            "judge_session_id": first_verdict["judge_session_id"],
            "model_family": first_verdict["model_family"],
            "reasoning_effort": first_verdict["reasoning_effort"],
            "prompt_hash": first_verdict["prompt_hash"],
            "reviewed_bundle_sha256": first_verdict["reviewed_bundle_sha256"],
            "stage_assignment_sha256": first_verdict["stage_assignment_sha256"],
            "stage_packet_sha256": stage_packet_manifest["stage_bundle_sha256"],
            "stage_packet_manifest_sha256": hashlib.sha256(args.stage_packet_manifest.read_bytes()).hexdigest(),
            "expected_count": len(stage_cases),
            "completed_count": result.completed_count,
            "missing_count": len(result.missing_case_ids),
            "missing_case_ids": list(result.missing_case_ids),
            "merged_verdict_output_sha256": merged_hash,
            "output_manifest_sha256": output_manifest_hash,
            "hard_deterministic_errors_are_binding": True,
        }
    )
    resume_bytes = (_canonical_json(resume_manifest) + "\n").encode("utf-8")

    _write_bytes(args.output, merged_bytes)
    _write_bytes(args.resume_manifest, resume_bytes)
    if output_manifest_bytes is not None:
        _write_bytes(args.output_manifest, output_manifest_bytes)

    print(
        f"judge_stage={result.status} stage={stage_packet_manifest['judge_stage']} "
        f"completed={result.completed_count} missing={len(result.missing_case_ids)}"
    )
    return 0 if result.status == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())

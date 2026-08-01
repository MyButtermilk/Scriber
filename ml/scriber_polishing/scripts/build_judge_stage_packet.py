"""Materialize one exact, anonymous Terra/Sol judge-stage packet."""

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
    judge_bundle_sha256,
    materialize_sol_release_assignment,
)
from scriber_polishing.judge_bundle import (  # noqa: E402
    judge_materialization_plan_sha256,
    judge_stage_assignment_sha256,
    materialize_judge_stage_packet,
    validate_judge_bundle_manifest,
    validate_judge_materialization_plan,
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


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
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


def _require_distinct_outputs(
    *,
    output: Path,
    output_manifest: Path,
    inputs: list[Path],
) -> None:
    output_identities = {_path_identity(output), _path_identity(output_manifest)}
    if len(output_identities) != 2:
        raise ValueError("judge stage output and manifest paths must be distinct")
    input_identities = {_path_identity(path) for path in inputs}
    if output_identities.intersection(input_identities):
        raise ValueError("judge stage outputs must not overwrite an input artifact")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("terra_prejudge", "sol_release", "sol_tiebreak"),
        required=True,
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--materialization-plan", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path)
    parser.add_argument("--terra-verdicts", type=Path)
    parser.add_argument("--terra-output-manifest", type=Path)
    parser.add_argument("--aggregation-report", type=Path)
    parser.add_argument("--resume-manifest", type=Path)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument(
        "--allow-incomplete-coverage",
        action="store_true",
        help=(
            "Materialize an available-coverage PRIVATE_CANDIDATE stage packet. "
            "The bound plan remains failed and final aggregation must reject release."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.max_cases is not None and args.max_cases < 1:
        raise ValueError("--max-cases must be positive")

    optional_inputs = [
        path
        for path in (
            args.private_mapping,
            args.terra_verdicts,
            args.terra_output_manifest,
            args.aggregation_report,
            args.resume_manifest,
        )
        if path is not None
    ]
    _require_distinct_outputs(
        output=args.output,
        output_manifest=args.output_manifest,
        inputs=[
            args.bundle,
            args.bundle_manifest,
            args.materialization_plan,
            args.prompt,
            *optional_inputs,
        ],
    )
    sol_release_inputs = (
        args.private_mapping,
        args.terra_verdicts,
        args.terra_output_manifest,
    )
    if args.stage == "terra_prejudge" and (
        any(path is not None for path in sol_release_inputs) or args.aggregation_report is not None
    ):
        raise ValueError("Terra stage does not accept private or prior-stage evidence")
    if args.stage == "sol_release":
        if any(path is None for path in sol_release_inputs):
            raise ValueError("Sol release stage requires private mapping and complete Terra evidence")
        if args.aggregation_report is not None:
            raise ValueError("Sol release stage does not accept an aggregation report")
    if args.stage == "sol_tiebreak":
        if any(path is not None for path in sol_release_inputs):
            raise ValueError("Sol tie-break stage does not accept Terra private evidence")
        if args.aggregation_report is None:
            raise ValueError("Sol tie-break stage requires a pending aggregation report")

    cases = _load_canonical_jsonl(args.bundle, "judge bundle")
    raw_bundle_hash = hashlib.sha256(args.bundle.read_bytes()).hexdigest()
    if judge_bundle_sha256(cases) != raw_bundle_hash:
        raise ValueError("judge bundle is not canonical JSONL")
    bundle_manifest = validate_judge_bundle_manifest(
        _load_canonical_json(args.bundle_manifest, "judge bundle manifest")
    )
    if bundle_manifest["bundle_sha256"] != raw_bundle_hash:
        raise ValueError("judge bundle hash differs from bundle manifest")

    plan = validate_judge_materialization_plan(
        _load_canonical_json(args.materialization_plan, "judge materialization plan")
    )
    raw_plan_hash = hashlib.sha256(args.materialization_plan.read_bytes()).hexdigest()
    if judge_materialization_plan_sha256(plan) != raw_plan_hash:
        raise ValueError("judge materialization plan is not canonical")
    if bundle_manifest["materialization_plan_sha256"] != raw_plan_hash:
        raise ValueError("judge materialization plan hash differs from bundle manifest")
    if plan["bundle_sha256"] != raw_bundle_hash:
        raise ValueError("judge materialization plan is bound to another bundle")
    if not args.allow_incomplete_coverage and (not plan["coverage_passed"] or plan["coverage"]["deficits"]):
        raise ValueError("judge materialization coverage is incomplete")

    assignment_public_case_ids: list[str] | None = None
    packet_public_case_ids: list[str]
    if args.stage == "terra_prejudge":
        assignment = plan["stage_assignments"]["terra_prejudge"]
        packet_public_case_ids = assignment["public_case_ids"]
    elif args.stage == "sol_release":
        if args.private_mapping is None or args.terra_verdicts is None or args.terra_output_manifest is None:
            raise AssertionError("validated Sol release arguments changed")
        private_payload = _load_canonical_json(
            args.private_mapping,
            "private judge mapping",
        )
        if bundle_manifest["private_mapping_sha256"] != hashlib.sha256(args.private_mapping.read_bytes()).hexdigest():
            raise ValueError("private judge mapping hash differs from bundle manifest")
        if private_payload.get("bundle_sha256") != raw_bundle_hash:
            raise ValueError("private judge mapping is bound to another bundle")
        if private_payload.get("materialization_plan_sha256") != raw_plan_hash:
            raise ValueError("private judge mapping is bound to another materialization plan")
        private_cases = private_payload.get("cases")
        if not isinstance(private_cases, dict):
            raise ValueError("private judge mapping requires a cases object")
        terra_verdicts = _load_canonical_jsonl(
            args.terra_verdicts,
            "Terra verdict",
        )
        terra_output_manifest = _load_canonical_json(
            args.terra_output_manifest,
            "Terra output manifest",
        )
        assignment = materialize_sol_release_assignment(
            cases,
            private_cases,
            plan,
            terra_verdicts,
            terra_output_manifest,
        )
        packet_public_case_ids = assignment["public_case_ids"]
    else:
        if args.aggregation_report is None:
            raise AssertionError("validated tie-break arguments changed")
        aggregation = _load_json(args.aggregation_report, "judge aggregation report")
        if aggregation.get("status") != "pending_tiebreak":
            raise ValueError("Sol tie-break requires a pending_tiebreak aggregation")
        if aggregation.get("bundle_sha256") != raw_bundle_hash:
            raise ValueError("tie-break aggregation is bound to another bundle")
        if aggregation.get("materialization_plan_sha256") != raw_plan_hash:
            raise ValueError("tie-break aggregation is bound to another materialization plan")
        required_ids = aggregation.get("required_tie_break_case_ids")
        missing_ids = aggregation.get("missing_tie_break_case_ids")
        if (
            not isinstance(required_ids, list)
            or not required_ids
            or len(required_ids) != len(set(required_ids))
            or not all(isinstance(case_id, str) and case_id for case_id in required_ids)
        ):
            raise ValueError("tie-break aggregation has invalid required case IDs")
        if (
            not isinstance(missing_ids, list)
            or not missing_ids
            or len(missing_ids) != len(set(missing_ids))
            or not all(isinstance(case_id, str) and case_id for case_id in missing_ids)
            or not set(missing_ids).issubset(required_ids)
        ):
            raise ValueError("tie-break aggregation has invalid missing case IDs")
        expected_assignment_hash = judge_stage_assignment_sha256(
            "sol_tiebreak",
            raw_bundle_hash,
            required_ids,
        )
        assignment_hashes = aggregation.get("stage_assignment_sha256s")
        if not isinstance(assignment_hashes, dict) or assignment_hashes.get("sol_tiebreak") != expected_assignment_hash:
            raise ValueError("tie-break aggregation assignment hash differs")
        assignment = {
            "public_case_ids": required_ids,
            "assignment_sha256": expected_assignment_hash,
        }
        assignment_public_case_ids = required_ids
        packet_public_case_ids = missing_ids
    prompt_hash = "sha256:" + hashlib.sha256(args.prompt.read_bytes()).hexdigest()
    if args.resume_manifest is not None:
        resume_manifest = validate_judge_stage_resume_manifest(
            _load_canonical_json(
                args.resume_manifest,
                "judge stage resume manifest",
            )
        )
        if resume_manifest["status"] != "in_progress":
            raise ValueError("judge stage resume manifest is not in progress")
        if resume_manifest["judge_stage"] != args.stage:
            raise ValueError("judge stage resume manifest has another stage")
        if resume_manifest["reviewed_bundle_sha256"] != raw_bundle_hash:
            raise ValueError("judge stage resume manifest is bound to another bundle")
        if resume_manifest["stage_assignment_sha256"] != assignment["assignment_sha256"]:
            raise ValueError("judge stage resume manifest is bound to another assignment")
        if resume_manifest["prompt_hash"] != prompt_hash:
            raise ValueError("judge stage resume manifest is bound to another prompt")
        full_assignment_ids = assignment["public_case_ids"]
        if resume_manifest["expected_count"] != len(full_assignment_ids):
            raise ValueError("judge stage resume expected count differs from assignment")
        if resume_manifest["missing_count"] != len(resume_manifest["missing_case_ids"]):
            raise ValueError("judge stage resume missing count is inconsistent")
        full_packet = materialize_judge_stage_packet(
            cases,
            judge_stage=args.stage,
            public_case_ids=full_assignment_ids,
            assignment_sha256=assignment["assignment_sha256"],
            prompt_hash=prompt_hash,
        )
        full_manifest_bytes = (_canonical_json(full_packet.manifest) + "\n").encode("utf-8")
        if (
            resume_manifest["stage_packet_sha256"] != full_packet.manifest["stage_bundle_sha256"]
            or resume_manifest["stage_packet_manifest_sha256"] != hashlib.sha256(full_manifest_bytes).hexdigest()
        ):
            raise ValueError("judge stage resume manifest is bound to another original packet")
        assignment_public_case_ids = full_assignment_ids
        packet_public_case_ids = resume_manifest["missing_case_ids"]
    if args.max_cases is not None and len(packet_public_case_ids) > args.max_cases:
        if assignment_public_case_ids is None:
            assignment_public_case_ids = assignment["public_case_ids"]
        packet_id_set = set(packet_public_case_ids)
        packet_public_case_ids = [case["case_id"] for case in cases if case["case_id"] in packet_id_set][
            : args.max_cases
        ]
    packet = materialize_judge_stage_packet(
        cases,
        judge_stage=args.stage,
        public_case_ids=packet_public_case_ids,
        assignment_public_case_ids=assignment_public_case_ids,
        assignment_sha256=assignment["assignment_sha256"],
        prompt_hash=prompt_hash,
    )
    packet_bytes = ("\n".join(_canonical_json(case) for case in packet.cases) + "\n").encode("utf-8")
    if hashlib.sha256(packet_bytes).hexdigest() != packet.manifest["stage_bundle_sha256"]:
        raise AssertionError("judge stage packet hash differs from serialized bytes")
    manifest_bytes = (_canonical_json(packet.manifest) + "\n").encode("utf-8")
    _write_bytes(args.output, packet_bytes)
    _write_bytes(args.output_manifest, manifest_bytes)
    print(
        f"judge_stage_packet=complete stage={args.stage} "
        f"cases={len(packet.cases)} sha256={packet.manifest['stage_bundle_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

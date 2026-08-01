"""Aggregate exact, hash-bound Terra/Sol reviews with optional Sol tie-break."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_bundle import (  # noqa: E402
    validate_evaluation_bundle_manifest,
)
from scriber_polishing.evaluation_gate import load_evaluation_config  # noqa: E402
from scriber_polishing.evaluation_io import load_evaluation_references  # noqa: E402
from scriber_polishing.judge_aggregation import (  # noqa: E402
    aggregate_judge_verdicts,
    judge_bundle_sha256,
    judge_stage_output_manifest_sha256,
    judge_verdicts_sha256,
)
from scriber_polishing.judge_bundle import (  # noqa: E402
    judge_materialization_plan_sha256,
    validate_judge_bundle_manifest,
    validate_judge_materialization_plan,
)
from scriber_polishing.schemas import validate_judge_stage_output_manifest  # noqa: E402


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    expected = (_canonical_json(value) + "\n").encode("utf-8")
    if raw != expected:
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
    expected = ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8")
    if raw != expected:
        raise ValueError(f"{label} must use canonical JSONL bytes: {path}")
    return rows


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _load_stage(
    verdict_path: Path,
    manifest_path: Path,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    verdicts = _load_canonical_jsonl(verdict_path, f"{label} verdict")
    manifest = validate_judge_stage_output_manifest(_load_canonical_json(manifest_path, f"{label} output manifest"))
    raw_verdict_hash = hashlib.sha256(verdict_path.read_bytes()).hexdigest()
    if manifest["verdict_output_sha256"] != raw_verdict_hash:
        raise ValueError(f"{label} verdict bytes differ from output manifest")
    if judge_verdicts_sha256(verdicts) != raw_verdict_hash:
        raise ValueError(f"{label} verdict output hash is not canonical")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if judge_stage_output_manifest_sha256(manifest) != manifest_hash:
        raise ValueError(f"{label} output manifest hash is not canonical")
    return verdicts, manifest


def _validate_reference_manifest(
    path: Path,
    *,
    references_path: Path,
    references: tuple[dict[str, Any], ...],
) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("reference manifest must be a JSON object")
    manifest = validate_evaluation_bundle_manifest(value)
    reference_hash = hashlib.sha256(references_path.read_bytes()).hexdigest()
    case_ids_hash = hashlib.sha256(
        (_canonical_json([reference["case_id"] for reference in references]) + "\n").encode("utf-8")
    ).hexdigest()
    split_counts = dict(sorted(Counter(str(reference["split"]) for reference in references).items()))
    if manifest["kind"] != "scriber_evaluation_reference_bundle":
        raise ValueError("reference manifest has the wrong bundle kind")
    if manifest["output_filename"] != references_path.name:
        raise ValueError("reference manifest output filename differs from reference file")
    if manifest["records_sha256"] != reference_hash:
        raise ValueError("reference manifest hash differs from reference file")
    if manifest["record_count"] != len(references):
        raise ValueError("reference manifest count differs from reference file")
    if manifest["case_ids_sha256"] != case_ids_hash:
        raise ValueError("reference manifest case IDs differ from reference file")
    if manifest["split_counts"] != split_counts:
        raise ValueError("reference manifest split counts differ from reference file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--materialization-plan", type=Path, required=True)
    parser.add_argument("--terra-verdicts", type=Path, required=True)
    parser.add_argument("--terra-output-manifest", type=Path, required=True)
    parser.add_argument("--sol-verdicts", type=Path, required=True)
    parser.add_argument("--sol-output-manifest", type=Path, required=True)
    parser.add_argument("--tie-break-verdicts", type=Path)
    parser.add_argument("--tie-break-output-manifest", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--release-candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.tie_break_verdicts is None) != (args.tie_break_output_manifest is None):
        raise ValueError("--tie-break-verdicts and --tie-break-output-manifest must be supplied together")
    input_paths = [
        args.bundle,
        args.references,
        args.reference_manifest,
        args.manifest,
        args.private_mapping,
        args.materialization_plan,
        args.terra_verdicts,
        args.terra_output_manifest,
        args.sol_verdicts,
        args.sol_output_manifest,
        args.config,
    ]
    if args.tie_break_verdicts is not None:
        input_paths.append(args.tie_break_verdicts)
    if args.tie_break_output_manifest is not None:
        input_paths.append(args.tie_break_output_manifest)
    if _path_identity(args.output) in {_path_identity(path) for path in input_paths}:
        raise ValueError("judge aggregation output must not overwrite an input artifact")

    manifest = validate_judge_bundle_manifest(_load_canonical_json(args.manifest, "judge bundle manifest"))
    bundle_bytes = args.bundle.read_bytes()
    raw_bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    if manifest["bundle_sha256"] != raw_bundle_hash:
        raise ValueError("judge bundle hash differs from manifest")
    cases = _load_canonical_jsonl(args.bundle, "judge bundle")
    if judge_bundle_sha256(cases) != raw_bundle_hash:
        raise ValueError("judge bundle is not canonical JSONL")

    private_bytes = args.private_mapping.read_bytes()
    if manifest["private_mapping_sha256"] != hashlib.sha256(private_bytes).hexdigest():
        raise ValueError("private judge mapping hash differs from manifest")
    private_payload = _load_canonical_json(args.private_mapping, "private judge mapping")
    required_private_fields = {
        "schema_version",
        "bundle_sha256",
        "blind_seed",
        "comparison_kind",
        "candidate_labels",
        "candidate_roles",
        "reference_sha256",
        "reference_manifest_sha256",
        "coverage_policy_sha256",
        "materialization_plan_sha256",
        "candidate_prediction_sha256",
        "cases",
    }
    if set(private_payload) != required_private_fields or private_payload["schema_version"] != 2:
        raise ValueError("private judge mapping has an unexpected shape or schema version")
    if private_payload["bundle_sha256"] != raw_bundle_hash:
        raise ValueError("private judge mapping is bound to another bundle")
    candidate_labels = private_payload["candidate_labels"]
    candidate_roles = private_payload["candidate_roles"]
    expected_opponent_role = {
        "fine_tune_vs_base": "base_model",
        "fine_tune_vs_rule_baseline": "rule_baseline",
    }.get(private_payload["comparison_kind"])
    if expected_opponent_role is None:
        raise ValueError("private judge mapping has an unknown comparison kind")
    if (
        not isinstance(candidate_labels, list)
        or len(candidate_labels) != 2
        or len(set(candidate_labels)) != 2
        or not all(isinstance(label, str) and label for label in candidate_labels)
        or not isinstance(candidate_roles, dict)
        or set(candidate_roles) != set(candidate_labels)
        or not all(isinstance(role, str) for role in candidate_roles.values())
        or sorted(candidate_roles.values()) != sorted(["fine_tune", expected_opponent_role])
    ):
        raise ValueError("private judge mapping has invalid pairwise candidate roles")
    fine_tune_labels = [label for label, role in candidate_roles.items() if role == "fine_tune"]
    if fine_tune_labels != [args.release_candidate]:
        raise ValueError("release candidate differs from the private fine-tune role")
    opponent_labels = [label for label, role in candidate_roles.items() if role == expected_opponent_role]
    if len(opponent_labels) != 1:
        raise ValueError("private judge mapping requires one pairwise opponent")
    opponent_label = opponent_labels[0]
    private_cases = private_payload["cases"]
    if not isinstance(private_cases, dict):
        raise ValueError("private judge mapping requires a cases object")
    mapped_candidate_labels = {
        mapping.get(field)
        for mapping in private_cases.values()
        if isinstance(mapping, dict)
        for field in ("candidate_a_label", "candidate_b_label")
    }
    if mapped_candidate_labels != set(candidate_labels):
        raise ValueError("private candidate roles differ from the blinded case mapping")
    prediction_hashes = private_payload["candidate_prediction_sha256"]
    if (
        not isinstance(prediction_hashes, dict)
        or set(prediction_hashes) != set(candidate_labels)
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in prediction_hashes.values()
        )
    ):
        raise ValueError("private candidate prediction hashes are incomplete")

    plan = validate_judge_materialization_plan(
        _load_canonical_json(args.materialization_plan, "judge materialization plan")
    )
    raw_plan_hash = hashlib.sha256(args.materialization_plan.read_bytes()).hexdigest()
    if judge_materialization_plan_sha256(plan) != raw_plan_hash:
        raise ValueError("judge materialization plan is not canonical")
    for label, expected_hash in (
        ("judge bundle manifest", manifest["materialization_plan_sha256"]),
        ("private judge mapping", private_payload["materialization_plan_sha256"]),
    ):
        if expected_hash != raw_plan_hash:
            raise ValueError(f"materialization plan hash differs from {label}")
    if plan["coverage_policy_sha256"] != manifest["coverage_policy_sha256"]:
        raise ValueError("coverage policy hash differs from judge bundle manifest")
    if plan["coverage_policy_sha256"] != private_payload["coverage_policy_sha256"]:
        raise ValueError("coverage policy hash differs from private judge mapping")

    reference_hash = hashlib.sha256(args.references.read_bytes()).hexdigest()
    if manifest["reference_sha256"] != reference_hash:
        raise ValueError("evaluation reference hash differs from judge manifest")
    if private_payload["reference_sha256"] != reference_hash:
        raise ValueError("evaluation reference hash differs from private judge mapping")
    references = load_evaluation_references(args.references)
    reference_manifest_hash = _validate_reference_manifest(
        args.reference_manifest,
        references_path=args.references,
        references=references,
    )
    if manifest["reference_manifest_sha256"] != reference_manifest_hash:
        raise ValueError("reference manifest hash differs from judge bundle manifest")
    if private_payload["reference_manifest_sha256"] != reference_manifest_hash:
        raise ValueError("reference manifest hash differs from private judge mapping")
    mapped_base_ids = {mapping.get("base_case_id") for mapping in private_cases.values() if isinstance(mapping, dict)}
    reference_ids = {reference["case_id"] for reference in references}
    if mapped_base_ids != reference_ids:
        raise ValueError("evaluation references do not cover the exact blinded base cases")
    references_by_id = {reference["case_id"]: reference for reference in references}
    if set(plan["base_case_metadata"]) != reference_ids:
        raise ValueError("materialization metadata does not cover the exact references")
    for case_id, reference in references_by_id.items():
        metadata = plan["base_case_metadata"][case_id]
        if metadata["split"] != reference["split"]:
            raise ValueError(f"materialization split differs from reference: {case_id}")
        if metadata["slice_tags"] != sorted(reference["slice_tags"]):
            raise ValueError(f"materialization slice tags differ from reference: {case_id}")
        if metadata["is_identity"] is not reference["is_identity"]:
            raise ValueError(f"materialization identity marker differs from reference: {case_id}")
    if manifest["base_case_count"] != len(reference_ids):
        raise ValueError("judge manifest base-case count differs from references")
    if manifest["public_case_count"] != len(cases):
        raise ValueError("judge manifest public-case count differs from bundle")

    config = load_evaluation_config(args.config)
    if args.release_candidate not in config["candidates"]:
        raise ValueError("release candidate is not declared in evaluation config")
    judge_config = config["judge_aggregation"]
    terra, terra_manifest = _load_stage(
        args.terra_verdicts,
        args.terra_output_manifest,
        label="Terra",
    )
    sol, sol_manifest = _load_stage(
        args.sol_verdicts,
        args.sol_output_manifest,
        label="Sol",
    )
    tie_break: list[dict[str, Any]] | None = None
    tie_manifest: dict[str, Any] | None = None
    if args.tie_break_verdicts is not None and args.tie_break_output_manifest is not None:
        tie_break, tie_manifest = _load_stage(
            args.tie_break_verdicts,
            args.tie_break_output_manifest,
            label="tie-break",
        )

    result = aggregate_judge_verdicts(
        cases,
        private_cases,
        terra,
        sol,
        materialization_plan=plan,
        terra_output_manifest=terra_manifest,
        sol_output_manifest=sol_manifest,
        comparison_kind=private_payload["comparison_kind"],
        tie_break_verdicts=tie_break,
        tie_break_output_manifest=tie_manifest,
        release_candidate=args.release_candidate,
        acceptable_rate_minimums=judge_config["acceptable_rate_minimums"],
        rubric_minimums=judge_config["rubric_minimums"],
    )
    exact_stage_coverage = result.status == "complete" and all(
        stage["exact"] for stage in result.stage_coverage.values()
    )
    report = {
        **asdict(result),
        "release_candidate_prediction_sha256": prediction_hashes[args.release_candidate],
        "opponent_role": expected_opponent_role,
        "opponent_prediction_sha256": prediction_hashes[opponent_label],
        "reference_sha256": reference_hash,
        "reference_manifest_sha256": reference_manifest_hash,
        "judge_bundle_manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "private_mapping_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "evaluation_config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "exact_required_stage_coverage": exact_stage_coverage,
        "materialized_mandatory_coverage": plan["coverage_passed"],
        "deterministic_hard_errors_overridable": False,
        "human_curation": False,
        "generative_api": False,
    }
    _write_json(args.output, report)
    print(
        f"judge_aggregation={result.status} comparison={result.comparison_kind} "
        f"accepted={result.accepted_count} rejected={result.rejected_count} "
        f"acceptance_rate={result.acceptance_rate:.6f} "
        f"comparison_gate_passed={str(result.comparison_gate_passed).lower()} "
        "release_gate_passed=false"
    )
    if result.status == "pending_tiebreak":
        return 3
    return 0 if result.comparison_gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

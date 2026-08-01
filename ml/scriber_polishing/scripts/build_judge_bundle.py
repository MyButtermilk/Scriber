"""Build a blinded A/B judge bundle and a separately stored private label map."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_bundle import (  # noqa: E402
    validate_evaluation_bundle_manifest,
)
from scriber_polishing.evaluation_io import (  # noqa: E402
    load_candidate_predictions,
    load_evaluation_references,
)
from scriber_polishing.judge_aggregation import judge_bundle_sha256  # noqa: E402
from scriber_polishing.judge_bundle import (  # noqa: E402
    JUDGE_COMPARISON_KINDS,
    build_blinded_judge_bundle,
    judge_materialization_plan_sha256,
    load_mandatory_judge_coverage_policy,
    materialize_judge_coverage,
    validate_judge_bundle_manifest,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _candidate(argument: str) -> tuple[str, Path]:
    if "=" not in argument:
        raise ValueError("--candidate must use private-label=prediction-jsonl")
    label, raw_path = argument.split("=", 1)
    if not label or not raw_path:
        raise ValueError("--candidate must use private-label=prediction-jsonl")
    return label, Path(raw_path)


def _path_identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _validate_output_paths(
    outputs: tuple[Path, ...],
    inputs: tuple[Path, ...],
) -> None:
    output_identities = {_path_identity(path) for path in outputs}
    if len(output_identities) != len(outputs):
        raise ValueError("judge bundle output paths must be distinct")
    if output_identities.intersection({_path_identity(path) for path in inputs}):
        raise ValueError("judge bundle outputs must not overwrite input artifacts")


def _reference_manifest(
    path: Path,
    *,
    references_path: Path,
    references: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("reference manifest must be a JSON object")
    manifest = validate_evaluation_bundle_manifest(value)
    reference_hash = _sha256(references_path)
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
    return manifest, _sha256(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="Private candidate label and prediction JSONL as label=path; provide exactly twice.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--private-mapping", type=Path, required=True)
    parser.add_argument("--materialization-plan", type=Path, required=True)
    parser.add_argument(
        "--comparison-kind",
        choices=sorted(JUDGE_COMPARISON_KINDS),
        required=True,
        help="Stored only in the private mapping; build both required comparison kinds separately.",
    )
    parser.add_argument(
        "--fine-tune-label",
        required=True,
        help="Private label of the fine-tuned release candidate; never written to public artifacts.",
    )
    parser.add_argument("--blind-seed", type=int, required=True)
    parser.add_argument("--swapped-fraction", type=float, default=0.5)
    parser.add_argument(
        "--allow-incomplete-coverage",
        action="store_true",
        help=(
            "Materialize an anonymous PRIVATE_CANDIDATE packet despite explicit "
            "coverage deficits. The materialization plan remains failed and release "
            "aggregation must still reject it."
        ),
    )
    args = parser.parse_args()
    if len(args.candidate) != 2:
        raise ValueError("exactly two --candidate values are required")
    (x_label, x_path), (y_label, y_path) = (_candidate(value) for value in args.candidate)
    _validate_output_paths(
        (
            args.output,
            args.manifest,
            args.private_mapping,
            args.materialization_plan,
        ),
        (
            args.references,
            args.reference_manifest,
            x_path,
            y_path,
        ),
    )
    if args.fine_tune_label not in {x_label, y_label}:
        raise ValueError("--fine-tune-label must match exactly one private candidate label")
    opponent_label = y_label if args.fine_tune_label == x_label else x_label
    opponent_role = {
        "fine_tune_vs_base": "base_model",
        "fine_tune_vs_rule_baseline": "rule_baseline",
    }[args.comparison_kind]

    references = load_evaluation_references(args.references)
    _, reference_manifest_hash = _reference_manifest(
        args.reference_manifest,
        references_path=args.references,
        references=references,
    )
    candidate_x = load_candidate_predictions(x_path)
    candidate_y = load_candidate_predictions(y_path)
    bundle = build_blinded_judge_bundle(
        references,
        candidate_x,
        candidate_y,
        candidate_x_label=x_label,
        candidate_y_label=y_label,
        blind_seed=args.blind_seed,
        swapped_fraction=args.swapped_fraction,
    )
    bundle_bytes = ("\n".join(_canonical_json(case) for case in bundle.cases) + "\n").encode("utf-8")
    bundle_hash = judge_bundle_sha256(bundle.cases)
    if hashlib.sha256(bundle_bytes).hexdigest() != bundle_hash:
        raise AssertionError("judge bundle canonical hash differs from serialized bytes")

    coverage_policy = load_mandatory_judge_coverage_policy()
    materialization_plan = materialize_judge_coverage(
        references,
        bundle,
        coverage_policy,
    )
    materialization_bytes = (_canonical_json(materialization_plan) + "\n").encode("utf-8")
    materialization_hash = judge_materialization_plan_sha256(materialization_plan)
    if hashlib.sha256(materialization_bytes).hexdigest() != materialization_hash:
        raise AssertionError("judge materialization hash differs from serialized bytes")
    _write_bytes(args.materialization_plan, materialization_bytes)
    if not materialization_plan["coverage_passed"] and not args.allow_incomplete_coverage:
        deficits = ";".join(materialization_plan["coverage"]["deficits"])
        print(
            f"judge_bundle=blocked coverage_passed=false "
            f"materialization_sha256={materialization_hash} deficits={deficits}",
            file=sys.stderr,
        )
        return 2
    if not materialization_plan["coverage_passed"]:
        deficits = ";".join(materialization_plan["coverage"]["deficits"])
        print(
            f"judge_bundle=private_candidate coverage_passed=false "
            f"materialization_sha256={materialization_hash} deficits={deficits}",
            file=sys.stderr,
        )

    private_payload = {
        "schema_version": 2,
        "bundle_sha256": bundle_hash,
        "blind_seed": args.blind_seed,
        "comparison_kind": args.comparison_kind,
        "candidate_labels": [x_label, y_label],
        "candidate_roles": {
            args.fine_tune_label: "fine_tune",
            opponent_label: opponent_role,
        },
        "reference_sha256": _sha256(args.references),
        "reference_manifest_sha256": reference_manifest_hash,
        "coverage_policy_sha256": materialization_plan["coverage_policy_sha256"],
        "materialization_plan_sha256": materialization_hash,
        "candidate_prediction_sha256": {
            x_label: _sha256(x_path),
            y_label: _sha256(y_path),
        },
        "cases": bundle.private_mapping,
    }
    private_bytes = (_canonical_json(private_payload) + "\n").encode("utf-8")
    swapped_count = sum(mapping["position_variant"] == "swapped" for mapping in bundle.private_mapping.values())
    identical_base_case_count = len(
        {
            mapping["base_case_id"]
            for mapping in bundle.private_mapping.values()
            if mapping["candidate_outputs_identical"]
        }
    )
    manifest = validate_judge_bundle_manifest(
        {
            "schema_version": 2,
            "bundle_case_schema_version": 1,
            "reference_sha256": _sha256(args.references),
            "reference_manifest_sha256": reference_manifest_hash,
            "base_case_count": len(references),
            "public_case_count": len(bundle.cases),
            "swapped_duplicate_count": swapped_count,
            "swapped_source_fraction": swapped_count / len(references),
            "bundle_sha256": bundle_hash,
            "private_mapping_sha256": hashlib.sha256(private_bytes).hexdigest(),
            "coverage_policy_sha256": materialization_plan["coverage_policy_sha256"],
            "materialization_plan_sha256": materialization_hash,
            "contains_candidate_identity": False,
            "contains_model_or_checkpoint_identity": False,
            "contains_training_history": False,
            "contains_automatic_aggregate_scores": False,
            "hard_deterministic_errors_are_binding": True,
        }
    )
    _write_bytes(args.output, bundle_bytes)
    _write_bytes(args.private_mapping, private_bytes)
    _write_bytes(
        args.manifest,
        (_canonical_json(manifest) + "\n").encode("utf-8"),
    )
    print(
        f"judge_bundle=complete base_cases={len(references)} public_cases={len(bundle.cases)} "
        f"swapped={swapped_count} identical_base_cases={identical_base_case_count} "
        f"sha256={bundle_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

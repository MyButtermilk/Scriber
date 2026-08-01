"""Build and freeze deterministic split-first Scriber polishing JSONL datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.coverage_audit import (  # noqa: E402
    MANDATORY_QUOTA_BINDING,
    CoverageAuditError,
    audit_coverage,
)
from scriber_polishing.dataset_builder import build_dataset_pairs  # noqa: E402
from scriber_polishing.dataset_policy import (  # noqa: E402
    bind_payload,
    load_data_generation_policy,
)
from scriber_polishing.quota_sampler import select_quota_preserving_records  # noqa: E402
from scriber_polishing.split_dataset import Split  # noqa: E402
from scriber_polishing.validators import validate_canonical_record  # noqa: E402


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_canonicals(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank canonical JSONL line at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"canonical JSONL line {line_number} must be an object")
        records.append(validate_canonical_record(value))
    if not records:
        raise ValueError("canonical JSONL must contain at least one record")
    return records


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...]) -> str:
    payload = ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    payload = (_canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--data-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "data_generation.yaml",
    )
    parser.add_argument(
        "--coverage-report",
        type=Path,
        help="Coverage report destination (default: <output-root>/coverage_report.json).",
    )
    parser.add_argument("--seed", type=int, default=270_001)
    parser.add_argument("--minimum-canonical", type=int)
    parser.add_argument("--minimum-train", type=int)
    parser.add_argument("--minimum-validation", type=int)
    parser.add_argument("--minimum-test", type=int)
    parser.add_argument("--minimum-challenge", type=int)
    parser.add_argument(
        "--target-train",
        type=int,
        help="Must confirm the target_train_pairs value in --data-config.",
    )
    args = parser.parse_args()
    policy = load_data_generation_policy(args.data_config)
    target_train = policy.resolve_target_train(args.target_train)

    def minimum(cli_value: int | None, configured: int, option: str) -> int:
        if cli_value is None:
            return configured
        if isinstance(cli_value, bool) or cli_value < configured:
            raise ValueError(f"{option} cannot weaken configured minimum {configured}")
        return cli_value

    minimum_canonical = minimum(
        args.minimum_canonical,
        policy.minimum_canonical_documents,
        "--minimum-canonical",
    )
    minimum_train = minimum(
        args.minimum_train,
        policy.minimum_train_pairs,
        "--minimum-train",
    )
    minimum_validation = minimum(
        args.minimum_validation,
        policy.minimum_validation_pairs,
        "--minimum-validation",
    )
    minimum_test = minimum(
        args.minimum_test,
        policy.minimum_test_pairs,
        "--minimum-test",
    )
    minimum_challenge = minimum(
        args.minimum_challenge,
        policy.minimum_challenge_pairs,
        "--minimum-challenge",
    )
    if target_train < minimum_train:
        raise ValueError("configured target_train_pairs cannot be below the effective train minimum")

    canonicals = _load_canonicals(args.canonical)
    if len(canonicals) < minimum_canonical:
        raise ValueError(
            f"canonical count {len(canonicals)} is below required minimum {minimum_canonical}"
        )
    pairs = build_dataset_pairs(canonicals, seed=args.seed)
    generated_split_counts = {split.value: len(pairs[split]) for split in Split}
    train_selection: dict[str, object] = {
        "algorithm": "all_generated_rows",
        "algorithm_version": 1,
        "source_count": len(pairs[Split.TRAIN]),
        "selected_count": len(pairs[Split.TRAIN]),
        "target_count": target_train,
    }
    if len(pairs[Split.TRAIN]) > target_train:
        selection = select_quota_preserving_records(
            pairs[Split.TRAIN],
            target_count=target_train,
            seed=args.seed,
        )
        pairs[Split.TRAIN] = tuple(dict(record) for record in selection.records)
        train_selection = {
            "algorithm": "quota_union_then_sha256_fill_v1",
            "algorithm_version": 1,
            "source_count": selection.source_count,
            "selected_count": selection.selected_count,
            "target_count": target_train,
            "mandatory_union_count": selection.mandatory_union_count,
            "seed": selection.seed,
            "quota_counts": selection.quota_counts,
        }
    minima = {
        Split.TRAIN: minimum_train,
        Split.VALIDATION: minimum_validation,
        Split.TEST: minimum_test,
        Split.CHALLENGE: minimum_challenge,
    }
    for split, minimum in minima.items():
        if len(pairs[split]) < minimum:
            raise ValueError(
                f"{split.value} count {len(pairs[split])} is below required minimum {minimum}"
            )

    coverage_report = audit_coverage(row for split in Split for row in pairs[split])
    if not coverage_report.passed:
        raise CoverageAuditError(coverage_report)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    coverage_path = (
        args.coverage_report.resolve()
        if args.coverage_report is not None
        else output_root / "coverage_report.json"
    )
    try:
        coverage_path.relative_to(output_root)
    except ValueError as error:
        raise ValueError("--coverage-report must be located inside --output-root") from error
    coverage_payload = asdict(coverage_report)
    coverage_payload["passed"] = coverage_report.passed
    coverage_payload["deficits"] = coverage_report.deficits
    coverage_bytes = _write_json(coverage_path, coverage_payload)
    train_selection["quota_counts"] = {
        result.quota_id: result.train_count for result in coverage_report.quotas
    }

    split_hashes: dict[str, str] = {}
    split_counts: dict[str, int] = {}
    parent_splits: dict[str, set[str]] = {}
    for split in Split:
        rows = pairs[split]
        split_hashes[split.value] = _write_jsonl(output_root / f"{split.value}.jsonl", rows)
        split_counts[split.value] = len(rows)
        for row in rows:
            parent_splits.setdefault(row["parent_canonical_document_id"], set()).add(split.value)
    leakage = sorted(parent for parent, assigned in parent_splits.items() if len(assigned) != 1)
    if leakage:
        raise RuntimeError(f"split leakage detected for parent canonical: {leakage[0]}")

    critical_regressions = tuple(pairs[Split.CHALLENGE][:500])
    if len(critical_regressions) < 500:
        raise ValueError("challenge split cannot provide 500 fixed critical regressions")
    critical_hash = _write_jsonl(
        output_root / "critical_regressions.jsonl",
        critical_regressions,
    )
    coverage_binding = bind_payload(
        coverage_path,
        coverage_bytes,
        allowed_root=output_root,
    )
    manifest = {
        "manifest_version": 1,
        "seed": args.seed,
        "canonical_count": len(canonicals),
        "parent_canonical_count": len(parent_splits),
        "split_counts": split_counts,
        "generated_split_counts": generated_split_counts,
        "split_sha256": split_hashes,
        "train_selection": train_selection,
        "coverage_report": coverage_binding,
        "data_generation_policy": policy.source_binding,
        "mandatory_quota_contract": MANDATORY_QUOTA_BINDING,
        "effective_minimums": {
            "canonical": minimum_canonical,
            "train": minimum_train,
            "validation": minimum_validation,
            "test": minimum_test,
            "challenge": minimum_challenge,
        },
        "critical_regression_count": len(critical_regressions),
        "critical_regression_sha256": critical_hash,
        "split_before_variants": True,
        "split_group": policy.split_group,
        "split_leakage": False,
        "sealed_splits": ["test", "challenge"],
        "synthetic_only": True,
        "human_curation": False,
        "generative_api": False,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
    temporary_manifest.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
    temporary_manifest.replace(args.manifest)
    print(
        f"final_dataset=complete canonical={len(canonicals)} "
        f"train={split_counts['train']} validation={split_counts['validation']} "
        f"test={split_counts['test']} challenge={split_counts['challenge']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

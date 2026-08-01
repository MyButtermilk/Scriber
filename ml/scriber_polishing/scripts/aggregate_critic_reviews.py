"""Aggregate complete blinded critic reviews and recover approved seed identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.review_aggregation import ReviewDecision, aggregate_case_reviews  # noqa: E402
from scriber_polishing.schemas import validate_critic_verdict  # noqa: E402


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            raise ValueError(f"blank JSONL line in {path} at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"non-object JSONL line in {path} at {line_number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"review/package file is empty: {path}")
    return rows


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_canonical_json(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--private-index", type=Path, required=True)
    parser.add_argument("--review", type=Path, nargs="+", required=True)
    parser.add_argument("--accepted-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--minimum-adversarial-critics", type=int, default=2)
    parser.add_argument("--minimum-acceptance", type=float, default=0.98)
    args = parser.parse_args()
    if args.minimum_adversarial_critics < 1:
        raise ValueError("--minimum-adversarial-critics must be positive")
    if not 0 <= args.minimum_acceptance <= 1:
        raise ValueError("--minimum-acceptance must be in [0, 1]")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    package_sha256 = hashlib.sha256(args.package.read_bytes()).hexdigest()
    if manifest.get("package_sha256") != package_sha256:
        raise ValueError("critic package hash differs from manifest")
    cases = _load_jsonl(args.package)
    case_ids = tuple(str(case.get("case_id", "")) for case in cases)
    if len(case_ids) != manifest.get("case_count") or len(case_ids) != len(set(case_ids)):
        raise ValueError("critic package case count or identities are invalid")

    index_bytes = args.private_index.read_bytes()
    if manifest.get("private_index_sha256") != hashlib.sha256(index_bytes).hexdigest():
        raise ValueError("private critic index hash differs from manifest")
    private_index = json.loads(index_bytes)
    if not isinstance(private_index, dict) or set(private_index) != set(case_ids):
        raise ValueError("private critic index does not cover the exact package")

    review_sets: list[list[dict[str, Any]]] = []
    critic_roles: dict[str, str] = {}
    for review_path in args.review:
        reviews = [validate_critic_verdict(row) for row in _load_jsonl(review_path)]
        reviewed_case_ids = [review["case_id"] for review in reviews]
        if len(reviews) != len(case_ids) or set(reviewed_case_ids) != set(case_ids):
            raise ValueError(f"critic review is incomplete or contains duplicates: {review_path}")
        identities = {review["critic_id"] for review in reviews}
        roles = {review["critic_role"] for review in reviews}
        package_hashes = {review["reviewed_package_sha256"] for review in reviews}
        if len(identities) != 1 or len(roles) != 1:
            raise ValueError(f"review file changes critic identity or role: {review_path}")
        critic_id = next(iter(identities))
        role = next(iter(roles))
        if critic_id in critic_roles:
            raise ValueError(f"critic identity appears in multiple review files: {critic_id}")
        critic_roles[critic_id] = role
        if package_hashes != {package_sha256}:
            raise ValueError(f"review file is bound to another package: {review_path}")
        review_sets.append(reviews)

    role_counts = Counter(critic_roles.values())
    if role_counts["fact"] < 1 or role_counts["style"] < 1:
        raise ValueError("aggregation requires independent fact and style critics")
    if role_counts["adversarial"] < args.minimum_adversarial_critics:
        raise ValueError(
            f"aggregation requires {args.minimum_adversarial_critics} adversarial critics"
        )
    result = aggregate_case_reviews(case_ids, review_sets, require_adversarial=True)
    accepted_cases = sorted(
        case_id for case_id, decision in result.decisions.items() if decision is ReviewDecision.ACCEPT
    )
    rejected_cases = sorted(set(case_ids).difference(accepted_cases))
    accepted_seed_ids = sorted(str(private_index[case_id]) for case_id in accepted_cases)
    if len(accepted_seed_ids) != len(set(accepted_seed_ids)):
        raise ValueError("accepted cases map to duplicate seed identities")
    reason_counts = Counter(
        reason
        for case_id in rejected_cases
        for reason in result.rejection_reasons.get(case_id, ())
    )
    disagreement_count = sum(
        1
        for case_id in case_ids
        if len(
            {
                review["acceptable"]
                for reviews in review_sets
                for review in reviews
                if review["case_id"] == case_id
            }
        )
        > 1
    )
    acceptance_rate = len(accepted_cases) / len(case_ids)
    accepted_payload = {
        "schema_version": 1,
        "package_sha256": package_sha256,
        "accepted_seed_ids": accepted_seed_ids,
        "critic_ids": dict(sorted(critic_roles.items())),
        "approval_rule": "all_independent_critics_acceptable_no_critical_flags",
    }
    report = {
        "schema_version": 1,
        "package_sha256": package_sha256,
        "case_count": len(case_ids),
        "accepted_count": len(accepted_cases),
        "rejected_count": len(rejected_cases),
        "acceptance_rate": acceptance_rate,
        "minimum_acceptance": args.minimum_acceptance,
        "critic_count": len(critic_roles),
        "critic_role_counts": dict(sorted(role_counts.items())),
        "critic_ids": dict(sorted(critic_roles.items())),
        "disagreement_count": disagreement_count,
        "disagreement_rate": disagreement_count / len(case_ids),
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "human_curation": False,
        "generative_api": False,
    }
    _write_json(args.accepted_output, accepted_payload)
    _write_json(args.report, report)
    if acceptance_rate < args.minimum_acceptance:
        raise RuntimeError(
            f"critic acceptance {acceptance_rate:.4%} is below required {args.minimum_acceptance:.2%}"
        )
    print(
        f"critic_aggregation=pass accepted={len(accepted_cases)} rejected={len(rejected_cases)} "
        f"acceptance_rate={acceptance_rate:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

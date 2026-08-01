"""Build a clean-room JSONL package for independent data critics."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.critic_package import build_blinded_case_index, build_blinded_cases  # noqa: E402


def _load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                raise ValueError(f"blank line in {path} at {line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"non-object in {path} at {line_number}")
            records.append(value)
    return records


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-root", type=Path, nargs="+", required=True)
    parser.add_argument("--target-root", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--private-index",
        type=Path,
        help="Optional private case-to-seed index; never expose this file to critics.",
    )
    parser.add_argument("--blind-seed", type=int, default=270_003)
    parser.add_argument(
        "--allow-missing-targets",
        action="store_true",
        help="Exclude seed plans explicitly rejected before target writing.",
    )
    args = parser.parse_args()
    plans = _load_jsonl(
        [path for root in args.seed_root for path in root.rglob("plans.jsonl")]
    )
    targets = _load_jsonl(
        [path for root in args.target_root for path in root.rglob("targets.jsonl")]
    )
    target_seed_ids = {target.get("seed_id") for target in targets}
    if args.allow_missing_targets:
        selected_plans = [plan for plan in plans if plan.get("seed_id") in target_seed_ids]
    else:
        selected_plans = plans
    cases = build_blinded_cases(selected_plans, targets, blind_seed=args.blind_seed)
    package_bytes = ("\n".join(_canonical_json(case) for case in cases) + "\n").encode("utf-8")
    manifest = {
        "manifest_version": 1,
        "package_schema_version": 1,
        "blind_seed": args.blind_seed,
        "case_count": len(cases),
        "source_plan_count": len(plans),
        "excluded_plan_count": len(plans) - len(selected_plans),
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "contains_agent_identity": False,
        "contains_split": False,
        "contains_model_identity": False,
        "contains_training_history": False,
    }
    if args.private_index is not None:
        private_index = build_blinded_case_index(
            selected_plans,
            targets,
            blind_seed=args.blind_seed,
        )
        private_index_payload = (_canonical_json(private_index) + "\n").encode("utf-8")
        args.private_index.parent.mkdir(parents=True, exist_ok=True)
        args.private_index.write_bytes(private_index_payload)
        manifest["private_index_sha256"] = hashlib.sha256(private_index_payload).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(package_bytes)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
    print(f"critic_package=complete cases={len(cases)} sha256={manifest['package_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

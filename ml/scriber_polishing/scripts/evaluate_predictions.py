"""Evaluate one complete candidate JSONL against immutable frozen references."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_gate import (  # noqa: E402
    evaluate_candidate_gate,
    load_evaluation_config,
)
from scriber_polishing.evaluation_io import (  # noqa: E402
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)
from scriber_polishing.metrics import evaluate_predictions  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--gate-candidate")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.candidate.strip():
        raise ValueError("--candidate must be non-empty")
    gate_candidate = args.gate_candidate or args.candidate
    if not gate_candidate.strip():
        raise ValueError("--gate-candidate must be non-empty")

    references = load_evaluation_references(args.references)
    predictions = load_candidate_predictions(args.predictions)
    semantic_plans = {reference["case_id"]: reference["semantic_plan"] for reference in references}
    address_modes = {reference["case_id"]: reference.get("address_mode") for reference in references}
    cases = tuple(
        replace(
            case,
            semantic_plan=semantic_plans[case.case_id],
            address_mode=address_modes[case.case_id],
        )
        for case in join_evaluation_cases(references, predictions)
    )
    metrics = evaluate_predictions(cases)
    serialized_metrics = asdict(metrics)
    config = load_evaluation_config(args.config)
    gate = evaluate_candidate_gate(
        serialized_metrics,
        config,
        candidate=gate_candidate,
    )
    report = {
        "schema_version": 1,
        "candidate": args.candidate,
        "evaluation_config_sha256": _sha256(args.config),
        "reference_sha256": _sha256(args.references),
        "prediction_sha256": _sha256(args.predictions),
        "exact_case_coverage": True,
        "metrics": serialized_metrics,
        "gate": gate,
    }
    if args.gate_candidate is not None:
        report["gate_candidate"] = gate_candidate
    _write_json(args.output, report)
    print(
        f"evaluation=complete cases={metrics.total} parse_rate={metrics.sst_parse_rate:.6f} "
        f"gate_status={gate['status']} output={args.output}"
    )
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

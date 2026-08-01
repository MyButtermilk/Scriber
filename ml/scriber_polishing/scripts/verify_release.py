"""Recompute automatic and blinded-judge gates into one final release decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.evaluation_gate import (  # noqa: E402
    validate_evaluation_config,
    verify_release_evidence,
)
from scriber_polishing.variant_release import (  # noqa: E402
    VariantReleaseError,
    validate_variant_release_report,
)


def _load_json_evidence(path: Path, label: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value, hashlib.sha256(payload).hexdigest()


def _load_config_evidence(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = yaml.safe_load(payload)
    if not isinstance(value, dict):
        raise ValueError(f"evaluation config must be a YAML object: {path}")
    return validate_evaluation_config(value), hashlib.sha256(payload).hexdigest()


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
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--evaluation-report", type=Path, required=True)
    parser.add_argument("--judge-test-report", type=Path, required=True)
    parser.add_argument("--judge-challenge-report", type=Path, required=True)
    parser.add_argument("--variant-release-matrix-report", type=Path, required=True)
    parser.add_argument("--paired-significance-report", type=Path)
    parser.add_argument("--completed-training-report", type=Path)
    parser.add_argument("--private-hub-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config, config_sha256 = _load_config_evidence(args.config)
    evaluation_report, evaluation_report_sha256 = _load_json_evidence(
        args.evaluation_report,
        "evaluation report",
    )
    judge_test_report, judge_test_report_sha256 = _load_json_evidence(
        args.judge_test_report,
        "test judge report",
    )
    judge_challenge_report, judge_challenge_report_sha256 = _load_json_evidence(
        args.judge_challenge_report,
        "challenge judge report",
    )
    paired_significance_report: dict[str, Any] | None = None
    paired_significance_report_sha256: str | None = None
    if args.paired_significance_report is not None:
        (
            paired_significance_report,
            paired_significance_report_sha256,
        ) = _load_json_evidence(
            args.paired_significance_report,
            "paired significance report",
        )
    completed_training_report: dict[str, Any] | None = None
    completed_training_report_sha256: str | None = None
    if args.completed_training_report is not None:
        (
            completed_training_report,
            completed_training_report_sha256,
        ) = _load_json_evidence(
            args.completed_training_report,
            "completed training report",
        )
    private_hub_report: dict[str, Any] | None = None
    private_hub_report_sha256: str | None = None
    if args.private_hub_report is not None:
        private_hub_report, private_hub_report_sha256 = _load_json_evidence(
            args.private_hub_report,
            "private Hub verification report",
        )
    variant_release_matrix_report_sha256: str | None = None
    try:
        (
            variant_release_matrix_report,
            variant_release_matrix_report_sha256,
        ) = _load_json_evidence(
            args.variant_release_matrix_report,
            "variant release matrix report",
        )
        matrix = validate_variant_release_report(
            variant_release_matrix_report
        )
        matrix_component = {
            "passed": matrix["passed"] is True,
            "status": matrix["status"],
            "training_fingerprint": matrix["training_fingerprint"],
            "tested_variants": matrix["tested_variants"],
            "variants": matrix["variants"],
            "ineligible_variants": matrix["ineligible_variants"],
            "qualification_failures": matrix["qualification_failures"],
            "reasons": matrix["reasons"],
        }
    except (OSError, ValueError, VariantReleaseError) as error:
        matrix_component = {
            "passed": False,
            "status": "invalid",
            "reasons": [str(error)],
        }
    decision = verify_release_evidence(
        evaluation_report,
        judge_test_report,
        judge_challenge_report,
        config,
        candidate=args.candidate,
        config_sha256=config_sha256,
        paired_significance_report=paired_significance_report,
        evaluation_report_sha256=evaluation_report_sha256,
        completed_training_report=completed_training_report,
        completed_training_report_sha256=completed_training_report_sha256,
        private_hub_report=private_hub_report,
        private_hub_report_sha256=private_hub_report_sha256,
        variant_release_component=matrix_component,
    )
    decision["evidence_sha256"] = {
        "evaluation_report": evaluation_report_sha256,
        "judge_test_report": judge_test_report_sha256,
        "judge_challenge_report": judge_challenge_report_sha256,
    }
    if variant_release_matrix_report_sha256 is not None:
        decision["evidence_sha256"][
            "variant_release_matrix_report"
        ] = variant_release_matrix_report_sha256
    if paired_significance_report_sha256 is not None:
        decision["evidence_sha256"][
            "paired_significance_report"
        ] = paired_significance_report_sha256
    if completed_training_report_sha256 is not None:
        decision["evidence_sha256"][
            "completed_training_report"
        ] = completed_training_report_sha256
    if private_hub_report_sha256 is not None:
        decision["evidence_sha256"]["private_hub_report"] = private_hub_report_sha256
    _write_json(args.output, decision)
    print(
        f"release_verification={decision['status']} candidate={args.candidate} "
        f"failed_components={len(decision['failed_components'])} output={args.output}"
    )
    if decision["status_basis"]["ai_judging_pending"]:
        return 3
    return 0 if decision["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

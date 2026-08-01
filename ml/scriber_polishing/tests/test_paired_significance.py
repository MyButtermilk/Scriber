from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.paired_significance import compare_paired_evaluations


def _evaluation_report(candidate: str, values: tuple[bool, ...]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": candidate,
        "evaluation_config_sha256": "a" * 64,
        "reference_sha256": "b" * 64,
        "prediction_sha256": "c" * 64,
        "exact_case_coverage": True,
        "metrics": {
            "case_results": [
                {"case_id": f"case-{index:03d}", "normalized_exact_match": value}
                for index, value in enumerate(values, start=1)
            ]
        },
    }


def test_paired_significance_is_seeded_case_matched_and_hash_bound() -> None:
    base = _evaluation_report("base_model", (False, False, False))
    fine_tuned = _evaluation_report("final", (True, True, True))

    first = compare_paired_evaluations(
        base,
        fine_tuned,
        base_report_sha256="d" * 64,
        fine_tuned_report_sha256="e" * 64,
        metric="normalized_exact_match",
        seed=27,
        resamples=100,
        confidence_level=0.95,
        minimum_delta=0.0,
    )
    second = compare_paired_evaluations(
        base,
        fine_tuned,
        base_report_sha256="d" * 64,
        fine_tuned_report_sha256="e" * 64,
        metric="normalized_exact_match",
        seed=27,
        resamples=100,
        confidence_level=0.95,
        minimum_delta=0.0,
    )

    assert first == second
    assert first["status"] == "complete"
    assert first["decision"] == "SIGNIFICANT_IMPROVEMENT"
    assert first["inputs"]["base_report_sha256"] == "d" * 64
    assert first["paired_delta"]["mean"] == 1.0
    assert first["bootstrap"]["lower"] == 1.0


def test_paired_significance_rejects_reports_from_different_reference_sets() -> None:
    base = _evaluation_report("base_model", (False, False))
    fine_tuned = _evaluation_report("final", (True, True))
    fine_tuned["reference_sha256"] = "f" * 64

    with pytest.raises(ValueError, match="reference SHA-256"):
        compare_paired_evaluations(
            base,
            fine_tuned,
            base_report_sha256="d" * 64,
            fine_tuned_report_sha256="e" * 64,
            metric="normalized_exact_match",
            seed=27,
            resamples=100,
            confidence_level=0.95,
            minimum_delta=0.0,
        )


def test_paired_significance_cli_writes_an_immutable_hash_bound_report(tmp_path: Path) -> None:
    base_path = tmp_path / "base.json"
    fine_tuned_path = tmp_path / "fine-tuned.json"
    output_path = tmp_path / "paired.json"
    base_path.write_text(json.dumps(_evaluation_report("base_model", (False, False))), encoding="utf-8")
    fine_tuned_path.write_text(json.dumps(_evaluation_report("final", (True, True))), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "compare_paired_evaluations.py"),
            "--base-report",
            str(base_path),
            "--fine-tuned-report",
            str(fine_tuned_path),
            "--resamples",
            "100",
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["decision"] == "SIGNIFICANT_IMPROVEMENT"
    assert report["inputs"]["case_count"] == 2

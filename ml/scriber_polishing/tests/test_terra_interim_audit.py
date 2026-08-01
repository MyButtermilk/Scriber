from __future__ import annotations

import hashlib
import json

import pytest

from scriber_polishing.terra_interim_audit import (
    TerraPackageAudit,
    build_remediation_records,
    build_terra_interim_artifacts,
    canonical_json,
    validate_terra_interim_audit,
    validate_terra_remediation_record,
    write_terra_interim_artifacts,
)

_HASH = "a" * 64
_CASE_ID = "judge_" + "b" * 24
_SCORES = {
    "semantic_fidelity": 5,
    "no_additions": 5,
    "no_omissions": 5,
    "spelling": 5,
    "grammar": 5,
    "punctuation": 5,
    "paragraph_structure": 5,
    "heading_structure": 5,
    "numbered_lists": 5,
    "disfluency_cleanup": 5,
    "unit_normalization": 5,
    "legal_citations": 5,
    "address_pronouns": 5,
    "formatting": 5,
    "style_preservation": 5,
}


def _verdict() -> dict[str, object]:
    return {
        "schema_version": 2,
        "case_id": _CASE_ID,
        "judge_id": "judge_terra_audit_test",
        "judge_session_id": "session_terra_audit_test_v1",
        "judge_stage": "terra_prejudge",
        "model_family": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "prompt_hash": "sha256:" + _HASH,
        "reviewed_bundle_sha256": _HASH,
        "stage_assignment_sha256": _HASH,
        "candidate": "B",
        "acceptable": True,
        "critical_error": False,
        "scores": dict(_SCORES),
        "flags": [],
        "brief_reason": "synthetic",
    }


def _provenance(comparison_kind: str) -> dict[str, str]:
    return {
        "comparison_kind": comparison_kind,
        "judge_stage": "terra_prejudge",
        "judge_id": "judge_terra_audit_test",
        "judge_session_id": "session_terra_audit_test_v1",
        "model_family": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "prompt_hash": "sha256:" + _HASH,
        "stage_assignment_sha256": _HASH,
        "verdict_output_sha256": _HASH,
        "stage_packet_sha256": _HASH,
        "stage_packet_manifest_sha256": _HASH,
        "bundle_sha256": _HASH,
        "bundle_manifest_sha256": _HASH,
        "private_mapping_sha256": _HASH,
    }


def _package_summary(comparison_kind: str) -> dict[str, object]:
    return {
        "comparison_kind": comparison_kind,
        "judge_stage": "terra_prejudge",
        "package_status": "in_progress",
        "expected_case_count": 2,
        "completed_case_count": 1,
        "missing_case_count": 1,
        "coverage_fraction": 0.5,
        "judge_id": "judge_terra_audit_test",
        "judge_session_id": "session_terra_audit_test_v1",
        "model_family": "gpt-5.6-terra",
        "reasoning_effort": "max",
        "prompt_hash": "sha256:" + _HASH,
        "stage_assignment_sha256": _HASH,
        "verdict_output_sha256": _HASH,
        "resume_manifest_sha256": _HASH,
        "stage_packet_sha256": _HASH,
        "stage_packet_manifest_sha256": _HASH,
        "bundle_sha256": _HASH,
        "bundle_manifest_sha256": _HASH,
        "private_mapping_sha256": _HASH,
    }


def _audit(comparison_kind: str) -> TerraPackageAudit:
    return TerraPackageAudit(
        comparison_kind=comparison_kind,
        verdicts=(_verdict(),),
        cases_by_id={
            _CASE_ID: {
                "deterministic_critical_flags": {
                    "A": ["changed_number"],
                    "B": [],
                }
            }
        },
        private_cases={
            _CASE_ID: {
                "base_case_id": "synthetic_base_case",
                "candidate_a_label": "final",
                "candidate_b_label": "opponent",
            }
        },
        fine_tune_label="final",
        provenance=_provenance(comparison_kind),
        package_summary=_package_summary(comparison_kind),
    )


def test_backlog_is_pseudonymous_and_does_not_emit_judge_reasons_or_text() -> None:
    records = build_remediation_records(
        comparison_kind="fine_tune_vs_base",
        verdicts=(_verdict(),),
        cases_by_id=_audit("fine_tune_vs_base").cases_by_id,
        private_cases=_audit("fine_tune_vs_base").private_cases,
        fine_tune_label="final",
        provenance=_provenance("fine_tune_vs_base"),
    )

    assert len(records) == 1
    record = records[0]
    assert record["severity"] == "critical"
    assert record["failure_signals"] == [
        "deterministic_fine_tune_error",
        "pairwise_preference_loss",
    ]
    assert record["error_flags"] == ["changed_number"]
    assert "brief_reason" not in record
    assert "case_id" not in record
    assert _CASE_ID not in json.dumps(record)
    assert validate_terra_remediation_record(record) == record


def test_backlog_schema_rejects_raw_text_fields() -> None:
    record = build_remediation_records(
        comparison_kind="fine_tune_vs_base",
        verdicts=(_verdict(),),
        cases_by_id=_audit("fine_tune_vs_base").cases_by_id,
        private_cases=_audit("fine_tune_vs_base").private_cases,
        fine_tune_label="final",
        provenance=_provenance("fine_tune_vs_base"),
    )[0]
    invalid = {**record, "source_text": "must never be emitted"}

    with pytest.raises(ValueError, match="schema failed"):
        validate_terra_remediation_record(invalid)


def test_interim_artifacts_are_canonical_and_cannot_claim_release(tmp_path) -> None:
    artifacts = build_terra_interim_artifacts((_audit("fine_tune_vs_base"), _audit("fine_tune_vs_rule_baseline")))

    report = artifacts.audit_report
    assert report["status"] == "in_progress"
    assert report["release_aggregation"]["release_gate_passed"] is False
    assert validate_terra_interim_audit(report) == report
    invalid = {
        **report,
        "release_aggregation": {
            **report["release_aggregation"],
            "release_gate_passed": True,
        },
    }
    with pytest.raises(ValueError, match="schema failed"):
        validate_terra_interim_audit(invalid)

    output_dir = tmp_path / "terra-audit"
    paths = write_terra_interim_artifacts(output_dir, artifacts)
    assert paths["audit_report"].read_bytes() == (canonical_json(report) + "\n").encode("utf-8")
    assert paths["backlog"].read_text(encoding="utf-8").count("\n") == 2
    manifest = json.loads(paths["artifact_manifest"].read_bytes())
    assert (
        manifest["markdown_report_sha256"]
        == "sha256:" + hashlib.sha256(paths["markdown_report"].read_bytes()).hexdigest()
    )
    assert "synthetic" not in paths["backlog"].read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        write_terra_interim_artifacts(output_dir, artifacts)

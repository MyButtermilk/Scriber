import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scriber_polishing.curriculum_comparison import (
    aggregate_case_evaluations,
)
from scriber_polishing.metrics import EvaluationCase, evaluate_predictions
from scriber_polishing.remediation_comparison import (
    RemediationComparisonError,
    _ArmEvidence,
    _artifact,
    _comparison_report,
    _LoadedFile,
    _validate_evaluation_report,
    _validate_prediction_manifest,
    _validate_training_report,
    validate_remediation_comparison_report,
)

_RAW_A = "a" * 64
_RAW_B = "b" * 64
_RAW_C = "c" * 64
_RAW_D = "d" * 64
_RAW_E = "e" * 64
_RAW_F = "f" * 64


def _case(prediction: str) -> EvaluationCase:
    target = "[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]"
    return EvaluationCase(
        case_id="case_one",
        source_text="hallo welt",
        target_sst=target,
        target_plain_text="Hallo Welt.",
        prediction_sst=prediction,
    )


def _arm(
    mode: str,
    prediction: str,
    *,
    restoration_errors: int = 0,
) -> _ArmEvidence:
    cases = (_case(prediction),)
    evaluated = evaluate_predictions(cases)
    fingerprint_character = "1" if mode == "shuffled" else "2"
    return _ArmEvidence(
        mode=mode,
        training={
            "checkpoint_state": {
                "global_step": 10,
            }
        },
        training_sha256=_RAW_A if mode == "shuffled" else _RAW_B,
        predictions_sha256=_RAW_C if mode == "shuffled" else _RAW_D,
        prediction_manifest={
            "restoration_error_count": restoration_errors,
        },
        prediction_manifest_sha256=(
            _RAW_E if mode == "shuffled" else _RAW_F
        ),
        evaluation={
            "evaluation_config_sha256": _RAW_A,
        },
        evaluation_sha256=_RAW_B if mode == "shuffled" else _RAW_C,
        cases=cases,
        results=evaluated.case_results,
        aggregate=aggregate_case_evaluations(evaluated.case_results),
        eval_loss=1.0,
        restoration_error_count=restoration_errors,
        valid_sst_count=1,
        run_fingerprint="sha256:" + fingerprint_character * 64,
        final_model_tree_sha256=(
            "sha256:" + ("3" if mode == "shuffled" else "4") * 64
        ),
    )


def _policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed": 270014,
        "bootstrap_resamples": 10_000,
        "confidence_level": 0.95,
        "minimum_composite_delta": 0.005,
        "maximum_eval_loss_ratio": 1.02,
        "weights": {
            "safety_content": 0.60,
            "task_quality": 0.25,
            "structure": 0.10,
            "eval_loss": 0.05,
        },
        "require_no_new_critical_errors": True,
        "require_metric_non_regression": True,
    }


def _manifest() -> dict[str, object]:
    return {
        "experiment_id": "remediation_test",
        "comparison_pair_sha256": _RAW_A,
        "parity_dev": {
            "manifest": {
                "sha256": _RAW_B,
            }
        },
        "adoption_policy": {
            "comparison_config": {
                "sha256": _RAW_C,
            }
        },
    }


def _matched() -> dict[str, bool]:
    return {
        "accepted_training_ids_equal": True,
        "rejected_training_ids_equal": True,
        "training_contract_equal": True,
        "parent_checkpoint_equal": True,
        "protection_policy_equal": True,
        "parity_dev_case_ids_equal": True,
        "evaluation_config_equal": True,
    }


def _build_report(
    *,
    staged_restoration_errors: int = 0,
) -> dict[str, object]:
    shuffled = _arm(
        "shuffled",
        "[DOC]\n[P]Hallo Welt[/P]\n[/DOC]",
    )
    staged = _arm(
        "staged",
        "[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]",
        restoration_errors=staged_restoration_errors,
    )
    return _comparison_report(
        manifest_loaded=_LoadedFile(
            path=Path("bundle.json"),
            payload=b"bundle",
            sha256=_RAW_D,
        ),
        manifest=_manifest(),
        references_loaded=_LoadedFile(
            path=Path("references.jsonl"),
            payload=b"references",
            sha256=_RAW_E,
        ),
        case_ids_sha256=_RAW_F,
        policy=_policy(),
        shuffled=shuffled,
        staged=staged,
        matched=_matched(),
    )


def _with_cases(
    evidence: _ArmEvidence,
    cases: tuple[EvaluationCase, ...],
) -> _ArmEvidence:
    evaluated = evaluate_predictions(cases)
    return replace(
        evidence,
        cases=cases,
        results=evaluated.case_results,
        aggregate=aggregate_case_evaluations(evaluated.case_results),
        valid_sst_count=sum(
            result.sst_valid for result in evaluated.case_results
        ),
    )


def test_fixed_hard_first_comparison_adopts_only_with_positive_bootstrap() -> None:
    report = validate_remediation_comparison_report(_build_report())

    assert report["decision"] == "ADOPT_STAGED"
    assert report["adoption"] == "STAGED"
    assert report["hard_gates"]["passed"] is True
    assert report["bootstrap"]["lower"] > 0
    assert report["composite_delta"]["actual"] >= 0.005
    assert report["eval_loss"]["ratio"] <= 1.02
    assert report["policy"]["weights"] == {
        "safety_content": 0.60,
        "task_quality": 0.25,
        "structure": 0.10,
        "eval_loss": 0.05,
    }


def test_restoration_failure_keeps_shuffled_without_inventing_subcounts() -> None:
    report = validate_remediation_comparison_report(
        _build_report(staged_restoration_errors=1)
    )

    assert report["decision"] == "KEEP_SHUFFLED"
    assert report["adoption"] == "NO_ADOPTION"
    staged = report["hard_gates"]["protected_span_integrity"]["staged"]
    assert staged["successful_restoration_attempt_rate"] == 0
    assert staged["deletions"] is None
    assert staged["duplications"] is None
    assert staged["malformed"] is None
    assert staged["classification"] == (
        "unclassified_failure_no_zero_claim"
    )


def test_new_criticals_are_paired_by_case_not_hidden_by_equal_totals() -> None:
    exact = "[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]"
    shuffled = _with_cases(
        _arm("shuffled", exact),
        (
            replace(_case("invalid"), case_id="case_one"),
            replace(_case(exact), case_id="case_two"),
        ),
    )
    staged = _with_cases(
        _arm("staged", exact),
        (
            replace(_case(exact), case_id="case_one"),
            replace(_case("invalid"), case_id="case_two"),
        ),
    )
    report = _comparison_report(
        manifest_loaded=_LoadedFile(
            path=Path("bundle.json"),
            payload=b"bundle",
            sha256=_RAW_D,
        ),
        manifest=_manifest(),
        references_loaded=_LoadedFile(
            path=Path("references.jsonl"),
            payload=b"references",
            sha256=_RAW_E,
        ),
        case_ids_sha256=_RAW_F,
        policy=_policy(),
        shuffled=shuffled,
        staged=staged,
        matched=_matched(),
    )

    critical = report["hard_gates"]["new_critical_errors"]
    assert critical["actual"] > 0
    assert critical["checks"]["invalid_sst"]["shuffled"] == 1
    assert critical["checks"]["invalid_sst"]["staged"] == 1
    assert critical["checks"]["invalid_sst"]["new_case_ids"] == [
        "case_two"
    ]
    assert report["decision"] == "KEEP_SHUFFLED"


def test_report_validator_rejects_decision_and_completion_tampering() -> None:
    report = _build_report()
    decision_tamper = copy.deepcopy(report)
    decision_tamper["decision"] = "KEEP_SHUFFLED"
    with pytest.raises(
        RemediationComparisonError,
        match="adoption decision",
    ):
        validate_remediation_comparison_report(decision_tamper)

    completion_tamper = copy.deepcopy(report)
    completion_tamper["candidates"]["staged"][
        "actual_completed_steps"
    ] = 9
    with pytest.raises(
        RemediationComparisonError,
        match="identity/completion",
    ):
        validate_remediation_comparison_report(completion_tamper)


def test_evaluation_report_must_equal_independent_recomputation() -> None:
    case = _case("[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]")
    serialized = json.loads(
        json.dumps(asdict(evaluate_predictions((case,))))
    )
    evaluation = {
        "schema_version": 1,
        "candidate": "staged",
        "evaluation_config_sha256": _RAW_A,
        "reference_sha256": _RAW_B,
        "prediction_sha256": _RAW_C,
        "exact_case_coverage": True,
        "metrics": copy.deepcopy(serialized),
    }
    _validate_evaluation_report(
        evaluation,
        mode="staged",
        references_sha256=_RAW_B,
        predictions_sha256=_RAW_C,
        metrics=serialized,
    )
    evaluation["metrics"]["total"] = 2
    with pytest.raises(
        RemediationComparisonError,
        match="independent recomputation",
    ):
        _validate_evaluation_report(
            evaluation,
            mode="staged",
            references_sha256=_RAW_B,
            predictions_sha256=_RAW_C,
            metrics=serialized,
        )


def test_prediction_manifest_distinguishes_restoration_from_raw_sst_parse(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model"
    results = evaluate_predictions(
        (_case("[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]"),)
    ).case_results
    policy_sha256 = "sha256:" + _RAW_A
    manifest = {
        "schema_version": 1,
        "candidate": "shuffled",
        "record_count": 1,
        "valid_sst_count": 0,
        "restoration_error_count": 1,
        "prediction_sha256": _RAW_C,
        "reference_sha256": _RAW_B,
        "model": str(model_path),
        "quantization": "none",
        "protection_policy": {
            "policy_id": "gemma_unused_v1",
            "policy_sha256": policy_sha256,
            "artifact_sha256": _RAW_D,
        },
        "total_generation_seconds": 1.0,
        "total_input_tokens": 10,
        "total_output_tokens": 20,
    }

    restoration_errors, valid_sst_count = _validate_prediction_manifest(
        manifest,
        loaded=_LoadedFile(
            path=tmp_path / "manifest.json",
            payload=b"manifest",
            sha256=_RAW_E,
        ),
        mode="shuffled",
        training={"final_model": {"path": str(model_path)}},
        references_sha256=_RAW_B,
        predictions_sha256=_RAW_C,
        case_count=1,
        results=results,
        experiment={
            "inputs": {
                "protection_policy": {
                    "policy_id": "gemma_unused_v1",
                    "policy_sha256": policy_sha256,
                    "artifact_sha256": _RAW_D,
                }
            }
        },
    )

    assert restoration_errors == 1
    assert valid_sst_count == 0


def test_adapter_rejects_pilot_reports_before_any_metric_use() -> None:
    payload = json.dumps(
        {
            "schema_version": 2,
            "run_kind": "pilot",
            "training_completed": True,
        }
    ).encode()
    loaded = _LoadedFile(
        path=Path("pilot.json"),
        payload=payload,
        sha256=_RAW_A,
    )
    with pytest.raises(
        RemediationComparisonError,
        match="not a completed improvement report",
    ):
        _validate_training_report(
            loaded,
            mode="staged",
            manifest={},
        )


def test_bundle_artifact_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    artifact_path = tmp_path / "plan.json"
    artifact_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        RemediationComparisonError,
        match="SHA-256 mismatch",
    ):
        _artifact(
            tmp_path,
            {
                "filename": artifact_path.name,
                "sha256": _RAW_A,
            },
            "test plan",
        )

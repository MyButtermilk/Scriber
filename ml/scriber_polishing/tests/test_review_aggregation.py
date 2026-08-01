from copy import deepcopy

import pytest

from scriber_polishing.review_aggregation import ReviewDecision, aggregate_case_reviews

VALID_VERDICT = {
    "schema_version": 1,
    "case_id": "case_" + "a" * 24,
    "critic_id": "critic_fact_terra_01",
    "critic_role": "fact",
    "model_family": "gpt-5.6-terra",
    "reasoning_effort": "max",
    "prompt_hash": "sha256:" + "b" * 64,
    "reviewed_package_sha256": "c" * 64,
    "acceptable": True,
    "critical_flags": [],
    "noncritical_flags": [],
    "confidence": "high",
    "summary_code": "all_checks_passed",
}


def _review(case_id: str, critic_id: str, role: str, acceptable: bool = True) -> dict[str, object]:
    value = deepcopy(VALID_VERDICT)
    value.update(
        {
            "case_id": case_id,
            "critic_id": critic_id,
            "critic_role": role,
            "model_family": "gpt-5.6-sol" if role == "adversarial" else "gpt-5.6-terra",
            "acceptable": acceptable,
            "summary_code": "acceptable" if acceptable else "style_rejected",
        }
    )
    return value


def test_review_aggregation_requires_independent_fact_style_and_adversarial_approval() -> None:
    case_id = "case_" + "a" * 24
    result = aggregate_case_reviews(
        [case_id],
        [
            [_review(case_id, "critic_fact_terra_01", "fact")],
            [_review(case_id, "critic_style_terra_01", "style")],
            [_review(case_id, "critic_adversarial_sol_01", "adversarial")],
            [_review(case_id, "critic_adversarial_sol_02", "adversarial")],
        ],
        require_adversarial=True,
    )

    assert result.decisions[case_id] is ReviewDecision.ACCEPT
    assert result.rejection_reasons == {}


def test_review_aggregation_rejects_missing_disagreement_and_critical_flags() -> None:
    accepted_case = "case_" + "a" * 24
    critical_case = "case_" + "b" * 24
    critical = _review(critical_case, "critic_adversarial_sol_01", "adversarial", False)
    critical["critical_flags"] = ["changed_number"]

    result = aggregate_case_reviews(
        [accepted_case, critical_case],
        [
            [
                _review(accepted_case, "critic_fact_terra_01", "fact"),
                _review(critical_case, "critic_fact_terra_01", "fact"),
            ],
            [
                _review(accepted_case, "critic_style_terra_01", "style", False),
                _review(critical_case, "critic_style_terra_01", "style"),
            ],
            [critical],
        ],
        require_adversarial=True,
    )

    assert result.decisions[accepted_case] is ReviewDecision.REJECT
    assert "style_rejected" in result.rejection_reasons[accepted_case]
    assert result.decisions[critical_case] is ReviewDecision.REJECT
    assert "critical_flag:changed_number" in result.rejection_reasons[critical_case]


def test_review_aggregation_rejects_duplicate_case_verdict_from_same_critic() -> None:
    case_id = "case_" + "a" * 24
    duplicate = _review(case_id, "critic_fact_terra_01", "fact")

    with pytest.raises(ValueError, match="duplicate verdict"):
        aggregate_case_reviews(
            [case_id],
            [[duplicate, duplicate]],
            require_adversarial=False,
        )

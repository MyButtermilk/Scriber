from copy import deepcopy

import pytest

from scriber_polishing.schemas import validate_critic_verdict

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


def test_critic_verdict_schema_accepts_complete_blinded_evidence() -> None:
    assert validate_critic_verdict(VALID_VERDICT) == VALID_VERDICT


def test_critic_verdict_schema_rejects_approval_with_critical_flags() -> None:
    invalid = deepcopy(VALID_VERDICT)
    invalid["critical_flags"] = ["changed_number"]

    with pytest.raises(ValueError, match="critic verdict failed"):
        validate_critic_verdict(invalid)


def test_critic_verdict_schema_binds_roles_to_required_model_families() -> None:
    adversarial = deepcopy(VALID_VERDICT)
    adversarial.update(
        {
            "critic_id": "critic_adversarial_sol_01",
            "critic_role": "adversarial",
            "model_family": "gpt-5.6-sol",
        }
    )
    assert validate_critic_verdict(adversarial) == adversarial

    wrong_adversarial_model = deepcopy(adversarial)
    wrong_adversarial_model["model_family"] = "gpt-5.6-terra"
    with pytest.raises(ValueError, match="critic verdict failed"):
        validate_critic_verdict(wrong_adversarial_model)

    wrong_fact_model = deepcopy(VALID_VERDICT)
    wrong_fact_model["model_family"] = "gpt-5.6-sol"
    with pytest.raises(ValueError, match="critic verdict failed"):
        validate_critic_verdict(wrong_fact_model)

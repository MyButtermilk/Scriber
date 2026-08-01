from scriber_polishing.critic_aggregation import (
    CriticRole,
    CriticVerdict,
    ExampleRisk,
    ReleaseDecision,
    aggregate_critic_verdicts,
)


def verdict(
    *,
    agent: str,
    role: CriticRole,
    acceptable: bool,
    critical_flags: tuple[str, ...] = (),
) -> CriticVerdict:
    return CriticVerdict(
        agent_id=agent,
        role=role,
        acceptable=acceptable,
        critical_flags=critical_flags,
    )


def test_generator_cannot_approve_its_own_example() -> None:
    decision = aggregate_critic_verdicts(
        generator_agent_id="agent-a",
        risk=ExampleRisk.NORMAL,
        deterministic_valid=True,
        verdicts=(
            verdict(agent="agent-a", role=CriticRole.FACT, acceptable=True),
            verdict(agent="agent-style", role=CriticRole.STYLE, acceptable=True),
        ),
    )

    assert decision is ReleaseDecision.REJECT


def test_normal_example_requires_independent_fact_and_style_approval() -> None:
    decision = aggregate_critic_verdicts(
        generator_agent_id="agent-a",
        risk=ExampleRisk.NORMAL,
        deterministic_valid=True,
        verdicts=(
            verdict(agent="agent-fact", role=CriticRole.FACT, acceptable=True),
            verdict(agent="agent-style", role=CriticRole.STYLE, acceptable=True),
        ),
    )

    assert decision is ReleaseDecision.ACCEPT


def test_high_risk_example_requires_adversarial_approval() -> None:
    verdicts = (
        verdict(agent="agent-fact", role=CriticRole.FACT, acceptable=True),
        verdict(agent="agent-style", role=CriticRole.STYLE, acceptable=True),
    )

    assert (
        aggregate_critic_verdicts(
            generator_agent_id="agent-a",
            risk=ExampleRisk.HIGH,
            deterministic_valid=True,
            verdicts=verdicts,
        )
        is ReleaseDecision.NEEDS_TIE_BREAK
    )

    assert (
        aggregate_critic_verdicts(
            generator_agent_id="agent-a",
            risk=ExampleRisk.HIGH,
            deterministic_valid=True,
            verdicts=(
                *verdicts,
                verdict(agent="agent-sol", role=CriticRole.ADVERSARIAL, acceptable=True),
            ),
        )
        is ReleaseDecision.ACCEPT
    )


def test_any_possible_fact_error_rejects_without_majority_override() -> None:
    decision = aggregate_critic_verdicts(
        generator_agent_id="agent-a",
        risk=ExampleRisk.HIGH,
        deterministic_valid=True,
        verdicts=(
            verdict(agent="agent-fact", role=CriticRole.FACT, acceptable=True),
            verdict(agent="agent-style", role=CriticRole.STYLE, acceptable=True),
            verdict(
                agent="agent-sol",
                role=CriticRole.ADVERSARIAL,
                acceptable=False,
                critical_flags=("changed_number",),
            ),
            verdict(agent="agent-tie", role=CriticRole.FACT, acceptable=True),
        ),
    )

    assert decision is ReleaseDecision.REJECT


def test_failed_deterministic_validation_cannot_be_overridden() -> None:
    decision = aggregate_critic_verdicts(
        generator_agent_id="agent-a",
        risk=ExampleRisk.NORMAL,
        deterministic_valid=False,
        verdicts=(
            verdict(agent="agent-fact", role=CriticRole.FACT, acceptable=True),
            verdict(agent="agent-style", role=CriticRole.STYLE, acceptable=True),
        ),
    )

    assert decision is ReleaseDecision.REJECT

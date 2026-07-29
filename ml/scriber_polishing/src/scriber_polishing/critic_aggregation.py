"""Fail-closed aggregation for independent AI critic verdicts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CriticRole(StrEnum):
    FACT = "fact"
    STYLE = "style"
    ADVERSARIAL = "adversarial"
    RELEASE_JUDGE = "release_judge"


class ExampleRisk(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    AI_GOLD = "ai_gold"
    CHALLENGE = "challenge"


class ReleaseDecision(StrEnum):
    ACCEPT = "accept"
    NEEDS_TIE_BREAK = "needs_tie_break"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    agent_id: str
    role: CriticRole
    acceptable: bool
    critical_flags: tuple[str, ...] = ()


def aggregate_critic_verdicts(
    *,
    generator_agent_id: str,
    risk: ExampleRisk,
    deterministic_valid: bool,
    verdicts: tuple[CriticVerdict, ...],
) -> ReleaseDecision:
    """Return the evidence-derived decision without letting AI override hard gates."""

    if not deterministic_valid or not generator_agent_id:
        return ReleaseDecision.REJECT

    if any(verdict.agent_id == generator_agent_id for verdict in verdicts):
        return ReleaseDecision.REJECT

    if any(verdict.critical_flags for verdict in verdicts):
        return ReleaseDecision.REJECT

    independent = tuple(verdict for verdict in verdicts if verdict.agent_id)
    facts = tuple(verdict for verdict in independent if verdict.role is CriticRole.FACT)
    styles = tuple(verdict for verdict in independent if verdict.role is CriticRole.STYLE)
    adversarial = tuple(verdict for verdict in independent if verdict.role is CriticRole.ADVERSARIAL)

    if not any(verdict.acceptable for verdict in facts) or not any(verdict.acceptable for verdict in styles):
        return ReleaseDecision.NEEDS_TIE_BREAK

    if any(not verdict.acceptable for verdict in facts):
        return ReleaseDecision.NEEDS_TIE_BREAK

    high_risk = risk in {ExampleRisk.HIGH, ExampleRisk.AI_GOLD, ExampleRisk.CHALLENGE}
    if high_risk and not any(verdict.acceptable for verdict in adversarial):
        return ReleaseDecision.NEEDS_TIE_BREAK

    if any(not verdict.acceptable for verdict in adversarial):
        return ReleaseDecision.NEEDS_TIE_BREAK

    return ReleaseDecision.ACCEPT

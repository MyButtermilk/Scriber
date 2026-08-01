from scriber_polishing.critic_aggregation import CriticRole, CriticVerdict, ExampleRisk
from scriber_polishing.gold_builder import build_gold_examples
from scriber_polishing.split_dataset import Split


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "example_id": "gold-a",
        "canonical_document_id": "doc-a",
        "source_text": "Sprecher: Bitte pruefen Sie 1.200,00 €.",
        "target_sst": "[DOC]\n[P]Sprecher: Bitte prüfen Sie 1.200,00 €.[/P]\n[/DOC]",
        "semantic_plan": {"id": "plan-a", "facts": ["1.200,00 €"]},
        "target_ast": {"version": "sst-v1"},
        "generator_agent_id": "generator-a",
        "risk": ExampleRisk.AI_GOLD,
        "deterministic_valid": True,
        "fact_roundtrip_valid": True,
        "verdicts": (
            CriticVerdict("fact-agent", CriticRole.FACT, True),
            CriticVerdict("style-agent", CriticRole.STYLE, True),
            CriticVerdict("sol-agent", CriticRole.ADVERSARIAL, True),
        ),
    }
    candidate.update(overrides)
    return candidate


def test_gold_builder_accepts_only_complete_independently_approved_records() -> None:
    result = build_gold_examples((_candidate(),), {"gold-a": Split.TEST})

    assert tuple(item["example_id"] for item in result.accepted) == ("gold-a",)
    assert result.rejected == {}


def test_gold_builder_fails_closed_for_missing_or_disagreeing_critics_and_protected_roundtrip() -> None:
    candidates = (
        _candidate(
            example_id="missing",
            canonical_document_id="doc-missing",
            source_text="Sprecher: Nachricht A.",
            target_sst="[DOC]\n[P]Sprecher: Nachricht A.[/P]\n[/DOC]",
            verdicts=(),
        ),
        _candidate(
            example_id="disagrees",
            canonical_document_id="doc-disagrees",
            source_text="Sprecher: Nachricht B.",
            target_sst="[DOC]\n[P]Sprecher: Nachricht B.[/P]\n[/DOC]",
            verdicts=(
                CriticVerdict("fact-agent", CriticRole.FACT, False),
                CriticVerdict("style-agent", CriticRole.STYLE, True),
                CriticVerdict("sol-agent", CriticRole.ADVERSARIAL, True),
            ),
        ),
        _candidate(
            example_id="protected",
            canonical_document_id="doc-protected",
            source_text="Sprecher: Betrag 1.200,00 €.",
            target_sst="[DOC]\n[P]Sprecher: Betrag 1.200,01 €.[/P]\n[/DOC]",
        ),
    )

    result = build_gold_examples(
        candidates, {"missing": Split.TRAIN, "disagrees": Split.VALIDATION, "protected": Split.CHALLENGE}
    )

    assert result.accepted == ()
    assert result.rejected["missing"] == ("critic_not_accepted",)
    assert result.rejected["disagrees"] == ("critic_not_accepted",)
    assert result.rejected["protected"] == ("protected_span_failed",)


def test_gold_builder_never_accepts_a_family_that_crosses_train_and_test() -> None:
    candidates = (
        _candidate(example_id="train", canonical_document_id="same"),
        _candidate(example_id="test", canonical_document_id="same"),
    )

    result = build_gold_examples(candidates, {"train": Split.TRAIN, "test": Split.TEST})

    assert result.accepted == ()
    assert result.rejected == {"test": ("split_leakage",), "train": ("split_leakage",)}

from scriber_polishing.pronoun_plan_repair import (
    remove_unsupported_pronoun_commentary,
    repair_pronoun_plan,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text


def _plan(fact_text: str, protected_values: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed_id": "orthopron_test_001",
        "generator_agent": "generator_orthography_pronouns_01",
        "batch_id": "orthography_pronouns_batch_01",
        "prompt_hash": "sha256:" + "a" * 64,
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "polarity": "positive",
                    "modality": "recommendation",
                    "condition": None,
                    "text": fact_text,
                    "protected_values": protected_values,
                }
            ]
        },
    }


def test_repairs_wrong_gender_direct_recipient_in_plan_without_commentary() -> None:
    plan = _plan(
        "Bitte gebt Ihr Eure Rückmeldung an Lea Dorn, damit er Euch den Stand senden kann.",
        ["Ihr", "Eure", "Lea Dorn", "er", "Euch"],
    )

    repaired = repair_pronoun_plan(
        plan,
        generator_agent="plan_repair_adversarial_v4_02",
        batch_id="adversarial_plan_repair_v4_02",
        prompt_hash="sha256:" + "b" * 64,
    )

    fact = repaired["semantic_plan"]["facts"][0]
    assert fact["text"] == ("Bitte gebt Ihr Eure Rückmeldung an Lea Dorn, damit Lea Dorn Euch den Stand senden kann.")
    assert fact["protected_values"] == ["Ihr", "Eure", "Lea Dorn", "Euch"]
    assert repaired["generator_agent"] == "plan_repair_adversarial_v4_02"
    assert repaired["batch_id"] == "adversarial_plan_repair_v4_02"


def test_repairs_wrong_gender_coordination_and_keeps_unrelated_object_pronoun() -> None:
    plan = _plan(
        "Bitte stimmen Sie sich mit Jonas Falk ab; sie hält die vereinbarte Reihenfolge fest und dokumentiert sie.",
        ["Sie", "Jonas Falk", "sie"],
    )

    repaired = repair_pronoun_plan(
        plan,
        generator_agent="plan_repair_adversarial_v4_02",
        batch_id="adversarial_plan_repair_v4_02",
        prompt_hash="sha256:" + "b" * 64,
    )

    fact = repaired["semantic_plan"]["facts"][0]
    assert fact["text"] == (
        "Bitte stimmen Sie sich mit Jonas Falk ab; Jonas Falk hält die vereinbarte "
        "Reihenfolge fest und dokumentiert sie."
    )
    assert fact["protected_values"] == ["Sie", "Jonas Falk", "sie"]


def test_removes_only_unsupported_pronoun_commentary_from_target_document() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Bitte stimmen Sie sich mit Jonas Falk ab; Jonas Falk hält die Reihenfolge fest. "
        "Das Pronomen „sie“ bezieht sich dabei auf Jonas Falk.[/P]\n"
        "[/DOC]"
    )

    repaired = remove_unsupported_pronoun_commentary(document)

    assert render_plain_text(repaired) == (
        "Bitte stimmen Sie sich mit Jonas Falk ab; Jonas Falk hält die Reihenfolge fest."
    )


def test_pronoun_plan_repair_fails_when_no_supported_pattern_changes() -> None:
    plan = _plan("Die Unterlagen bleiben unverändert.", ["Unterlagen"])

    try:
        repair_pronoun_plan(
            plan,
            generator_agent="plan_repair_adversarial_v4_02",
            batch_id="adversarial_plan_repair_v4_02",
            prompt_hash="sha256:" + "b" * 64,
        )
    except ValueError as error:
        assert "no supported pronoun repair" in str(error)
    else:
        raise AssertionError("an unrecognized plan repair must fail closed")

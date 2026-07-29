"""Behavioural checks for the versioned polishing contract seam."""

from __future__ import annotations

from pathlib import Path

from scriber_polishing.schemas import load_behavioral_contract, validate_behavioral_contract

CONTRACT_PATH = Path(__file__).parents[1] / "contracts" / "behavioral_contract.yaml"

REQUIRED_RULE_IDS = {
    "output_only",
    "preserve_language",
    "preserve_meaning",
    "preserve_order",
    "preserve_names",
    "preserve_organizations",
    "preserve_technical_terms",
    "preserve_legal_terms",
    "preserve_numbers",
    "preserve_amounts",
    "preserve_percentages",
    "preserve_dates",
    "preserve_times",
    "preserve_negations",
    "preserve_conditions",
    "preserve_legal_references",
    "preserve_speaker_labels",
    "preserve_timestamps",
    "preserve_quotes",
    "do_not_answer_questions",
    "do_not_summarize",
    "do_not_add_content",
    "do_not_omit_content",
    "do_not_invent_headings",
    "do_not_invent_closing",
    "correct_spelling",
    "correct_grammar",
    "correct_word_order",
    "restore_punctuation",
    "restore_sentence_boundaries",
    "remove_nonsemantic_fillers",
    "preserve_semantic_fillers",
    "remove_stutters",
    "remove_accidental_repetitions",
    "remove_unnecessary_immediate_repetition",
    "preserve_intentional_repetition",
    "resolve_false_starts",
    "resolve_explicit_self_corrections",
    "interpret_spoken_punctuation",
    "interpret_spoken_formatting",
    "create_semantic_paragraphs",
    "preserve_statement_order",
    "format_subjects",
    "format_headings",
    "format_greetings",
    "format_closings",
    "format_signatures",
    "create_bullet_lists_only_when_justified",
    "create_numbered_lists_only_when_justified",
    "preserve_list_order",
    "normalize_german_numbers",
    "normalize_dates",
    "normalize_times",
    "normalize_money",
    "normalize_percentages",
    "normalize_units",
    "normalize_legal_abbreviations",
    "normalize_legal_citations",
    "capitalize_formal_address",
    "capitalize_familiar_address_in_correspondence",
    "preserve_third_person_lowercase",
    "apply_preferred_orthographic_variants",
    "accept_valid_alternative_spellings",
    "distinguish_context_sensitive_spellings",
}


def test_contract_loads_and_covers_every_goal_rule_with_a_complete_record() -> None:
    contract = load_behavioral_contract(CONTRACT_PATH)

    assert {rule["id"] for rule in contract["rules"]} == REQUIRED_RULE_IDS
    validate_behavioral_contract(contract)
    for rule in contract["rules"]:
        assert rule["criticality"] in {"critical", "high", "medium", "low"}
        assert rule["positive_examples"]
        assert rule["counterexamples"]
        assert rule["automatic_checks"]
        assert rule["ai_judge_criterion"]
        assert rule["error_code"].startswith("BC_")


def test_contract_rejects_a_rule_that_omits_a_required_quality_gate() -> None:
    contract = {
        "version": "1",
        "rules": [
            {
                "id": "output_only",
                "description": "Return only polished text.",
                "criticality": "critical",
                "positive_examples": ["Text."],
                "counterexamples": ["Kommentar: Text."],
                "automatic_checks": ["no_wrapper"],
                "error_code": "BC_OUTPUT_ONLY",
            }
        ],
    }

    try:
        validate_behavioral_contract(contract)
    except ValueError as error:
        assert "ai_judge_criterion" in str(error)
    else:
        raise AssertionError("Incomplete contract rule must be rejected")

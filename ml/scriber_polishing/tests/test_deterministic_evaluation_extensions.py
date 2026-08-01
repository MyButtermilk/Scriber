from __future__ import annotations

from scriber_polishing.metrics import EvaluationCase, evaluate_predictions


def test_text_similarity_metrics_are_exact_for_a_matching_prediction() -> None:
    target = "[DOC]\n[P]Bitte sende die Rechnung morgen.[/P]\n[/DOC]"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="text-similarity-perfect",
                source_text="Bitte sende die Rechnung morgen.",
                target_sst=target,
                prediction_sst=target,
            ),
        )
    )

    assert report.cer == 0.0
    assert report.wer == 0.0
    assert report.chrfpp_f2 == 1.0
    assert report.slice_reports["all"]["cer"] == 0.0
    assert report.capitalization_accuracy is None
    assert report.metric_availability["capitalization_accuracy"]["status"] == "unknown"


def test_annotation_backed_cleanup_metrics_measure_only_tagged_reference_cases() -> None:
    target = "[DOC]\n[P]Bitte sende die Rechnung. Morgen prüfen.[/P]\n[/DOC]"
    source = "bitte sende die rechnung morgen prüfen"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="annotated-cleanup",
                source_text=source,
                target_sst=target,
                prediction_sst=target,
                slice_tags=(
                    "operation:self_correction",
                    "operation:spoken_formatting",
                    "operation:grammar_error",
                    "operation:spelling_error",
                ),
            ),
        )
    )

    assert report.capitalization_accuracy == 1.0
    assert report.sentence_boundary_f1 == 1.0
    assert report.self_correction_recovery_accuracy == 1.0
    assert report.format_command_recovery_accuracy == 1.0
    assert report.grammar_recovery_accuracy == 1.0
    assert report.spelling_recovery_accuracy == 1.0
    assert report.metric_availability["sentence_boundary_f1"]["status"] == "measured"


def test_ast_and_semantic_plan_evidence_drive_structure_domain_and_hard_fact_checks() -> None:
    target = (
        "[DOC]\n"
        "[SUBJECT]Prüfung[/SUBJECT]\n"
        "[H1]Status[/H1]\n"
        "[P]Mara prüft 5 kWh nach § 7 Absatz 4 Satz 2 EStG.[/P]\n"
        "[OL]\n[LI1]Erster Schritt[/LI1]\n[LI2]Unterpunkt[/LI2]\n[/OL]\n"
        "[/DOC]"
    )
    semantic_plan = {
        "facts": [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Mara prüft 5 kWh.",
                "protected_values": ["Mara", "5 kWh"],
            }
        ],
        "entities": [{"type": "person", "value": "Mara", "protected": True}],
        "structure": {"heading_levels": ["h1"], "list_kind": "nested"},
    }
    complete = EvaluationCase(
        case_id="ast-semantic-complete",
        source_text="Mara prüft 5 kWh nach § 7 Absatz 4 Satz 2 EStG",
        target_sst=target,
        prediction_sst=target,
        protected_values=("Mara", "5 kWh", "§ 7 Absatz 4 Satz 2 EStG"),
        semantic_plan=semantic_plan,
    )
    changed = EvaluationCase(
        case_id="ast-semantic-changed",
        source_text=complete.source_text,
        target_sst=target,
        prediction_sst=target.replace("5 kWh", "6 kWh"),
        protected_values=complete.protected_values,
        semantic_plan=semantic_plan,
    )

    complete_report = evaluate_predictions((complete,))
    changed_report = evaluate_predictions((changed,))

    assert complete_report.structural_metrics["heading_f1"] == 1.0
    assert complete_report.structural_metrics["paragraph_boundary_f1"] == 1.0
    assert complete_report.structural_metrics["list_type_accuracy"] == 1.0
    assert complete_report.structural_metrics["list_nesting_accuracy"] == 1.0
    assert complete_report.domain_metrics["unit_symbol_exact_rate"] == 1.0
    assert complete_report.domain_metrics["legal_reference_exact_rate"] == 1.0
    assert complete_report.domain_metrics["address_pronoun_accuracy"] is None
    assert complete_report.metric_availability["domain.address_pronoun_accuracy"]["status"] == "unknown"
    assert "semantic_omission" in changed_report.case_results[0].critical_errors


def test_address_pronoun_accuracy_requires_explicit_address_mode_metadata() -> None:
    target = "[DOC]\n[P]Bitte senden Sie die Unterlagen.[/P]\n[/DOC]"
    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="formal-address",
                source_text="bitte senden sie die unterlagen",
                target_sst=target,
                prediction_sst=target,
                address_mode="formal_sie",
            ),
        )
    )

    assert report.domain_metrics["address_pronoun_accuracy"] == 1.0
    assert report.metric_availability["domain.address_pronoun_accuracy"]["status"] == "measured"


def test_semantic_hard_checks_are_conservative_and_plan_bound() -> None:
    target = "[DOC]\n[P]Alpha bleibt gültig. Beta bleibt gültig.[/P]\n[/DOC]"
    plan = {
        "facts": [
            {"fact_id": "f1", "order": 1, "protected_values": ["Alpha"]},
            {"fact_id": "f2", "order": 2, "protected_values": ["Beta"]},
        ],
        "entities": [],
    }
    reordered = EvaluationCase(
        case_id="semantic-reordered",
        source_text="Was gilt für Alpha und Beta?",
        target_sst=target,
        prediction_sst="[DOC]\n[P]Beta bleibt gültig. Alpha bleibt gültig.[/P]\n[/DOC]",
        semantic_plan=plan,
    )
    answered_and_summarized = EvaluationCase(
        case_id="semantic-answered",
        source_text=reordered.source_text,
        target_sst=target,
        prediction_sst="[DOC]\n[P]Antwort: Zusammenfassung: Alpha bleibt gültig. Beta bleibt gültig.[/P]\n[/DOC]",
        semantic_plan=plan,
    )

    reordered_result = evaluate_predictions((reordered,)).case_results[0]
    answered_result = evaluate_predictions((answered_and_summarized,)).case_results[0]
    reverse_plan_exact_result = evaluate_predictions(
        (
            EvaluationCase(
                case_id="semantic-plan-order-differs-from-reference",
                source_text=reordered.source_text,
                target_sst=target,
                prediction_sst=target,
                semantic_plan={
                    "facts": [
                        {"fact_id": "f2", "order": 1, "protected_values": ["Beta"]},
                        {"fact_id": "f1", "order": 2, "protected_values": ["Alpha"]},
                    ],
                    "entities": [],
                },
            ),
        )
    ).case_results[0]

    assert "semantic_reorder" in reordered_result.critical_errors
    assert {"semantic_addition", "semantic_question_answer", "semantic_summary"}.issubset(
        answered_result.critical_errors
    )
    assert "semantic_reorder" not in reverse_plan_exact_result.critical_errors


def test_spoken_formatting_proxy_requires_an_exact_reference_ast() -> None:
    target = "[DOC]\n[H1]Nächste Schritte[/H1]\n[/DOC]"
    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="formatting-ast",
                source_text="überschrift nächste schritte",
                target_sst=target,
                prediction_sst="[DOC]\n[P]Nächste Schritte[/P]\n[/DOC]",
                slice_tags=("operation:spoken_formatting",),
            ),
        )
    )

    assert report.format_command_recovery_accuracy == 0.0

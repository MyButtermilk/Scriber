from dataclasses import asdict

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.metrics import EvaluationCase, evaluate_predictions
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text

VALID_SST = "[DOC]\n[P]Der Betrag beträgt 2.500 €.[/P]\n[/DOC]"


def test_evaluation_counts_parse_exact_and_protected_span_safety() -> None:
    cases = (
        EvaluationCase(
            case_id="exact",
            source_text="der betrag beträgt 2.500 euro",
            target_sst=VALID_SST,
            prediction_sst=VALID_SST,
            protected_values=("2.500",),
        ),
        EvaluationCase(
            case_id="unsafe",
            source_text="der betrag beträgt 15.000 euro",
            target_sst="[DOC]\n[P]Der Betrag beträgt 15.000 €.[/P]\n[/DOC]",
            prediction_sst="[DOC]\n[P]Der Betrag beträgt 50.000 €.[/P]\n[/DOC]",
            protected_values=("15.000",),
        ),
        EvaluationCase(
            case_id="invalid",
            source_text="bitte prüfen",
            target_sst="[DOC]\n[P]Bitte prüfen.[/P]\n[/DOC]",
            prediction_sst="Hier ist die bereinigte Fassung: Bitte prüfen.",
        ),
    )

    report = evaluate_predictions(cases)

    assert report.total == 3
    assert report.sst_parse_rate == 2 / 3
    assert report.normalized_exact_match == 1 / 3
    assert report.protected_span_exact_rate == 2 / 3
    assert report.critical_errors == {
        "changed_amount": 1,
        "changed_number": 1,
        "damaged_protected_span": 1,
        "invalid_sst": 1,
        "output_not_clean_text": 1,
        "protected_span_changed": 1,
    }


def test_evaluation_reports_ast_renderer_blocks_hard_content_and_slices() -> None:
    target = (
        "[DOC]\n[SUBJECT]Prüfung[/SUBJECT]\n[H2]Nächste Schritte[/H2]\n"
        "[OL]\n[LI1]Bitte zahlen Sie 15.000,00 € bis 31.07.2026.[/LI1]\n"
        "[LI2]Beachten Sie § 7 Absatz 4 Satz 2 EStG.[/LI2]\n[/OL]\n[/DOC]"
    )
    target_document = parse_sst(target)
    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="perfect",
                source_text="Prüfung. Nächste Schritte. Bitte zahlen Sie 15.000,00 € bis 31.07.2026.",
                target_sst=target,
                prediction_sst=target,
                target_plain_text=render_plain_text(target_document),
                target_ast=document_to_dict(target_document),
                protected_values=("15.000,00 €", "31.07.2026"),
                slice_tags=("challenge", "amounts", "legal"),
            ),
        )
    )

    assert report.output_only_compliance_rate == 1.0
    assert report.ast_exact_match_rate == 1.0
    assert report.renderer_round_trip_rate == 1.0
    assert report.safe_renderer_rate == 1.0
    assert report.block_sequence_exact_rate == 1.0
    assert report.block_type_f1 == 1.0
    assert report.block_metrics["heading_2"]["f1"] == 1.0
    assert report.block_metrics["ordered_list"]["f1"] == 1.0
    assert all(rate == 1.0 for rate in report.hard_content_exact_rates.values())
    assert report.hard_gate_passed is True
    assert report.case_results[0].critical_errors == ()
    assert set(report.slice_reports) == {"all", "amounts", "challenge", "legal"}
    assert report.slice_reports["challenge"]["hard_gate_passed"] is True
    assert report.slice_reports["challenge"]["block_metrics"]["ordered_list"]["f1"] == 1.0
    assert asdict(report)["case_results"][0]["case_id"] == "perfect"


def test_evaluation_accepts_render_equivalent_implicit_plain_inlines() -> None:
    target = "[DOC]\n[P]Bitte prüfen.[/P]\n[/DOC]"
    target_document = parse_sst(target)
    target_ast = document_to_dict(target_document)
    target_ast["blocks"][0]["inlines"] = []

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="implicit-plain-inlines",
                source_text="bitte prüfen",
                target_sst=target,
                prediction_sst=target,
                target_plain_text=render_plain_text(target_document),
                target_ast=target_ast,
            ),
        )
    )

    assert report.total == 1
    assert report.ast_exact_match_rate == 1.0


@pytest.mark.parametrize(
    "unit",
    (
        "kWh/(m²·a)",
        "kWh/m²a",
        "€/m²",
        "m²",
        "mm²",
        "cm²",
        "km²",
        "m³",
        "mm³",
        "cm³",
    ),
)
def test_evaluation_maps_nfkc_normalized_superscript_units(unit: str) -> None:
    target = f"[DOC]\n[P]Der Messwert beträgt 12 {unit}.[/P]\n[/DOC]"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id=f"unit-{unit}",
                source_text=f"der messwert beträgt 12 {unit}",
                target_sst=target,
                prediction_sst=target,
            ),
        )
    )

    assert report.hard_content_exact_rates["unit_dimensions"] == 1.0


def test_evaluation_exposes_every_hard_content_mismatch_and_never_passes_the_gate() -> None:
    target = (
        "[DOC]\n[P]Nicht unter der Voraussetzung, dass Sie bis 31.07.2026 um 14:30 Uhr "
        "15.000,00 € (0,01 %) für § 7 Absatz 4 Satz 2 EStG und 25 kWh zahlen müssen.[/P]\n[/DOC]"
    )
    prediction = (
        "[DOC]\n[P]Falls Sie bis 30.07.2026 um 15:30 Uhr 50.000,00 € (0,1 %) "
        "für § 7 Absatz 3 Satz 2 EStG und 25 kW zahlen können.[/P]\n[/DOC]"
    )
    target_document = parse_sst(target)

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="unsafe",
                source_text=render_plain_text(target_document),
                target_sst=target,
                prediction_sst=prediction,
                target_plain_text=render_plain_text(target_document),
                target_ast=document_to_dict(target_document),
                protected_values=("31.07.2026", "15.000,00 €"),
                slice_tags=("challenge",),
            ),
        )
    )

    assert report.hard_gate_passed is False
    assert report.protected_span_exact_rate == 0.0
    assert report.hard_content_exact_rates == {
        "amounts": 0.0,
        "conditions": 0.0,
        "dates": 0.0,
        "legal_references": 0.0,
        "modalities": 0.0,
        "negations": 0.0,
        "numbers": 0.0,
        "percentages": 0.0,
        "protected_spans": 0.0,
        "times": 0.0,
        "unit_dimensions": 0.0,
        "units": 0.0,
    }
    assert set(report.case_results[0].critical_errors) == {
        "changed_amount",
        "changed_condition",
        "changed_date_or_time",
        "changed_legal_reference",
        "changed_modality",
        "changed_negation",
        "changed_number",
        "changed_percentage",
        "changed_unit_dimension",
        "damaged_protected_span",
        "false_unit_normalization",
        "protected_span_changed",
    }
    assert report.case_results[0].hard_content["amounts"]["reference"] == ("15.000,00 €",)
    assert report.case_results[0].hard_content["amounts"]["candidate"] == ("50.000,00 €",)
    assert report.case_results[0].hard_content["amounts"]["missing"] == ("15.000,00 €",)
    assert report.case_results[0].hard_content["amounts"]["added"] == ("50.000,00 €",)


def test_evaluation_identity_and_invalid_output_are_reported_fail_closed() -> None:
    target = "[DOC]\n[P]Der Text bleibt unverändert.[/P]\n[/DOC]"
    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="identity",
                source_text="Der Text bleibt unverändert.",
                target_sst=target,
                prediction_sst="Hier ist die bereinigte Fassung: Der Text bleibt unverändert.",
                target_plain_text="Der Text bleibt unverändert.",
                target_ast=document_to_dict(parse_sst(target)),
                slice_tags=("identity",),
                is_identity=True,
            ),
        )
    )

    assert report.identity_total == 1
    assert report.identity_exact_match == 0.0
    assert report.sst_parse_rate == 0.0
    assert report.output_only_compliance_rate == 0.0
    assert report.safe_renderer_rate == 0.0
    assert report.hard_gate_passed is False
    assert report.critical_errors["invalid_sst"] == 1
    assert report.critical_errors["output_not_clean_text"] == 1


def test_evaluation_rejects_a_reference_whose_ast_or_plain_text_is_not_frozen_consistently() -> None:
    target = "[DOC]\n[P]Referenz.[/P]\n[/DOC]"
    wrong_ast = document_to_dict(parse_sst("[DOC]\n[P]Andere Referenz.[/P]\n[/DOC]"))

    with pytest.raises(ValueError, match="reference AST"):
        evaluate_predictions(
            (
                EvaluationCase(
                    case_id="bad-reference",
                    source_text="Referenz.",
                    target_sst=target,
                    prediction_sst=target,
                    target_plain_text="Referenz.",
                    target_ast=wrong_ast,
                ),
            )
        )


def test_protected_span_metric_preserves_repeated_occurrence_counts() -> None:
    target = "[DOC]\n[P]Ticket AB-123 verweist erneut auf Ticket AB-123.[/P]\n[/DOC]"
    prediction = "[DOC]\n[P]Ticket AB-123 wurde nur einmal genannt.[/P]\n[/DOC]"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="repeated-protected",
                source_text="Ticket AB-123 verweist erneut auf Ticket AB-123.",
                target_sst=target,
                prediction_sst=prediction,
                protected_values=("AB-123",),
            ),
        )
    )

    evidence = report.case_results[0].hard_content["protected_spans"]
    assert evidence["reference"] == ("AB-123", "AB-123")
    assert evidence["candidate"] == ("AB-123",)
    assert evidence["missing"] == ("AB-123",)
    assert "damaged_protected_span" in report.case_results[0].critical_errors


def test_hard_content_sequence_reordering_is_not_hidden_by_matching_multisets() -> None:
    target = "[DOC]\n[P]Sie kann 15 Punkte prüfen und muss 20 Punkte freigeben.[/P]\n[/DOC]"
    prediction = "[DOC]\n[P]Sie muss 20 Punkte freigeben und kann 15 Punkte prüfen.[/P]\n[/DOC]"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="reordered-values",
                source_text="Sie kann 15 Punkte prüfen und muss 20 Punkte freigeben.",
                target_sst=target,
                prediction_sst=prediction,
            ),
        )
    )

    assert report.case_results[0].hard_content["numbers"]["reference"] == ("15", "20")
    assert report.case_results[0].hard_content["numbers"]["candidate"] == ("20", "15")
    assert report.case_results[0].hard_content["numbers"]["missing"] == ()
    assert report.case_results[0].hard_content["numbers"]["added"] == ()
    assert report.case_results[0].hard_content["numbers"]["exact"] is False
    assert "changed_number" in report.case_results[0].critical_errors
    assert "changed_modality" in report.case_results[0].critical_errors


def test_identity_exact_match_is_strict_even_when_normalized_exact_match_collapses_spaces() -> None:
    target = "[DOC]\n[P]Der Text bleibt exakt.[/P]\n[/DOC]"
    prediction = "[DOC]\n[P]Der  Text bleibt exakt.[/P]\n[/DOC]"

    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="identity-spacing",
                source_text="Der Text bleibt exakt.",
                target_sst=target,
                prediction_sst=prediction,
                is_identity=True,
            ),
        )
    )

    assert report.normalized_exact_match == 1.0
    assert report.exact_match_rate == 0.0
    assert report.identity_exact_match == 0.0
    assert report.case_results[0].strict_exact_match is False


def test_evaluation_reports_punctuation_filler_and_repetition_cleanup_f1_per_slice() -> None:
    target = "[DOC]\n[P]Bitte zahlen.[/P]\n[/DOC]"
    perfect = EvaluationCase(
        case_id="cleanup-perfect",
        source_text="äh bitte bitte zahlen",
        target_sst=target,
        prediction_sst=target,
        slice_tags=("cleanup",),
    )
    missed = EvaluationCase(
        case_id="cleanup-missed",
        source_text="äh bitte bitte zahlen",
        target_sst=target,
        prediction_sst="[DOC]\n[P]Äh bitte bitte zahlen[/P]\n[/DOC]",
        slice_tags=("cleanup",),
    )

    report = evaluate_predictions((perfect, missed))

    assert report.punctuation_f1 == pytest.approx(2 / 3)
    assert report.filler_f1 == pytest.approx(2 / 3)
    assert report.repetition_cleanup_f1 == pytest.approx(2 / 3)
    assert report.slice_reports["cleanup"]["punctuation_f1"] == pytest.approx(2 / 3)
    assert report.slice_reports["cleanup"]["filler_f1"] == pytest.approx(2 / 3)
    assert report.slice_reports["cleanup"]["repetition_cleanup_f1"] == pytest.approx(2 / 3)


def test_evaluation_marks_an_invented_heading_as_a_binding_deterministic_error() -> None:
    report = evaluate_predictions(
        (
            EvaluationCase(
                case_id="invented-heading",
                source_text="Status",
                target_sst="[DOC]\n[P]Status[/P]\n[/DOC]",
                prediction_sst="[DOC]\n[H1]Status[/H1]\n[/DOC]",
                slice_tags=("split:challenge",),
            ),
        )
    )

    assert "invented_heading" in report.case_results[0].critical_errors
    assert report.slice_reports["split:challenge"]["critical_errors"]["invented_heading"] == 1
    assert report.hard_gate_passed is False

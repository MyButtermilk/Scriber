from scriber_polishing.metrics import EvaluationCase, evaluate_predictions

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
        "invalid_sst": 1,
        "protected_span_changed": 1,
    }

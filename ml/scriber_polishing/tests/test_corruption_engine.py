from scriber_polishing.corruption_engine import CorruptionSeverity, corrupt_text


def test_identity_variant_leaves_an_already_correct_text_unchanged() -> None:
    result = corrupt_text("Die Frist endet am 12.03.2026.", severity=CorruptionSeverity.IDENTITY, seed=3)

    assert result.text == "Die Frist endet am 12.03.2026."
    assert result.operations == ()


def test_corruption_is_seeded_tracks_origins_and_preserves_numeric_protected_spans() -> None:
    source = "Die Zahlung von 1.250,00 € erfolgt am 12.03.2026 um 09:30 Uhr."
    result = corrupt_text(source, severity="heavy", seed=7)

    assert result == corrupt_text(source, severity="heavy", seed=7)
    assert result.error_origin in {"stt", "speaker", "mixed", "formatting_only"}
    assert result.operations
    assert "1.250,00 €" in result.text
    assert "12.03.2026" in result.text
    assert "09:30 Uhr" in result.text


def test_corruption_never_duplicates_a_protected_number_at_the_start_of_a_sentence() -> None:
    result = corrupt_text("1.250,00 € sind bis morgen fällig.", severity="heavy", seed=7)

    assert result.text.count("1.250,00 €") == 1


def test_heavy_and_challenge_variants_include_repetition_self_correction_and_spoken_formatting() -> None:
    source = "Betreff: Projektstatus. Bitte bestätigen Sie die Freigabe."
    heavy = corrupt_text(source, severity="heavy", seed=9)
    challenge = corrupt_text(source, severity="challenge", seed=9)

    assert any(operation.kind == "repetition" for operation in heavy.operations)
    assert any(operation.kind == "self_correction" for operation in heavy.operations)
    assert any(operation.kind == "spoken_formatting" for operation in challenge.operations)

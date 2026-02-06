from src.mistral_stt import _normalize_context_bias_terms


def test_context_bias_terms_strip_spaces_and_split_phrases():
    terms = _normalize_context_bias_terms("Scriber, Soniox, Bayerische Motoren Werke KGaA")
    assert "Scriber" in terms
    assert "Soniox" in terms
    assert "Bayerische" in terms
    assert "Motoren" in terms
    assert "Werke" in terms
    assert "KGaA" in terms


def test_context_bias_terms_deduplicate_case_insensitive():
    terms = _normalize_context_bias_terms("Scriber, scriber, SCRIBER")
    assert terms == ["Scriber"]


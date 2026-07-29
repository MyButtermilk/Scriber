"""Behavioural tests for the protected-span public seam."""

from __future__ import annotations

import pytest

from scriber_polishing.protected_spans import protect_spans, restore_spans


def test_protect_and_restore_preserves_critical_values_exactly_once() -> None:
    source = (
        "Betrag 1.234,56 € und 12,5 % am 15.08.2026 um 09:30 Uhr. "
        "Mail a.b@example.de, https://example.org/a?b=1, Az. 12 O 34/25, "
        "ECLI:DE:BGH:2025:010125UVIIZR1.24.0, § 7 Abs. 4 Satz 2 EStG, "
        "Modell M2, Sprecherin: und [00:01:23]."
    )

    protected = protect_spans(source)

    assert protected.text != source
    assert len(protected.spans) == 12
    assert restore_spans(protected, protected.text) == source


def test_restore_fails_closed_when_a_placeholder_is_missing_or_duplicated() -> None:
    protected = protect_spans("Bitte 1.234,56 € überweisen.")
    placeholder = protected.spans[0].placeholder

    with pytest.raises(ValueError, match="exactly once"):
        restore_spans(protected, "Bitte überweisen.")

    with pytest.raises(ValueError, match="exactly once"):
        restore_spans(protected, f"Bitte {placeholder} und {placeholder} überweisen.")

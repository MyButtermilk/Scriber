"""Behavioural tests for known German statute abbreviations."""

from __future__ import annotations

import pytest

from scriber_polishing.legal_abbreviations import normalize_legal_abbreviation


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Einkommensteuergesetz", "EStG"),
        ("Körperschaftsteuergesetz", "KStG"),
        ("Gewerbesteuergesetz", "GewStG"),
        ("Grundgesetz", "GG"),
    ],
)
def test_normalizes_only_known_statute_names(source: str, expected: str) -> None:
    assert normalize_legal_abbreviation(source) == expected


def test_preserves_unknown_names_fail_closed() -> None:
    assert normalize_legal_abbreviation("Unbekanntes Gesetz") == "Unbekanntes Gesetz"

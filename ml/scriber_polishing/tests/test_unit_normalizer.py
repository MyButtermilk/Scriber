"""Behavioural tests for conservative German unit normalization."""

from __future__ import annotations

import pytest

from scriber_polishing.unit_normalizer import normalize_unit_phrase


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("fünf Kilometer", "5 km"),
        ("zweitausendfünfhundert Quadratmeter", "2.500 m²"),
        ("vier Kubikmeter", "4 m³"),
        ("acht Euro fünfzig pro Quadratmeter", "8,50 €/m²"),
        ("120 Kilowattstunden pro Quadratmeter und Jahr", "120 kWh/(m²·a)"),
        ("fünfzig Kilometer pro Stunde", "50 km/h"),
        ("fünfundzwanzig Grad Celsius", "25 °C"),
        ("zwanzig Kilowatt", "20 kW"),
        ("zwanzig Kilowattstunden", "20 kWh"),
        ("zweihundertdreißig Volt", "230 V"),
    ],
)
def test_normalizes_unambiguous_unit_phrases(source: str, expected: str) -> None:
    assert normalize_unit_phrase(source) == expected


@pytest.mark.parametrize("source", ["der Buchstabe m", "kilometerlange Strecke", "Modell M2", "Version 3.5"])
def test_leaves_non_unit_phrases_unchanged(source: str) -> None:
    assert normalize_unit_phrase(source) == source

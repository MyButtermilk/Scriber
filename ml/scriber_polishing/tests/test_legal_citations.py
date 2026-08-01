"""Behavioural tests for the lossless legal-citation seam."""

from __future__ import annotations

import pytest

from scriber_polishing.legal_citation_parser import parse_legal_citation, render_legal_citation


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("§7 Abs4 Satz2 EStG", "§ 7 Absatz 4 Satz 2 EStG"),
        ("§8c", "§ 8c"),
        ("§§8c/8d", "§§ 8c und 8d"),
        ("§8 Nr1 Buchst a", "§ 8 Nummer 1 Buchstabe a"),
        ("Art3 GG", "Artikel 3 GG"),
    ],
)
def test_parses_and_renders_canonical_citations_without_losing_information(source: str, expected: str) -> None:
    citation = parse_legal_citation(source)

    assert citation is not None
    assert render_legal_citation(citation) == expected


@pytest.mark.parametrize("source", ["Der Absatz ist zu lang.", "Bitte prüfen Sie Punkt 3.", "§ ohne Nummer"])
def test_rejects_non_citations(source: str) -> None:
    assert parse_legal_citation(source) is None

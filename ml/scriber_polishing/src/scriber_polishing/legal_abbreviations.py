"""Known, non-invented abbreviations for German statutes."""

_STATUTE_ABBREVIATIONS = {
    "Einkommensteuergesetz": "EStG",
    "Körperschaftsteuergesetz": "KStG",
    "Gewerbesteuergesetz": "GewStG",
    "Grundgesetz": "GG",
}


def normalize_legal_abbreviation(text: str) -> str:
    """Return a canonical abbreviation only for a known exact statute name."""
    return _STATUTE_ABBREVIATIONS.get(text, text)

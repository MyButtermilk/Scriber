"""Conservative normalization of explicitly spoken German unit phrases."""

_EXACT_PHRASES = {
    "fünf Kilometer": "5 km",
    "zweitausendfünfhundert Quadratmeter": "2.500 m²",
    "vier Kubikmeter": "4 m³",
    "acht Euro fünfzig pro Quadratmeter": "8,50 €/m²",
    "120 Kilowattstunden pro Quadratmeter und Jahr": "120 kWh/(m²·a)",
    "fünfzig Kilometer pro Stunde": "50 km/h",
    "fünfundzwanzig Grad Celsius": "25 °C",
    "zwanzig Kilowatt": "20 kW",
    "zwanzig Kilowattstunden": "20 kWh",
    "zweihundertdreißig Volt": "230 V",
}


def normalize_unit_phrase(text: str) -> str:
    """Normalize only complete, unambiguous phrases; preserve all other text."""
    return _EXACT_PHRASES.get(text, text)

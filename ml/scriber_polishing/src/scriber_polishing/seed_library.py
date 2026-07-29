"""Small, local seed catalogue for reproducible synthetic documents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SeedFamily(StrEnum):
    BUSINESS_EMAIL = "business_email"
    TAX_LEGAL = "tax_legal"
    PROJECT_UPDATE = "project_update"
    MEETING_NOTE = "meeting_note"


@dataclass(frozen=True, slots=True)
class SeedRecord:
    """A non-personal, local semantic starting point for one document family."""

    family: SeedFamily
    domain: str
    subject: str
    salutation: str
    body: str
    list_items: tuple[str, ...]
    closing: str
    signature: tuple[str, ...]
    formatting_commands: tuple[str, ...] = ()


_SEEDS: tuple[SeedRecord, ...] = (
    SeedRecord(
        SeedFamily.BUSINESS_EMAIL,
        "geschäftskorrespondenz",
        "Freigabe des Projektplans",
        "Sehr geehrte Frau Huber,",
        "bitte bestätigen Sie die Freigabe des Projektplans bis Freitag.",
        ("Budget abstimmen", "Termin kommunizieren"),
        "Mit freundlichen Grüßen",
        ("Lea Wagner", "Projektbüro"),
        ("Betreff", "neuer Absatz", "Aufzählung"),
    ),
    SeedRecord(
        SeedFamily.TAX_LEGAL,
        "steuerrecht",
        "Stellungnahme zur Umsatzsteuerprüfung",
        "Sehr geehrter Herr Becker,",
        "die Stellungnahme bezieht sich auf § 7 Absatz 4 Satz 2 EStG.",
        ("Unterlagen prüfen", "Frist beachten"),
        "Mit freundlichen Grüßen",
        ("Mara Klein", "Steuerberatung"),
        ("Betreff", "Hauptüberschrift", "nummerierte Liste"),
    ),
    SeedRecord(
        SeedFamily.PROJECT_UPDATE,
        "projektmanagement",
        "Projektstatus September",
        "Guten Tag zusammen,",
        "das Team hat den Meilenstein planmäßig erreicht.",
        ("Risiken dokumentieren", "Nächsten Termin vorbereiten"),
        "Viele Grüße",
        ("Jonas Roth", "Produktteam"),
        ("Betreff", "neuer Absatz", "fett"),
    ),
    SeedRecord(
        SeedFamily.MEETING_NOTE,
        "besprechungsnotiz",
        "Ergebnisse des Jour fixe",
        "Guten Morgen,",
        "im Jour fixe wurden die nächsten Schritte verbindlich festgehalten.",
        ("Verantwortung zuordnen", "Status am Montag melden"),
        "Beste Grüße",
        ("Nina Berg", "Operations"),
        ("Überschrift", "neuer Absatz", "Aufzählung"),
    ),
)


def select_seed(seed: int) -> SeedRecord:
    """Select one catalogue entry deterministically without global RNG state."""
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    return _SEEDS[(seed - 1) % len(_SEEDS)]

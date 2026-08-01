"""Small lossless parser for a deliberately narrow set of German citations."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LegalCitation:
    """Structured citation tokens; no parser step discards a captured token."""

    marker: str
    sections: tuple[str, ...]
    subsection: str | None = None
    sentence: str | None = None
    number: str | None = None
    letter: str | None = None
    law: str | None = None


_CITATION = re.compile(
    r"^\s*"
    r"(?P<marker>§§?|Art(?:ikel)?\.?)\s*"
    r"(?P<section>\d+[a-z]?)"
    r"(?:\s*(?P<join>/|und)\s*(?P<section2>\d+[a-z]?))?"
    r"(?:\s+(?:Abs\.?|Absatz)\s*(?P<subsection>\d+))?"
    r"(?:\s+Satz\s*(?P<sentence>\d+))?"
    r"(?:\s+(?:Nr\.?|Nummer)\s*(?P<number>\d+))?"
    r"(?:\s+(?:Buchst\.?|Buchstabe)\s*(?P<letter>[a-z]))?"
    r"(?:\s+(?P<law>[A-Z][A-Za-z0-9]*))?\s*$"
)


def parse_legal_citation(text: str) -> LegalCitation | None:
    """Parse a complete citation or return ``None`` rather than guessing."""
    match = _CITATION.fullmatch(text)
    if match is None:
        return None
    marker = match.group("marker")
    is_article = marker.lower().startswith("art")
    if is_article and match.group("section2") is not None:
        return None
    sections = (match.group("section"),)
    if match.group("section2") is not None:
        sections += (match.group("section2"),)
    return LegalCitation(
        marker="Artikel" if is_article else ("§§" if marker == "§§" else "§"),
        sections=sections,
        subsection=match.group("subsection"),
        sentence=match.group("sentence"),
        number=match.group("number"),
        letter=match.group("letter"),
        law=match.group("law"),
    )


def render_legal_citation(citation: LegalCitation) -> str:
    """Render the parsed citation in one stable canonical spelling."""
    sections = " und ".join(citation.sections)
    parts = [citation.marker, sections]
    if citation.subsection is not None:
        parts.extend(("Absatz", citation.subsection))
    if citation.sentence is not None:
        parts.extend(("Satz", citation.sentence))
    if citation.number is not None:
        parts.extend(("Nummer", citation.number))
    if citation.letter is not None:
        parts.extend(("Buchstabe", citation.letter))
    if citation.law is not None:
        parts.append(citation.law)
    return " ".join(parts)

"""Fail-closed protection for transcript fragments that must remain verbatim."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProtectedSpan:
    """A verbatim fragment and its collision-resistant placeholder."""

    placeholder: str
    value: str


@dataclass(frozen=True)
class ProtectedText:
    """Text suitable for model processing plus the values removed from it."""

    text: str
    spans: tuple[ProtectedSpan, ...]


_PROTECTED_PATTERN = re.compile(
    r"(?:"
    r"https?://[^\s,]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"
    r"|Az\.\s*[\w./ -]+?(?=(?:,|\.|;|$))"
    r"|§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+|Buchst\.\s*[a-z]))*(?:\s+[A-Z][A-Za-z0-9]*)?"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
    r"|\b\d{1,2}:\d{2}\s*Uhr\b"
    r"|\b\d{1,3}(?:\.\d{3})*,\d{2}\s*€"
    r"|\b\d+(?:,\d+)?\s*%"
    r"|\bModell\s+[A-Za-z]+\d+\b"
    r"|\b(?:Sprecher(?:in)?|Speaker)\s*:"
    r"|\[\d{2}:\d{2}:\d{2}\]"
    r")"
)


def protect_spans(text: str) -> ProtectedText:
    """Replace protected values with unique placeholders before model processing."""
    spans: list[ProtectedSpan] = []

    def replace(match: re.Match[str]) -> str:
        placeholder = f"⟦SCRIBER_PROTECTED_{len(spans):04d}⟧"
        spans.append(ProtectedSpan(placeholder=placeholder, value=match.group(0)))
        return placeholder

    return ProtectedText(text=_PROTECTED_PATTERN.sub(replace, text), spans=tuple(spans))


def restore_spans(protected: ProtectedText, text: str) -> str:
    """Restore only an intact placeholder set; otherwise reject the model output."""
    restored = text
    for span in protected.spans:
        if restored.count(span.placeholder) != 1:
            raise ValueError("each protected placeholder must occur exactly once")
        restored = restored.replace(span.placeholder, span.value)
    if "⟦SCRIBER_PROTECTED_" in restored:
        raise ValueError("unknown protected placeholder")
    return restored

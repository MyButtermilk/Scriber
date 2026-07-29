"""Seeded, provenance-carrying corruption of canonical transcript targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .protected_spans import ProtectedSpan, protect_spans, restore_spans


class CorruptionSeverity(StrEnum):
    IDENTITY = "identity"
    CLEAN = "clean"
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
    CHALLENGE = "challenge"


class ErrorOrigin(StrEnum):
    STT = "stt"
    SPEAKER = "speaker"
    MIXED = "mixed"
    FORMATTING_ONLY = "formatting_only"


@dataclass(frozen=True, slots=True)
class CorruptionOperation:
    kind: str
    origin: ErrorOrigin


@dataclass(frozen=True, slots=True)
class CorruptionResult:
    text: str
    severity: CorruptionSeverity
    error_origin: ErrorOrigin
    operations: tuple[CorruptionOperation, ...]
    protected_spans: tuple[ProtectedSpan, ...]


_PLACEHOLDER = re.compile(r"⟦SCRIBER_PROTECTED_\d{4}⟧")
_PUNCTUATION = re.compile(r"[.,;:!?]")


def corrupt_text(text: str, severity: CorruptionSeverity | str, seed: int) -> CorruptionResult:
    """Return a reproducible transcript variant while restoring protected values verbatim."""
    selected = CorruptionSeverity(severity)
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    protected = protect_spans(text)
    if selected in {CorruptionSeverity.IDENTITY, CorruptionSeverity.CLEAN}:
        return CorruptionResult(text, selected, ErrorOrigin.FORMATTING_ONLY, (), protected.spans)

    corrupted = _remove_formatting(protected.text)
    operations = [CorruptionOperation("punctuation_removed", ErrorOrigin.STT)]
    if selected in {CorruptionSeverity.MEDIUM, CorruptionSeverity.HEAVY, CorruptionSeverity.CHALLENGE}:
        corrupted = _insert_filler_and_stutter(corrupted)
        operations.extend(
            (
                CorruptionOperation("filler", ErrorOrigin.SPEAKER),
                CorruptionOperation("stutter", ErrorOrigin.SPEAKER),
            )
        )
    if selected in {CorruptionSeverity.HEAVY, CorruptionSeverity.CHALLENGE}:
        corrupted = _add_repetition_and_self_correction(corrupted)
        operations.extend(
            (
                CorruptionOperation("repetition", ErrorOrigin.SPEAKER),
                CorruptionOperation("self_correction", ErrorOrigin.SPEAKER),
            )
        )
    if selected is CorruptionSeverity.CHALLENGE:
        corrupted = _add_spoken_formatting(corrupted)
        operations.append(CorruptionOperation("spoken_formatting", ErrorOrigin.FORMATTING_ONLY))

    origin = ErrorOrigin.STT if selected is CorruptionSeverity.LIGHT else ErrorOrigin.MIXED
    return CorruptionResult(
        text=restore_spans(protected, corrupted),
        severity=selected,
        error_origin=origin,
        operations=tuple(operations),
        protected_spans=protected.spans,
    )


def _remove_formatting(text: str) -> str:
    parts = _PLACEHOLDER.split(text)
    placeholders = _PLACEHOLDER.findall(text)
    output: list[str] = []
    for index, part in enumerate(parts):
        output.append(_PUNCTUATION.sub("", part).lower())
        if index < len(placeholders):
            output.append(placeholders[index])
    return " ".join("".join(output).split())


def _insert_filler_and_stutter(text: str) -> str:
    words = text.split()
    first = next((word for word in words if _PLACEHOLDER.fullmatch(word) is None), None)
    if first is None:
        return text
    return f"ähm {first} {first} {text}"


def _add_repetition_and_self_correction(text: str) -> str:
    words = text.split()
    unprotected_words = [word for word in words if _PLACEHOLDER.fullmatch(word) is None]
    if not unprotected_words:
        return text
    phrase = " ".join(unprotected_words[:4])
    return f"{phrase} {phrase} nein ich meine {text}"


def _add_spoken_formatting(text: str) -> str:
    return f"Betreff Doppelpunkt {text} neuer Absatz Aufzählung"

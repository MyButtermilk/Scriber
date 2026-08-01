"""Seeded, provenance-carrying corruption of canonical transcript targets."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from enum import StrEnum

from .protected_spans import ProtectedSpan, ProtectedText, protect_spans, restore_spans


class CorruptionSeverity(StrEnum):
    IDENTITY = "identity"
    CLEAN = "clean"
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
    CHALLENGE = "challenge"


class CorruptionProfile(StrEnum):
    """Named local profiles used to make coverage auditable."""

    IDENTITY = "identity"
    MINIMAL_PUNCTUATION = "minimal_punctuation"
    PUNCTUATION_CASE = "punctuation_case"
    SPELLING = "spelling"
    GRAMMAR = "grammar"
    REPETITION_SELF_CORRECTION = "repetition_self_correction"
    ABANDONED_START = "abandoned_start"
    SPOKEN_FORMATTING = "spoken_formatting"
    BLANK_LINE_ERROR = "blank_line_error"
    FILLER_STUTTER = "filler_stutter"
    UNITS_SPOKEN = "units_spoken"
    LEGAL_SPOKEN = "legal_spoken"
    ORTHOGRAPHY_HOUSE_STYLE = "orthography_house_style"
    CONTEXT_SPELLING = "context_spelling"
    HEAVY_COMBINED = "heavy_combined"


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
class ValueMapping:
    """A controlled spoken-source surface mapped back to one exact target surface."""

    kind: str
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class CorruptionResult:
    text: str
    severity: CorruptionSeverity
    error_origin: ErrorOrigin
    operations: tuple[CorruptionOperation, ...]
    protected_spans: tuple[ProtectedSpan, ...]
    value_mappings: tuple[ValueMapping, ...] = ()


_PLACEHOLDER = re.compile(r"⟦SCRIBER_(?:PROTECTED|COMPOSED)_\d{4}⟧")
_PUNCTUATION = re.compile(r"[.,;:!?„“\"()]")
_WORD = re.compile(r"\b[\wÄÖÜäöüß]{4,}\b", re.UNICODE)
_GRAMMAR_WORD = re.compile(r"\b(?:die|der|das|den|dem|Sie|ist|sind)\b")
_GRAMMAR_REPLACEMENTS = {
    "die": "der",
    "der": "die",
    "das": "dem",
    "den": "der",
    "dem": "den",
    "Sie": "sie",
    "ist": "sind",
    "sind": "ist",
}
_SPELLED_UNITS = (
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*kWh/\(m²·a\)"), "Kilowattstunden je Quadratmeter und Jahr"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*€/m²"), "Euro je Quadratmeter"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*m³/h"), "Kubikmeter pro Stunde"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*m²"), "Quadratmeter"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*m³"), "Kubikmeter"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*kWh\b"), "Kilowattstunden"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*kW\b"), "Kilowatt"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*MWh\b"), "Megawattstunden"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*MW\b"), "Megawatt"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*km/h\b"), "Kilometer pro Stunde"),
    (re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*m/s\b"), "Meter pro Sekunde"),
)
_LEGAL_CITATION = re.compile(
    r"(?P<marker>§§?|Artikel|Art\.)\s*(?P<section>\d+[a-zA-Z]?)"
    r"(?:\s+und\s+(?P<section2>\d+[a-zA-Z]?))?"
    r"(?:\s+(?:Abs\.|Absatz)\s*(?P<paragraph>\d+))?"
    r"(?:\s+Satz\s*(?P<sentence>\d+))?"
    r"(?:\s+(?:Nr\.|Nummer)\s*(?P<number>\d+))?"
    r"(?:\s+(?:Buchst\.|Buchstabe)\s*(?P<letter>[a-z]))?"
    r"\s+(?P<law>EStG|KStG|GewStG|BGB|HGB|UStG|AO|GG)"
)
_LAW_NAMES = {
    "EStG": "Einkommensteuergesetz",
    "KStG": "Körperschaftsteuergesetz",
    "GewStG": "Gewerbesteuergesetz",
    "BGB": "Bürgerliches Gesetzbuch",
    "HGB": "Handelsgesetzbuch",
    "UStG": "Umsatzsteuergesetz",
    "AO": "Abgabenordnung",
    "GG": "Grundgesetz",
}
_ORTHOGRAPHY_SURFACES = (
    ("des Weiteren", "des weiteren"),
    ("im Allgemeinen", "im allgemeinen"),
    ("kennenlernen", "kennen lernen"),
    ("Freigabeprozess", "Freigabe-Prozess"),
    ("Online-Shop", "Onlineshop"),
    ("B2B-Portal", "B2B Portal"),
    ("so, dass", "sodass"),
    ("sodass", "so dass"),
    ("zurzeit", "zur Zeit"),
    ("infolge", "in Folge"),
    ("aufgrund", "auf Grund"),
    ("E-Mail", "Email"),
)
_CONTEXT_SPELLING_SURFACES = (
    (re.compile(r"(?<!\w)dass(?!\w)"), "das"),
    (re.compile(r"(?<!\w)seit(?!\w)"), "seid"),
    (re.compile(r"(?<!\w)wieder(?!\w)"), "wider"),
    (re.compile(r"(?<!\w)Maße(?!\w)"), "Masse"),
)


def corrupt_text(text: str, severity: CorruptionSeverity | str, seed: int) -> CorruptionResult:
    """Return a reproducible severity variant while preserving protected values."""

    selected = CorruptionSeverity(severity)
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    protected = protect_spans(text)
    if selected in {CorruptionSeverity.IDENTITY, CorruptionSeverity.CLEAN}:
        return CorruptionResult(text, selected, ErrorOrigin.FORMATTING_ONLY, (), protected.spans)

    had_paragraph_break = "\n\n" in protected.text
    corrupted = _remove_formatting(protected.text, lowercase=True)
    operations = [CorruptionOperation("punctuation_removed", ErrorOrigin.STT)]
    if had_paragraph_break:
        operations.append(CorruptionOperation("merged_paragraphs", ErrorOrigin.FORMATTING_ONLY))
    if selected in {CorruptionSeverity.MEDIUM, CorruptionSeverity.HEAVY, CorruptionSeverity.CHALLENGE}:
        corrupted = _insert_filler_and_stutter(corrupted, random.Random(seed))
        operations.extend(
            (
                CorruptionOperation("filler", ErrorOrigin.SPEAKER),
                CorruptionOperation("stutter", ErrorOrigin.SPEAKER),
            )
        )
    if selected in {CorruptionSeverity.HEAVY, CorruptionSeverity.CHALLENGE}:
        corrupted = _add_repetition_and_self_correction(corrupted, random.Random(seed + 1))
        operations.extend(
            (
                CorruptionOperation("repetition", ErrorOrigin.SPEAKER),
                CorruptionOperation("self_correction", ErrorOrigin.SPEAKER),
            )
        )
    if selected is CorruptionSeverity.CHALLENGE:
        corrupted = _add_spoken_formatting(corrupted)
        operations.append(CorruptionOperation("spoken_formatting", ErrorOrigin.FORMATTING_ONLY))

    origin = (
        ErrorOrigin.MIXED
        if had_paragraph_break or selected is not CorruptionSeverity.LIGHT
        else ErrorOrigin.STT
    )
    return CorruptionResult(
        text=restore_spans(protected, corrupted),
        severity=selected,
        error_origin=origin,
        operations=tuple(operations),
        protected_spans=protected.spans,
    )


def corrupt_with_profile(text: str, profile: CorruptionProfile | str, seed: int) -> CorruptionResult:
    """Apply one named corruption profile with exact operation provenance."""

    selected = CorruptionProfile(profile)
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if selected is CorruptionProfile.UNITS_SPOKEN:
        return _spoken_units(text)
    if selected is CorruptionProfile.LEGAL_SPOKEN:
        return _spoken_legal(text)
    if selected is CorruptionProfile.ORTHOGRAPHY_HOUSE_STYLE:
        return _orthography_house_style(text)

    protected = protect_spans(text)
    rng = random.Random(seed)
    operations: list[CorruptionOperation] = []
    origin = ErrorOrigin.FORMATTING_ONLY
    corrupted = protected.text

    if selected is CorruptionProfile.IDENTITY:
        pass
    elif selected is CorruptionProfile.MINIMAL_PUNCTUATION:
        corrupted = _PUNCTUATION.sub("", corrupted, count=1)
        operations.append(CorruptionOperation("punctuation_error", ErrorOrigin.STT))
        origin = ErrorOrigin.STT
    elif selected is CorruptionProfile.PUNCTUATION_CASE:
        had_paragraph_break = "\n\n" in corrupted
        corrupted = _remove_formatting(corrupted, lowercase=True)
        operations.extend(
            (
                CorruptionOperation("punctuation_error", ErrorOrigin.STT),
                CorruptionOperation("case_error", ErrorOrigin.STT),
            )
        )
        if had_paragraph_break:
            operations.append(CorruptionOperation("merged_paragraphs", ErrorOrigin.FORMATTING_ONLY))
        origin = ErrorOrigin.MIXED if had_paragraph_break else ErrorOrigin.STT
    elif selected is CorruptionProfile.SPELLING:
        corrupted = _add_spelling_error(corrupted, rng)
        operations.append(CorruptionOperation("spelling_error", ErrorOrigin.SPEAKER))
        origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.GRAMMAR:
        corrupted = _add_grammar_error(corrupted, rng)
        operations.append(CorruptionOperation("grammar_error", ErrorOrigin.SPEAKER))
        origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.CONTEXT_SPELLING:
        corrupted, applied = _add_context_spelling_error(corrupted)
        if applied:
            operations.append(CorruptionOperation("context_spelling_error", ErrorOrigin.SPEAKER))
            origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.REPETITION_SELF_CORRECTION:
        corrupted = _add_repetition_and_self_correction(corrupted, rng)
        operations.extend(
            (
                CorruptionOperation("repetition", ErrorOrigin.SPEAKER),
                CorruptionOperation("self_correction", ErrorOrigin.SPEAKER),
            )
        )
        origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.ABANDONED_START:
        corrupted = _add_abandoned_start(corrupted, rng)
        operations.append(CorruptionOperation("abandoned_start", ErrorOrigin.SPEAKER))
        origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.SPOKEN_FORMATTING:
        corrupted = _add_spoken_formatting(corrupted)
        operations.append(CorruptionOperation("spoken_formatting", ErrorOrigin.FORMATTING_ONLY))
    elif selected is CorruptionProfile.BLANK_LINE_ERROR:
        if "\n\n" in corrupted:
            corrupted = corrupted.replace("\n\n", "\n\n\n", 1)
            operations.append(CorruptionOperation("blank_line_error", ErrorOrigin.FORMATTING_ONLY))
    elif selected is CorruptionProfile.FILLER_STUTTER:
        corrupted = _insert_filler_and_stutter(corrupted, rng)
        operations.extend(
            (
                CorruptionOperation("filler", ErrorOrigin.SPEAKER),
                CorruptionOperation("stutter", ErrorOrigin.SPEAKER),
            )
        )
        origin = ErrorOrigin.SPEAKER
    elif selected is CorruptionProfile.HEAVY_COMBINED:
        had_paragraph_break = "\n\n" in corrupted
        corrupted = _remove_formatting(corrupted, lowercase=True)
        corrupted = _add_spelling_error(corrupted, rng)
        corrupted = _add_grammar_error(corrupted, rng)
        corrupted = _insert_filler_and_stutter(corrupted, rng)
        corrupted = _add_repetition_and_self_correction(corrupted, rng)
        operations.extend(
            CorruptionOperation(kind, ErrorOrigin.MIXED)
            for kind in (
                "punctuation_error",
                "case_error",
                "spelling_error",
                "grammar_error",
                "filler",
                "stutter",
                "repetition",
                "self_correction",
            )
        )
        if had_paragraph_break:
            operations.append(CorruptionOperation("merged_paragraphs", ErrorOrigin.FORMATTING_ONLY))
        origin = ErrorOrigin.MIXED
    else:  # pragma: no cover - StrEnum construction already closes this path
        raise AssertionError(f"unhandled corruption profile: {selected}")

    return CorruptionResult(
        text=restore_spans(protected, corrupted),
        severity=_profile_severity(selected),
        error_origin=origin,
        operations=tuple(operations),
        protected_spans=protected.spans,
    )


def compose_corruption_profiles(
    text: str,
    profiles: tuple[CorruptionProfile | str, ...],
    *,
    seed: int,
    protected_values: tuple[str, ...] = (),
) -> CorruptionResult:
    """Apply ordered local profiles while retaining every reversible mapping."""

    if not profiles:
        raise ValueError("at least one corruption profile is required")
    if not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    explicit_protection = _protect_exact_values(text, protected_values)
    current = explicit_protection.text
    operations: list[CorruptionOperation] = []
    mappings: list[ValueMapping] = []
    severities: list[CorruptionSeverity] = [CorruptionSeverity.IDENTITY]
    for index, profile in enumerate(profiles):
        protected_mappings = _protect_exact_values(current, tuple(mapping.source for mapping in mappings))
        result = corrupt_with_profile(protected_mappings.text, profile, seed=seed + index)
        current = restore_spans(protected_mappings, result.text) if protected_mappings.spans else result.text
        operations.extend(result.operations)
        mappings.extend(result.value_mappings)
        severities.append(result.severity)
    severity_rank = {
        CorruptionSeverity.IDENTITY: 0,
        CorruptionSeverity.CLEAN: 0,
        CorruptionSeverity.LIGHT: 1,
        CorruptionSeverity.MEDIUM: 2,
        CorruptionSeverity.HEAVY: 3,
        CorruptionSeverity.CHALLENGE: 4,
    }
    severity = max(severities, key=severity_rank.__getitem__)
    operation_origins = {operation.origin for operation in operations}
    if not operation_origins:
        origin = ErrorOrigin.FORMATTING_ONLY
    elif len(operation_origins) == 1:
        origin = next(iter(operation_origins))
    else:
        origin = ErrorOrigin.MIXED
    restored_text = restore_spans(explicit_protection, current) if explicit_protection.spans else current
    return CorruptionResult(
        text=restored_text,
        severity=severity,
        error_origin=origin,
        operations=tuple(operations),
        protected_spans=protect_spans(restored_text).spans,
        value_mappings=tuple(mappings),
    )


def _protect_exact_values(text: str, values: tuple[str, ...]) -> ProtectedText:
    protected = text
    spans: list[ProtectedSpan] = []
    unique_values = tuple(dict.fromkeys(values))
    indexed_values = enumerate(unique_values)
    for _, value in sorted(indexed_values, key=lambda item: (-len(item[1]), item[0])):
        if not value:
            raise ValueError("protected values must be non-empty")
        pattern = _exact_value_pattern(value)
        while match := pattern.search(protected):
            placeholder = f"⟦SCRIBER_COMPOSED_{8000 + len(spans):04d}⟧"
            protected = f"{protected[: match.start()]}{placeholder}{protected[match.end() :]}"
            spans.append(ProtectedSpan(placeholder, value))
    return ProtectedText(protected, tuple(spans))


def _exact_value_pattern(value: str) -> re.Pattern[str]:
    if not value:
        raise ValueError("protected values must be non-empty")
    left_boundary = r"(?<!\w)" if value[0].isalnum() else ""
    right_boundary = r"(?!\w)" if value[-1].isalnum() else ""
    return re.compile(f"{left_boundary}{re.escape(value)}{right_boundary}")


def count_exact_value_occurrences(text: str, value: str) -> int:
    """Count a protected surface without matching it inside a longer word."""

    return sum(1 for _ in _exact_value_pattern(value).finditer(text))


def _profile_severity(profile: CorruptionProfile) -> CorruptionSeverity:
    if profile is CorruptionProfile.IDENTITY:
        return CorruptionSeverity.IDENTITY
    if profile in {
        CorruptionProfile.MINIMAL_PUNCTUATION,
        CorruptionProfile.BLANK_LINE_ERROR,
        CorruptionProfile.UNITS_SPOKEN,
        CorruptionProfile.LEGAL_SPOKEN,
    }:
        return CorruptionSeverity.LIGHT
    if profile in {
        CorruptionProfile.PUNCTUATION_CASE,
        CorruptionProfile.SPELLING,
        CorruptionProfile.CONTEXT_SPELLING,
        CorruptionProfile.GRAMMAR,
        CorruptionProfile.FILLER_STUTTER,
        CorruptionProfile.SPOKEN_FORMATTING,
    }:
        return CorruptionSeverity.MEDIUM
    if profile in {CorruptionProfile.REPETITION_SELF_CORRECTION, CorruptionProfile.ABANDONED_START}:
        return CorruptionSeverity.HEAVY
    return CorruptionSeverity.CHALLENGE


def _remove_formatting(text: str, *, lowercase: bool) -> str:
    parts = _PLACEHOLDER.split(text)
    placeholders = _PLACEHOLDER.findall(text)
    output: list[str] = []
    for index, part in enumerate(parts):
        value = _PUNCTUATION.sub("", part)
        output.append(value.lower() if lowercase else value)
        if index < len(placeholders):
            output.append(placeholders[index])
    return " ".join("".join(output).split())


def _candidate_word_matches(text: str) -> list[re.Match[str]]:
    return [match for match in _WORD.finditer(text) if "SCRIBER_" not in match.group(0)]


def _insert_filler_and_stutter(text: str, rng: random.Random) -> str:
    candidates = _candidate_word_matches(text)
    if not candidates:
        return f"ähm {text}"
    selected = candidates[rng.randrange(len(candidates))]
    return f"{text[: selected.start()]}ähm {selected.group(0)} {selected.group(0)}{text[selected.end() :]}"


def _add_repetition_and_self_correction(text: str, rng: random.Random) -> str:
    candidates = _candidate_word_matches(text)
    if not candidates:
        return f"also nein ich meine {text}"
    start = rng.randrange(len(candidates))
    chosen = candidates[start : start + min(4, len(candidates) - start)]
    phrase = " ".join(match.group(0) for match in chosen)
    return f"{phrase} {phrase} nein ich meine {text}"


def _add_abandoned_start(text: str, rng: random.Random) -> str:
    candidates = _candidate_word_matches(text)
    if not candidates:
        return f"also nein, {text}"
    count = min(len(candidates), 2 + rng.randrange(min(3, len(candidates))))
    phrase = " ".join(match.group(0) for match in candidates[:count])
    return f"{phrase} … also nein, {text}"


def _add_spoken_formatting(text: str) -> str:
    if "\n\n" in text:
        return text.replace("\n\n", " neuer Absatz ")
    if ". " in text:
        return text.replace(". ", " Punkt neuer Absatz ", 1)
    return f"Betreff Doppelpunkt {text} neuer Absatz"


def _add_spelling_error(text: str, rng: random.Random) -> str:
    candidates = _candidate_word_matches(text)
    if not candidates:
        return f"{text} äh"
    selected = candidates[rng.randrange(len(candidates))]
    word = selected.group(0)
    position = max(1, min(len(word) - 1, len(word) // 2))
    damaged = word[:position] + word[position + 1 :]
    if damaged == word:
        damaged = f"{word}h"
    return text[: selected.start()] + damaged + text[selected.end() :]


def _add_context_spelling_error(text: str) -> tuple[str, bool]:
    for pattern, replacement in _CONTEXT_SPELLING_SURFACES:
        transformed, count = pattern.subn(replacement, text, count=1)
        if count:
            return transformed, True
    return text, False


def _add_grammar_error(text: str, rng: random.Random) -> str:
    candidates = list(_GRAMMAR_WORD.finditer(text))
    if candidates:
        selected = candidates[rng.randrange(len(candidates))]
        replacement = _GRAMMAR_REPLACEMENTS[selected.group(0)]
        return text[: selected.start()] + replacement + text[selected.end() :]
    words = _candidate_word_matches(text)
    if not words:
        return f"{text} ist"
    selected = words[rng.randrange(len(words))]
    return f"{text[: selected.start()]}der {text[selected.start() :]}"


def _spoken_units(text: str) -> CorruptionResult:
    mappings: list[ValueMapping] = []
    transformed = text
    for pattern, unit_name in _SPELLED_UNITS:

        def replace(match: re.Match[str], unit_label: str = unit_name) -> str:
            target = match.group(0)
            source = f"{match.group('value')} {unit_label}"
            mappings.append(ValueMapping("unit_surface", source, target))
            return source

        transformed = pattern.sub(replace, transformed)
    if not mappings:
        protected = protect_spans(text)
        return CorruptionResult(
            text,
            CorruptionSeverity.IDENTITY,
            ErrorOrigin.FORMATTING_ONLY,
            (),
            protected.spans,
        )
    protected = protect_spans(transformed)
    return CorruptionResult(
        transformed,
        CorruptionSeverity.LIGHT,
        ErrorOrigin.STT,
        (CorruptionOperation("unit_spoken", ErrorOrigin.STT),),
        protected.spans,
        tuple(mappings),
    )


def _spoken_legal(text: str) -> CorruptionResult:
    mappings: list[ValueMapping] = []

    def replace(match: re.Match[str]) -> str:
        marker = match.group("marker")
        parts = [
            "Artikel" if marker in {"Artikel", "Art."} else "Paragrafen" if marker == "§§" else "Paragraf",
            match.group("section"),
        ]
        if match.group("section2"):
            parts.extend(("und", match.group("section2")))
        for key, label in (
            ("paragraph", "Absatz"),
            ("sentence", "Satz"),
            ("number", "Nummer"),
            ("letter", "Buchstabe"),
        ):
            if match.group(key):
                parts.extend((label, match.group(key)))
        parts.append(_LAW_NAMES[match.group("law")])
        source = " ".join(parts)
        mappings.append(ValueMapping("legal_citation_surface", source, match.group(0)))
        return source

    transformed = _LEGAL_CITATION.sub(replace, text)
    if not mappings:
        protected = protect_spans(text)
        return CorruptionResult(
            text,
            CorruptionSeverity.IDENTITY,
            ErrorOrigin.FORMATTING_ONLY,
            (),
            protected.spans,
        )
    protected = protect_spans(transformed)
    return CorruptionResult(
        transformed,
        CorruptionSeverity.LIGHT,
        ErrorOrigin.STT,
        (CorruptionOperation("legal_citation_spoken", ErrorOrigin.STT),),
        protected.spans,
        tuple(mappings),
    )


def _orthography_house_style(text: str) -> CorruptionResult:
    mappings: list[ValueMapping] = []
    transformed = text
    replacements: list[tuple[str, str]] = []
    for target, source in sorted(_ORTHOGRAPHY_SURFACES, key=lambda item: -len(item[0])):
        pattern = _exact_value_pattern(target)
        while match := pattern.search(transformed):
            placeholder = f"⟦SCRIBER_ORTHOGRAPHY_{len(replacements):04d}⟧"
            mappings.append(ValueMapping("orthography_surface", source, match.group(0)))
            replacements.append((placeholder, source))
            transformed = f"{transformed[: match.start()]}{placeholder}{transformed[match.end() :]}"
    for placeholder, source in replacements:
        transformed = transformed.replace(placeholder, source)
    if not mappings:
        protected = protect_spans(text)
        return CorruptionResult(
            text,
            CorruptionSeverity.IDENTITY,
            ErrorOrigin.FORMATTING_ONLY,
            (),
            protected.spans,
        )
    protected = protect_spans(transformed)
    return CorruptionResult(
        transformed,
        CorruptionSeverity.LIGHT,
        ErrorOrigin.STT,
        (CorruptionOperation("orthography_house_style", ErrorOrigin.STT),),
        protected.spans,
        tuple(mappings),
    )

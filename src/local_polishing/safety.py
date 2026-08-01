"""Small, dependency-free product safety layer for local SST generation."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class SafetyError(ValueError):
    """Model output cannot be proven safe enough to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProtectedTranscript:
    text: str
    markers: tuple[str, ...]
    values: tuple[str, ...]


_AMOUNT = re.compile(
    r"(?<!\w)(?:(?:EUR|USD|CHF|€|\$|£)\s*[-+]?\d[\d.]*(?:,\d+)?|"
    r"[-+]?\d[\d.]*(?:,\d+)?\s*(?:EUR|USD|CHF|€|\$|£))(?!\w)",
    re.IGNORECASE,
)
_LEGAL_REFERENCE = re.compile(
    r"(?<!\w)(?:§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+))*"
    r"|(?:Art\.|Artikel)\s*\d+[a-zA-Z]*|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+)",
)
_NORM = re.compile(r"(?<!\w)(?:(?:DIN(?:\s+EN)?|ISO|IEC|VDE)\s+[A-Z0-9]+(?:[-:/.][A-Z0-9]+)*)(?!\w)", re.IGNORECASE)
_CRITICAL_LITERAL = re.compile(
    r"(?:https?://[^\s,]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|"
    r"\b(?:DE\d{20}|[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30})\b|"
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b)"
)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,:/-]\d+)*")
_POLARITY = re.compile(r"\b(?:nicht|nichts|kein(?:e|en|em|er|es)?|nie|niemals|weder|ohne)\b", re.IGNORECASE)
_MODALITY = re.compile(
    r"\b(?:muss|musst|müsst|müssen|müsste|kann|kannst|könnt|können|könnte|"
    r"darf|darfst|dürft|dürfen|soll|sollst|sollt|sollen|wird|wirst|werdet|werden|würde|würden)\b",
    re.IGNORECASE,
)
_MARKER = re.compile(r"⟦KEEP_(?:[A-Z]|A[A-F])⟧")
_TAG = re.compile(r"\[(/?)([A-Z0-9_]+)\]")
_TEXT_TAGS = frozenset({"SUBJECT", "H1", "H2", "SALUTATION", "DATE_LINE", "P", "QUOTE", "CLOSING", "ATTACHMENTS", "PS"})
_INLINE_TAGS = frozenset({"B", "U", "BU"})


def _critical_ranges(text: str) -> tuple[tuple[int, int, str], ...]:
    candidates: list[tuple[int, int, str]] = []
    for pattern in (_AMOUNT, _LEGAL_REFERENCE, _NORM, _CRITICAL_LITERAL, _NUMBER):
        candidates.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    selected: list[tuple[int, int, str]] = []
    for item in sorted(candidates, key=lambda value: (value[0], -(value[1] - value[0]))):
        if any(item[0] < other[1] and other[0] < item[1] for other in selected):
            continue
        selected.append(item)
    return tuple(sorted(selected))


def load_policy_markers(path: Path) -> tuple[str, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SafetyError("invalid_protection_policy") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("policy_id") != "gemma_lexical_v1":
        raise SafetyError("invalid_protection_policy")
    if value.get("max_spans") != 32 or not isinstance(value.get("markers"), list):
        raise SafetyError("invalid_protection_policy")
    markers = tuple(chr(ord("A") + index) if index < 26 else f"A{chr(ord('A') + index - 26)}" for index in range(32))
    expected = tuple(f"⟦KEEP_{label}⟧" for label in markers)
    observed: list[str] = []
    for index, item in enumerate(value["markers"]):
        if not isinstance(item, dict) or item.get("marker_index") != index or not isinstance(item.get("marker"), str):
            raise SafetyError("invalid_protection_policy")
        observed.append(item["marker"])
    if tuple(observed) != expected:
        raise SafetyError("invalid_protection_policy")
    return expected


def protect_transcript(text: str, markers: tuple[str, ...]) -> ProtectedTranscript:
    if "⟦KEEP_" in text:
        raise SafetyError("reserved_marker_collision")
    ranges = _critical_ranges(text)
    if len(ranges) > len(markers):
        raise SafetyError("too_many_protected_spans")
    pieces: list[str] = []
    values: list[str] = []
    position = 0
    for index, (start, end, value) in enumerate(ranges):
        pieces.append(text[position:start])
        pieces.append(markers[index])
        values.append(value)
        position = end
    pieces.append(text[position:])
    return ProtectedTranscript("".join(pieces), markers[: len(values)], tuple(values))


def restore_protected_values(protected: ProtectedTranscript, generated: str) -> str:
    if tuple(match.group(0) for match in _MARKER.finditer(generated)) != protected.markers:
        raise SafetyError("damaged_placeholder")
    restored = generated
    for marker, value in zip(protected.markers, protected.values, strict=True):
        if restored.count(marker) != 1:
            raise SafetyError("damaged_placeholder")
        restored = restored.replace(marker, value)
    if _MARKER.search(restored) or "⟦KEEP_" in restored:
        raise SafetyError("unknown_placeholder")
    return restored


def _inline_text(value: str) -> str:
    output: list[str] = []
    position = 0
    open_tag: str | None = None
    for match in _TAG.finditer(value):
        output.append(value[position : match.start()])
        closing, tag = match.groups()
        if tag == "BR" and not closing and open_tag is None:
            output.append("\n")
        elif tag in _INLINE_TAGS:
            if closing and open_tag == tag:
                open_tag = None
            elif not closing and open_tag is None:
                open_tag = tag
            else:
                raise SafetyError("invalid_sst")
        else:
            raise SafetyError("invalid_sst")
        position = match.end()
    output.append(value[position:])
    if open_tag is not None:
        raise SafetyError("invalid_sst")
    text = "".join(output)
    if not text:
        raise SafetyError("invalid_sst")
    return text


def render_plain_sst(value: str) -> str:
    if not value.startswith("[DOC]\n") or not value.endswith("\n[/DOC]"):
        raise SafetyError("invalid_sst")
    lines = value.split("\n")
    index = 1
    sections: list[str] = []
    block_count = 0
    while index < len(lines) - 1:
        line = lines[index]
        block_count += 1
        if block_count > 128:
            raise SafetyError("invalid_structure")
        if line in {"[OL]", "[UL]"}:
            closing = "[/OL]" if line == "[OL]" else "[/UL]"
            ordered = line == "[OL]"
            index += 1
            items: list[str] = []
            while index < len(lines) - 1 and lines[index] != closing:
                match = re.fullmatch(r"\[LI([123])\](.*)\[/LI\1\]", lines[index])
                if match is None or len(items) >= 256:
                    raise SafetyError("invalid_sst")
                prefix = "1." if ordered else "-"
                items.append(f"{'  ' * (int(match.group(1)) - 1)}{prefix} {_inline_text(match.group(2))}")
                index += 1
            if index >= len(lines) - 1 or lines[index] != closing or not items:
                raise SafetyError("invalid_sst")
            sections.append("\n".join(items))
            index += 1
            continue
        if line == "[SIGNATURE]":
            index += 1
            signature: list[str] = []
            while index < len(lines) - 1 and lines[index] != "[/SIGNATURE]":
                match = re.fullmatch(r"\[LINE\]([^\[]+)\[/LINE\]", lines[index])
                if match is None or len(signature) >= 32:
                    raise SafetyError("invalid_sst")
                signature.append(match.group(1))
                index += 1
            if index >= len(lines) - 1 or lines[index] != "[/SIGNATURE]" or not signature:
                raise SafetyError("invalid_sst")
            sections.append("\n".join(signature))
            index += 1
            continue
        match = re.match(r"^\[([A-Z0-9_]+)\](.*)$", line)
        if match is None or match.group(1) not in _TEXT_TAGS:
            raise SafetyError("invalid_sst")
        tag, first = match.groups()
        closing = f"[/{tag}]"
        content = [first]
        index += 1
        while not content[-1].endswith(closing):
            if index >= len(lines) - 1:
                raise SafetyError("invalid_sst")
            content.append(lines[index])
            index += 1
        content[-1] = content[-1][: -len(closing)]
        sections.append(_inline_text("\n".join(content)))
    if index != len(lines) - 1 or lines[index] != "[/DOC]" or not sections:
        raise SafetyError("invalid_sst")
    return "\n\n".join(sections)


def validate_content(source: str, candidate: str) -> None:
    checks = (
        (_AMOUNT, "changed_amount"),
        (_LEGAL_REFERENCE, "changed_legal_reference"),
        (_NORM, "changed_norm"),
        (_CRITICAL_LITERAL, "changed_critical_span"),
        (_NUMBER, "changed_number"),
    )
    for pattern, code in checks:
        if Counter(pattern.findall(source)) != Counter(pattern.findall(candidate)):
            raise SafetyError(code)
    source_polarity = Counter(match.group(0).casefold() for match in _POLARITY.finditer(source))
    candidate_polarity = Counter(match.group(0).casefold() for match in _POLARITY.finditer(candidate))
    if source_polarity != candidate_polarity:
        raise SafetyError("changed_polarity")
    source_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(source))
    candidate_modality = Counter(match.group(0).casefold() for match in _MODALITY.finditer(candidate))
    if source_modality != candidate_modality:
        raise SafetyError("changed_modality")
    source_length = len(re.sub(r"\W", "", source, flags=re.UNICODE))
    candidate_length = len(re.sub(r"\W", "", candidate, flags=re.UNICODE))
    if source_length >= 24 and candidate_length * 100 < source_length * 40:
        raise SafetyError("content_loss")
    if source_length >= 12 and candidate_length > source_length * 3 + 40:
        raise SafetyError("content_addition")

"""Deterministic, review-scoped repairs for canonical target documents."""

from __future__ import annotations

import re
from collections.abc import Mapping, Set

from .document_ast import Block, BlockType, Document, Inline
from .target_renderer import render_html

_SUPPORTED_FLAGS = frozenset(
    {
        "accidental_repetition",
        "awkward_pronoun_repetition",
        "greeting_agreement_error",
        "missing_government_office_article",
        "redundant_heading",
        "rich_text_nonbreaking_space_missing",
        "technical_compound_error",
        "technical_term_agreement_error",
    }
)
_FEMALE_GIVEN_NAMES = frozenset({"Elin", "Lea", "Mara", "Sina", "Vera", "Kira", "Pia"})
_MALE_GIVEN_NAMES = frozenset({"Jonas", "Timo"})
_REPEATED_PERSON = re.compile(
    r"(?P<name>(?P<given>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)"
    r"(?: [A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)+(?: \d+)?)"
    r"(?P<prefix> teilte mit, dass )(?P=name)\b"
)
_BAD_FEMALE_SALUTATION = re.compile(r"^Lieber (?P<name>(?:Elin|Lea|Mara|Sina|Vera|Kira|Pia)\b)")
_BAD_MALE_SALUTATION = re.compile(r"^Liebe (?P<name>(?:Jonas|Timo)\b)")


def _with_text(block: Block, value: str) -> Block:
    if value == block.text:
        return block
    if block.items or block.lines:
        raise ValueError("review-scoped text repair cannot rewrite list or signature blocks")
    if len(block.inlines) > 1:
        raise ValueError("review-scoped text repair cannot cross styled inline boundaries")
    inlines = (Inline(value, style=block.inlines[0].style),) if block.inlines else ()
    return Block(type=block.type, text=value, inlines=inlines)


def _natural_pronoun(match: re.Match[str]) -> str:
    given = match.group("given")
    if given in _FEMALE_GIVEN_NAMES:
        pronoun = "sie"
    elif given in _MALE_GIVEN_NAMES:
        pronoun = "er"
    else:
        raise ValueError(f"cannot determine reviewed person pronoun for {given!r}")
    return f"{match.group('name')}{match.group('prefix')}{pronoun}"


def _repair_text(value: str, flags: Set[str]) -> str:
    repaired = value
    if "missing_government_office_article" in flags:
        repaired = repaired.replace(" von Finanzamt ", " vom Finanzamt ")
    if "greeting_agreement_error" in flags:
        repaired = _BAD_FEMALE_SALUTATION.sub(r"Liebe \g<name>", repaired)
        repaired = _BAD_MALE_SALUTATION.sub(r"Lieber \g<name>", repaired)
    if "awkward_pronoun_repetition" in flags:
        repaired = _REPEATED_PERSON.sub(_natural_pronoun, repaired)
        repaired = repaired.replace(
            "Wir formulieren die Hinweise so, dass die Hinweise",
            "Wir formulieren die Hinweise so, dass sie",
        )
    if "technical_compound_error" in flags:
        repaired = repaired.replace(
            "eine idempotency key-Prüfung",
            "eine Prüfung des „idempotency key“",
        )
    if "technical_term_agreement_error" in flags:
        repaired = repaired.replace("Der „canary deployment“", "Das „canary deployment“")
        repaired = repaired.replace("der „canary deployment“", "das „canary deployment“")
        repaired = repaired.replace("Den „feature flag“", "Das „feature flag“")
        repaired = repaired.replace("den „feature flag“", "das „feature flag“")
    return repaired


def _declared_distinct_topic(plan: Mapping[str, object], used: set[str]) -> str:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("repair plan requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, Mapping):
        raise ValueError("repair plan requires semantic_plan.structure")
    topics = structure.get("paragraph_topics")
    if not isinstance(topics, list) or not all(isinstance(topic, str) and topic for topic in topics):
        raise ValueError("repair plan requires declared paragraph topics")
    for topic in topics:
        if topic.casefold().strip() not in used:
            return topic
    raise ValueError("no declared distinct topic can repair the redundant heading")


def repair_document(
    document: Document,
    plan: Mapping[str, object],
    critic_flags: Set[str],
) -> Document:
    """Apply only the closed set of repairs named by an AI critic verdict."""

    flags = frozenset(critic_flags)
    unsupported = sorted(flags.difference(_SUPPORTED_FLAGS))
    if unsupported:
        raise ValueError(f"unsupported critic repair flag: {unsupported[0]}")

    blocks = [_with_text(block, _repair_text(block.text, flags)) if block.text else block for block in document.blocks]
    if flags.intersection({"accidental_repetition", "redundant_heading"}):
        used_headings: set[str] = set()
        repaired_duplicate = False
        for index, block in enumerate(blocks):
            if block.type not in {BlockType.SUBJECT, BlockType.HEADING_1, BlockType.HEADING_2}:
                continue
            normalized = block.text.casefold().strip()
            if normalized in used_headings and block.type in {BlockType.HEADING_1, BlockType.HEADING_2}:
                topic = _declared_distinct_topic(plan, used_headings)
                blocks[index] = _with_text(block, topic)
                used_headings.add(topic.casefold().strip())
                repaired_duplicate = True
            else:
                used_headings.add(normalized)
        if not repaired_duplicate:
            raise ValueError("critic reported a redundant heading but no duplicate heading was found")

    repaired = Document(blocks=tuple(blocks))
    plain = "\n".join(block.text for block in repaired.blocks if block.text)
    residual_checks = {
        "missing_government_office_article": " von Finanzamt " in plain,
        "greeting_agreement_error": bool(_BAD_FEMALE_SALUTATION.search(plain) or _BAD_MALE_SALUTATION.search(plain)),
        "awkward_pronoun_repetition": bool(
            _REPEATED_PERSON.search(plain) or "Wir formulieren die Hinweise so, dass die Hinweise" in plain
        ),
        "technical_compound_error": "idempotency key-Prüfung" in plain,
        "technical_term_agreement_error": any(
            marker in plain
            for marker in (
                "der „canary deployment“",
                "Der „canary deployment“",
                "den „feature flag“",
                "Den „feature flag“",
            )
        ),
    }
    for flag, remains in residual_checks.items():
        if flag in flags and remains:
            raise ValueError(f"critic repair left residual defect: {flag}")
    if "rich_text_nonbreaking_space_missing" in flags and "&nbsp;" not in render_html(repaired):
        raise ValueError("critic reported rich-text spacing but the repaired HTML has no nonbreaking space")
    return repaired


__all__ = ["repair_document"]

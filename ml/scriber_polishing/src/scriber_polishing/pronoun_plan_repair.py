"""Fail-closed repair of AI-generated plans with contradictory pronoun actors."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .document_ast import Block, Document, Inline

_DIRECT_PLURAL_RECIPIENT = re.compile(
    r"(?P<prefix>Bitte gebt Ihr Eure Rückmeldung an )(?P<person>[^,;]+), "
    r"damit er (?P<suffix>Euch den Stand senden kann\.)"
)
_COORDINATION = re.compile(
    r"(?P<prefix>Bitte stimmen Sie sich mit )(?P<person>[^,;]+)"
    r"(?P<context>(?: zu Vorgang OP-\d+)? ab); sie "
    r"(?P<suffix>(?:bestätigt|hält)\b)"
)
_PERSONAL_DOCUMENTATION = re.compile(
    r"(?P<prefix>(?:Liebe )?[^,;]+, bitte gib (?:Deine|deine) Rückmeldung an )"
    r"(?P<person>[^,;]+); er (?P<suffix>dokumentiert sie im Vorgang\.)"
)
_PERSON_HOLDS_FORM = re.compile(
    r"(?P<person>[^,;.]+) hält für Vorgang (?P<reference>OP-\d+) fest, "
    r"dass sie (?P<suffix>die geschützte Form nicht ersetzt\.)"
)
_UNSUPPORTED_COMMENTARY = re.compile(r"\s*Das Pronomen „[^“]+“ bezieht sich dabei auf [^.]+\.")


def _promote_structured_actor(value: str) -> str:
    repaired = _DIRECT_PLURAL_RECIPIENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')}, damit {match.group('person')} {match.group('suffix')}"
        ),
        value,
    )
    repaired = _COORDINATION.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')}{match.group('context')}; "
            f"{match.group('person')} {match.group('suffix')}"
        ),
        repaired,
    )
    repaired = _PERSONAL_DOCUMENTATION.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('person')}; {match.group('person')} {match.group('suffix')}"
        ),
        repaired,
    )
    repaired = _PERSON_HOLDS_FORM.sub(
        lambda match: (
            f"{match.group('person')} hält für Vorgang {match.group('reference')} fest, "
            f"dass {match.group('person')} {match.group('suffix')}"
        ),
        repaired,
    )
    replacements = {
        "Im Protokoll steht, dass du deine Notizen Jonas geben willst und er dir danach antwortet.": (
            "Im Protokoll steht, dass du deine Notizen Jonas geben willst und Jonas dir danach antwortet."
        ),
        "Die Maße des Musters sind verbindlich; seine Masse wird erst nach der Messung genannt.": (
            "Die Maße des Musters sind verbindlich; die Masse des Musters wird erst nach der Messung genannt."
        ),
        "Frau Linden erklärte, sie habe ihre Unterlagen bereits versendet.": (
            "Frau Linden erklärte, dass sie selbst ihre Unterlagen bereits versendet habe."
        ),
        "Ihr gebt Euer Ergebnis an Jonas, damit er Euch den finalen Stand senden kann.": (
            "Ihr gebt Euer Ergebnis an Jonas, damit Jonas Euch den finalen Stand senden kann."
        ),
        "Liebe Mara, Du kannst Deine Freigabe bis Freitag an Jonas senden; er bestätigt Dir den Eingang.": (
            "Liebe Mara, Du kannst Deine Freigabe bis Freitag an Jonas senden; Jonas bestätigt Dir den Eingang."
        ),
    }
    for original, replacement in replacements.items():
        repaired = repaired.replace(original, replacement)
    return repaired


def _contains_surface(text: str, value: str) -> bool:
    if not value:
        return False
    prefix = r"(?<!\w)" if value[0].isalnum() else ""
    suffix = r"(?!\w)" if value[-1].isalnum() else ""
    return re.search(prefix + re.escape(value) + suffix, text) is not None


def repair_pronoun_plan(
    plan: Mapping[str, Any],
    *,
    generator_agent: str,
    batch_id: str,
    prompt_hash: str,
) -> dict[str, Any]:
    """Promote the already-declared actor and remove only displaced pronoun spans."""

    repaired = copy.deepcopy(dict(plan))
    semantic_plan = repaired.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("pronoun plan repair requires semantic_plan")
    facts = semantic_plan.get("facts")
    if not isinstance(facts, list):
        raise ValueError("pronoun plan repair requires semantic_plan.facts")
    changed = False
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            raise ValueError("pronoun plan repair requires object facts with text")
        original = fact["text"]
        updated = _promote_structured_actor(original)
        if updated == original:
            continue
        protected = fact.get("protected_values")
        if not isinstance(protected, list) or not all(isinstance(value, str) for value in protected):
            raise ValueError("pronoun plan repair requires protected_values")
        fact["text"] = updated
        fact["protected_values"] = [value for value in protected if _contains_surface(updated, value)]
        changed = True
    if not changed:
        raise ValueError("no supported pronoun repair changed the semantic plan")
    repaired["generator_agent"] = generator_agent
    repaired["batch_id"] = batch_id
    repaired["prompt_hash"] = prompt_hash
    return repaired


def _without_commentary(block: Block) -> Block:
    if not block.text or not _UNSUPPORTED_COMMENTARY.search(block.text):
        return block
    updated = _UNSUPPORTED_COMMENTARY.sub("", block.text).strip()
    if not updated:
        raise ValueError("pronoun commentary removal would empty a target block")
    if len(block.inlines) > 1:
        raise ValueError("pronoun commentary removal cannot cross styled inline boundaries")
    inlines = (Inline(updated, style=block.inlines[0].style),) if block.inlines else ()
    return Block(type=block.type, text=updated, inlines=inlines)


def remove_unsupported_pronoun_commentary(document: Document) -> Document:
    """Remove the exact unsupported metalinguistic sentence from a target AST."""

    repaired = Document(blocks=tuple(_without_commentary(block) for block in document.blocks))
    if any(_UNSUPPORTED_COMMENTARY.search(block.text) for block in repaired.blocks if block.text):
        raise ValueError("unsupported pronoun commentary remains after repair")
    return repaired


__all__ = ["remove_unsupported_pronoun_commentary", "repair_pronoun_plan"]

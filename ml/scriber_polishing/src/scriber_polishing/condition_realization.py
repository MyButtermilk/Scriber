"""Closed semantic equivalences for conditions already present in target prose."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .document_ast import Block, Document, Inline

_COMMENTARY_MARKER = " Die Regelung gilt unter folgender Bedingung: "
_APPENDED_CONDITION = re.compile(
    r"^(?P<body>.+)\. Die Regelung gilt(?P<comma>,?) (?P<condition>.+)\.$"
)
_MODAL_BODY = re.compile(r"^(?P<article>Der|Die|Das) (?P<subject>.+?) kann (?P<predicate>.+)$")
_CONSENT_CONDITION = re.compile(r"^(?P<actor>.+?) stimmt zu$")
_MISSING_REFERENCE_CONDITION = re.compile(
    r"^(?P<reference>[A-Z]{2,5}-\d{3}) is not available by "
    r"(?P<date>\d{4}-\d{2}-\d{2})$"
)
_RECONCILIATION_CONDITION = re.compile(
    r"^invoice reconciliation for (?P<amount>\$ \d[\d,]*\.\d{2}) is complete$"
)
_CLOSED_RESTATEMENT = re.compile(
    r"^(?P<body>.+) Dies gilt, "
    r"(?P<condition>wenn die Prüfung abgeschlossen ist)\.$"
)


def _comparison_form(value: str) -> str:
    value = value.replace("„", "").replace("“", "")
    return re.sub(r"\s+", " ", value).strip(" .;:").casefold()


def _condition_comparison_form(value: str) -> str:
    normalized = _comparison_form(value)
    return re.sub(r"\bfiktiv(?:e|en|er|es)?\s+", "", normalized)


def condition_is_realized(condition: str, rendered_fact: str) -> bool:
    """Return true only for exact or independently audited equivalence families."""

    normalized_condition = _comparison_form(condition)
    normalized_fact = _comparison_form(rendered_fact)
    if normalized_condition in normalized_fact:
        return True
    fixed_pairs = (
        ("die prüfung schlägt fehl", r"\bfalls die prüfung fehlschlägt\b"),
        (
            "wenn die prüfung abgeschlossen ist",
            r"\bdie prüfung ist vollständig, sodass die freigabe erfolgen kann\b",
        ),
        (
            "es liegt keine schriftliche zustimmung vor",
            r"\bohne schriftliche zustimmung\b",
        ),
        ("es liegt keine zustimmung vor", r"\bohne zustimmung\b"),
        ("die lieferung ist bestätigt", r"\bbei bestätigter lieferung\b"),
    )
    for expected_condition, fact_pattern in fixed_pairs:
        if normalized_condition == expected_condition:
            return re.search(fact_pattern, normalized_fact) is not None

    consent = _CONSENT_CONDITION.fullmatch(condition.strip().rstrip("."))
    if consent:
        expected = _comparison_form(f"wenn {consent.group('actor')} zustimmt")
        if expected in normalized_fact:
            return True

    missing_reference = _MISSING_REFERENCE_CONDITION.fullmatch(condition.strip().rstrip("."))
    if missing_reference:
        expected = _comparison_form(
            f"falls {missing_reference.group('reference')} bis "
            f"{missing_reference.group('date')} nicht vorliegt"
        )
        if expected in normalized_fact:
            return True

    reconciliation = _RECONCILIATION_CONDITION.fullmatch(condition.strip().rstrip("."))
    if reconciliation:
        amount = re.escape(_comparison_form(reconciliation.group("amount")))
        pattern = rf"\bwenn (?:die )?invoice reconciliation für {amount} abgeschlossen ist\b"
        return re.search(pattern, normalized_fact) is not None
    return False


def _without_condition_commentary(
    block: Block,
    conditions: Sequence[str],
    consumed: set[int],
) -> Block:
    if not block.text or _COMMENTARY_MARKER not in block.text:
        return block
    if block.text.count(_COMMENTARY_MARKER) != 1:
        raise ValueError("condition commentary repair requires exactly one marker per block")
    updated, discarded = block.text.split(_COMMENTARY_MARKER, maxsplit=1)
    updated = updated.rstrip()
    if not updated or not discarded.strip():
        raise ValueError("condition commentary repair encountered an incomplete marker")
    matches = [
        index
        for index, condition in enumerate(conditions)
        if index not in consumed and condition_is_realized(condition, updated)
    ]
    if len(matches) != 1:
        raise ValueError("appended condition is not already realized by exactly one declared fact")
    consumed.add(matches[0])
    if len(block.inlines) > 1:
        raise ValueError("condition commentary repair cannot cross styled inline boundaries")
    inlines = (Inline(updated, style=block.inlines[0].style),) if block.inlines else ()
    return Block(type=block.type, text=updated, inlines=inlines)


def remove_redundant_condition_commentary(
    document: Document,
    conditions: Sequence[str],
) -> Document:
    """Remove only audited commentary backed by one unchanged declared condition."""

    marker_count = sum(_COMMENTARY_MARKER in block.text for block in document.blocks if block.text)
    if not marker_count:
        raise ValueError("no redundant condition commentary found")
    if not conditions or not all(isinstance(condition, str) and condition for condition in conditions):
        raise ValueError("condition commentary repair requires declared conditions")
    consumed: set[int] = set()
    repaired = Document(
        blocks=tuple(
            _without_condition_commentary(block, conditions, consumed)
            for block in document.blocks
        )
    )
    if len(consumed) != marker_count:
        raise ValueError("condition commentary repair did not bind every removed sentence")
    if any(_COMMENTARY_MARKER in block.text for block in repaired.blocks if block.text):
        raise ValueError("redundant condition commentary remains after repair")
    return repaired


def _matching_condition_index(
    rendered_condition: str,
    conditions: Sequence[str],
    consumed: set[int],
) -> int:
    normalized = _condition_comparison_form(rendered_condition)
    matches = [
        index
        for index, condition in enumerate(conditions)
        if index not in consumed and _condition_comparison_form(condition) == normalized
    ]
    if not matches:
        raise ValueError("appended condition does not match an unconsumed declared condition")
    return matches[0]


def _integrated_condition_block(
    block: Block,
    conditions: Sequence[str],
    consumed: set[int],
) -> Block:
    if not block.text or " Die Regelung gilt" not in block.text:
        return block
    match = _APPENDED_CONDITION.fullmatch(block.text)
    if match is None or "unter folgender Bedingung:" in match.group("condition"):
        raise ValueError("appended condition commentary is outside the closed integration forms")
    condition = match.group("condition")
    condition_index = _matching_condition_index(condition, conditions, consumed)
    consumed.add(condition_index)
    body = match.group("body")
    if match.group("comma"):
        updated = f"{body}, {condition}."
    elif condition.casefold().startswith("nach "):
        modal = _MODAL_BODY.fullmatch(body)
        if modal is None:
            raise ValueError("prepositional condition requires the reviewed modal sentence form")
        article = modal.group("article").casefold()
        updated = (
            f"{condition[:1].upper() + condition[1:]} kann {article} "
            f"{modal.group('subject')} {modal.group('predicate')}."
        )
    else:
        raise ValueError("appended condition commentary uses an unsupported separator")
    if len(block.inlines) > 1:
        raise ValueError("condition integration cannot cross styled inline boundaries")
    inlines = (Inline(updated, style=block.inlines[0].style),) if block.inlines else ()
    return Block(type=block.type, text=updated, inlines=inlines)


def integrate_appended_condition_commentary(
    document: Document,
    conditions: Sequence[str],
) -> Document:
    """Integrate exact declared conditions instead of emitting a template sentence."""

    marker_count = sum(" Die Regelung gilt" in block.text for block in document.blocks if block.text)
    if not marker_count:
        raise ValueError("no appended condition commentary found")
    if not conditions or not all(isinstance(condition, str) and condition for condition in conditions):
        raise ValueError("condition integration requires declared conditions")
    consumed: set[int] = set()
    repaired = Document(
        blocks=tuple(
            _integrated_condition_block(block, conditions, consumed)
            for block in document.blocks
        )
    )
    if len(consumed) != marker_count:
        raise ValueError("condition integration did not bind every transformed sentence")
    if any(" Die Regelung gilt" in block.text for block in repaired.blocks if block.text):
        raise ValueError("appended condition commentary remains after integration")
    return repaired


def _without_closed_restatement(
    block: Block,
    conditions: Sequence[str],
    consumed: set[int],
) -> Block:
    if not block.text or " Dies gilt, " not in block.text:
        return block
    match = _CLOSED_RESTATEMENT.fullmatch(block.text)
    if match is None:
        raise ValueError("condition restatement is outside the closed equivalence form")
    rendered_condition = match.group("condition")
    body = match.group("body")
    candidates = [
        index
        for index, condition in enumerate(conditions)
        if index not in consumed
        and _comparison_form(condition) == _comparison_form(rendered_condition)
        and condition_is_realized(condition, body)
    ]
    if len(candidates) != 1:
        raise ValueError("condition restatement lacks one closed declared equivalence")
    consumed.add(candidates[0])
    if len(block.inlines) > 1:
        raise ValueError("condition restatement repair cannot cross styled inline boundaries")
    inlines = (Inline(body, style=block.inlines[0].style),) if block.inlines else ()
    return Block(type=block.type, text=body, inlines=inlines)


def remove_redundant_condition_restatement(
    document: Document,
    conditions: Sequence[str],
) -> Document:
    """Remove only the audited completed-check restatement equivalence."""

    marker_count = sum(" Dies gilt, " in block.text for block in document.blocks if block.text)
    if not marker_count:
        raise ValueError("no closed condition restatement found")
    if not conditions or not all(isinstance(condition, str) and condition for condition in conditions):
        raise ValueError("condition restatement repair requires declared conditions")
    consumed: set[int] = set()
    repaired = Document(
        blocks=tuple(
            _without_closed_restatement(block, conditions, consumed)
            for block in document.blocks
        )
    )
    if len(consumed) != marker_count:
        raise ValueError("condition restatement repair did not bind every removed sentence")
    if any(" Dies gilt, " in block.text for block in repaired.blocks if block.text):
        raise ValueError("condition restatement remains after repair")
    return repaired


__all__ = [
    "condition_is_realized",
    "integrate_appended_condition_commentary",
    "remove_redundant_condition_commentary",
    "remove_redundant_condition_restatement",
]

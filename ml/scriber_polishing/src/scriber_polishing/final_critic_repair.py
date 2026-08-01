"""Closed deterministic repairs for findings from the final blinded critics."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .document_ast import Block, BlockType, Document, Inline, ListItem

ENGLISH_RECORD_REFERENCE = "english_record_reference"
REDUNDANT_RELEASE_CONDITION = "redundant_release_condition"
ARTIFICIAL_MEETING_VALUE = "artificial_meeting_value"
UNSUPPORTED_UNCHANGED_BLOCK = "unsupported_unchanged_block"
HEADING_INITIAL_LOWERCASE = "heading_initial_lowercase"
GRAMMAR_CASE_AGREEMENT = "grammar_case_agreement"
MISSING_REQUIRED_ARTICLE = "missing_required_article"
MALFORMED_MIXED_LANGUAGE_COMPOUND = "malformed_mixed_language_compound"
REDUNDANT_REVIEW_PHRASE = "redundant_review_phrase"
REDUNDANT_COMPLETION_CONDITION = "redundant_completion_condition"
ARTIFICIAL_REGISTRATION_LANGUAGE = "artificial_registration_language"
ARTIFICIAL_TICKET_LANGUAGE = "artificial_ticket_language"
PRESERVE_CODE_SWITCH_LANGUAGE = "preserve_code_switch_language"
MISSING_GENERAL_ARTICLE = "missing_general_article"
MISSING_REE_LIST_ARTICLE = "missing_ree_list_article"
MISSING_TAX_ARTICLE = "missing_tax_article"
ARTIFICIAL_CAUSAL_NOUN = "artificial_causal_noun"

_KNOWN_FINDINGS = frozenset(
    {
        ENGLISH_RECORD_REFERENCE,
        REDUNDANT_RELEASE_CONDITION,
        ARTIFICIAL_MEETING_VALUE,
        UNSUPPORTED_UNCHANGED_BLOCK,
        HEADING_INITIAL_LOWERCASE,
        GRAMMAR_CASE_AGREEMENT,
        MISSING_REQUIRED_ARTICLE,
        MALFORMED_MIXED_LANGUAGE_COMPOUND,
        REDUNDANT_REVIEW_PHRASE,
        REDUNDANT_COMPLETION_CONDITION,
        ARTIFICIAL_REGISTRATION_LANGUAGE,
        ARTIFICIAL_TICKET_LANGUAGE,
        PRESERVE_CODE_SWITCH_LANGUAGE,
        MISSING_GENERAL_ARTICLE,
        MISSING_REE_LIST_ARTICLE,
        MISSING_TAX_ARTICLE,
        ARTIFICIAL_CAUSAL_NOUN,
    }
)


@dataclass(frozen=True, slots=True)
class FinalCriticRepair:
    """One repaired plan/document pair plus auditable occurrence counts."""

    plan: dict[str, Any]
    document: Document
    applied_occurrences: tuple[tuple[str, int], ...]


def _replace_block_text(block: Block, old: str, new: str) -> Block:
    if block.text != old:
        return block
    if block.items or block.lines:
        raise ValueError("final critic text repair requires a text block")
    if not block.inlines:
        return Block(type=block.type, text=new)
    if len(block.inlines) != 1 or block.inlines[0].text != old:
        raise ValueError("final critic text repair cannot cross styled inline boundaries")
    return Block(
        type=block.type,
        text=new,
        inlines=(Inline(new, block.inlines[0].style),),
    )


def _replace_unique_document_text(document: Document, old: str, new: str) -> Document:
    matches = [index for index, block in enumerate(document.blocks) if block.text == old]
    if len(matches) != 1:
        raise ValueError("final critic source phrase must occur in exactly one text block")
    blocks = list(document.blocks)
    index = matches[0]
    blocks[index] = _replace_block_text(blocks[index], old, new)
    return Document(blocks=tuple(blocks))


def _replace_unique_document_substring(
    document: Document,
    old: str,
    new: str,
) -> Document:
    matches = [
        index
        for index, block in enumerate(document.blocks)
        if block.text.count(old) == 1
    ]
    if len(matches) != 1:
        raise ValueError("final critic source substring must occur in exactly one text block")
    blocks = list(document.blocks)
    index = matches[0]
    block = blocks[index]
    if block.items or block.lines:
        raise ValueError("final critic substring repair requires a text block")
    if block.inlines:
        inline_matches = [
            part_index
            for part_index, part in enumerate(block.inlines)
            if part.text.count(old) == 1
        ]
        if len(inline_matches) != 1:
            raise ValueError("final critic substring cannot cross styled boundaries")
        inlines = list(block.inlines)
        part_index = inline_matches[0]
        part = inlines[part_index]
        inlines[part_index] = Inline(part.text.replace(old, new), part.style)
        blocks[index] = Block(
            type=block.type,
            text=block.text.replace(old, new),
            inlines=tuple(inlines),
        )
    else:
        blocks[index] = Block(type=block.type, text=block.text.replace(old, new))
    return Document(blocks=tuple(blocks))


def _replace_unique_fact_substring(
    plan: dict[str, Any],
    old: str,
    new: str,
) -> dict[str, Any]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("final critic fact repair requires semantic_plan.facts")
    matches: list[dict[str, Any]] = []
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            raise ValueError("final critic fact repair encountered a malformed fact")
        if fact["text"].count(old) == 1:
            matches.append(fact)
    if len(matches) != 1:
        raise ValueError("final critic source substring must occur in exactly one fact")
    matches[0]["text"] = matches[0]["text"].replace(old, new)
    return plan


def _replace_fact_substring_occurrences(
    plan: dict[str, Any],
    old: str,
    new: str,
) -> tuple[dict[str, Any], int]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("final critic fact repair requires semantic_plan.facts")
    matches: list[dict[str, Any]] = []
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            raise ValueError("final critic fact repair encountered a malformed fact")
        if old in fact["text"]:
            matches.append(fact)
    if len(matches) != 1:
        raise ValueError("final critic source substring must occur in exactly one fact")
    occurrences = matches[0]["text"].count(old)
    matches[0]["text"] = matches[0]["text"].replace(old, new)
    return plan, occurrences


def _terminal_sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


def _naturalize_english_record_reference(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("English reference repair requires semantic_plan")
    facts = semantic_plan.get("facts")
    if not isinstance(facts, list):
        raise ValueError("English reference repair requires semantic_plan.facts")

    match: tuple[dict[str, Any], list[str]] | None = None
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("English reference repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 5 or not all(
            isinstance(value, str) for value in values
        ):
            continue
        first, reference, product, version, second = values
        old_fact = (
            f"Fictional {first} records reference {reference} for {product} {version} "
            f"with fictional {second}."
        )
        if fact.get("text") == old_fact:
            if match is not None:
                raise ValueError("English reference repair matched more than one fact")
            match = (fact, values)
    if match is None:
        raise ValueError("English reference repair did not match its closed source phrase")

    fact, values = match
    first, reference, product, version, second = values
    old_target = _terminal_sentence(
        f"Fictional {first} records reference {reference} for {product} {version} "
        f"together with fictional {second}"
    )
    new_text = _terminal_sentence(
        f"The fictional organization {first} documents reference {reference} for "
        f"{product} {version} together with the fictional organization {second}"
    )
    fact["text"] = new_text
    return plan, _replace_unique_document_text(document, old_target, new_text)


def _repair_redundant_release_condition(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("release-condition repair requires semantic_plan.facts")
    match: tuple[dict[str, Any], str, str] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("release-condition repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 2 or not all(
            isinstance(value, str) for value in values
        ):
            continue
        date, project = values
        old_fact = (
            f"Der Termin am {date} für {project} bleibt vorbehaltlich der "
            "Freigabe bestehen."
        )
        if fact.get("text") == old_fact:
            if match is not None:
                raise ValueError("release-condition repair matched more than one fact")
            match = (fact, date, project)
    if match is None:
        raise ValueError("release-condition repair did not match its closed source phrase")

    fact, date, project = match
    old_target = (
        f"Der Termin am {date} für {project} bleibt vorbehaltlich der Freigabe "
        "bestehen, wenn die Freigabe vorliegt."
    )
    new_text = (
        f"Der Termin am {date} für {project} wird verbindlich, wenn die Freigabe "
        "vorliegt."
    )
    fact["text"] = new_text
    return plan, _replace_unique_document_text(document, old_target, new_text)


def _repair_artificial_meeting_value(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("meeting-value repair requires semantic_plan.facts")
    match: tuple[dict[str, Any], str, str, str] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("meeting-value repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 2 or not all(
            isinstance(value, str) for value in values
        ):
            continue
        reference, value = values
        candidates = (
            (
                f"The meeting refers to {reference} and the value {value}.",
                (
                    f"The meeting references {reference} and specifies a period "
                    f"of {value}."
                ),
            ),
            (
                f"Das Meeting verweist auf {reference} und den Wert {value}.",
                (
                    f"Das Meeting verweist auf {reference} und nennt einen "
                    f"Zeitraum von {value}."
                ),
            ),
        )
        for old, new in candidates:
            if fact.get("text") == old:
                if match is not None:
                    raise ValueError("meeting-value repair matched more than one fact")
                match = (fact, old, new, reference)
    if match is None:
        raise ValueError("meeting-value repair did not match its closed source phrase")

    fact, old, new, _reference = match
    fact["text"] = new
    return plan, _replace_unique_document_text(document, old, new)


def _remove_unsupported_unchanged_block(document: Document) -> Document:
    phrase = "Inhalt bleibt unverändert."
    matches = [
        index
        for index, block in enumerate(document.blocks)
        if block.text == phrase and not block.items and not block.lines
    ]
    if len(matches) != 1:
        raise ValueError("unsupported unchanged block must occur exactly once")
    blocks = tuple(
        block for index, block in enumerate(document.blocks) if index != matches[0]
    )
    if not blocks:
        raise ValueError("unsupported unchanged block repair cannot empty the document")
    return Document(blocks=blocks)


def _repair_heading_initial(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    replacements = {
        "die Budgetfreigabe": "Die Budgetfreigabe",
        "die geordnete Übergabe": "Die geordnete Übergabe",
        "die priorisierte Umsetzung": "Die priorisierte Umsetzung",
    }
    matching_blocks = [
        (index, block.text)
        for index, block in enumerate(document.blocks)
        if block.type in {BlockType.HEADING_1, BlockType.HEADING_2}
        and block.text in replacements
    ]
    if len(matching_blocks) != 1:
        raise ValueError("heading-initial repair requires one closed lowercase heading")
    block_index, old = matching_blocks[0]
    new = replacements[old]

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("heading-initial repair requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, dict):
        raise ValueError("heading-initial repair requires semantic_plan.structure")
    topics = structure.get("paragraph_topics")
    if not isinstance(topics, list) or topics.count(old) != 1:
        raise ValueError("heading-initial repair requires one matching paragraph topic")
    structure["paragraph_topics"] = [new if topic == old else topic for topic in topics]

    blocks = list(document.blocks)
    blocks[block_index] = _replace_block_text(blocks[block_index], old, new)
    return plan, Document(blocks=tuple(blocks))


def _repair_grammar_case_agreement(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    replacements = (
        (
            "Fortschritt des schema migration",
            "Fortschritt der schema migration",
            "Fortschritt des „schema migration“",
            "Fortschritt der „schema migration“",
        ),
        (
            "wird das idempotency key",
            "wird der idempotency key",
            "wird den „idempotency key“",
            "wird der „idempotency key“",
        ),
        (
            "eine webhook signature-Prüfung",
            "eine Prüfung der webhook signature",
            "eine Prüfung des „webhook signature“",
            "eine Prüfung der „webhook signature“",
        ),
    )
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("case-agreement repair requires semantic_plan.facts")

    match: tuple[dict[str, Any], tuple[str, str, str, str]] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            raise ValueError("case-agreement repair encountered a malformed fact")
        for replacement in replacements:
            if fact["text"].count(replacement[0]) == 1:
                if match is not None:
                    raise ValueError("case-agreement repair matched more than one fact")
                match = (fact, replacement)
    if match is None:
        raise ValueError("case-agreement repair did not match its closed source phrase")

    fact, (old_plan, new_plan, old_target, new_target) = match
    fact["text"] = fact["text"].replace(old_plan, new_plan)
    return plan, _replace_unique_document_substring(
        document,
        old_target,
        new_target,
    )


def _repair_missing_required_article(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    old = "für Betriebstemperatur"
    new = "für die Betriebstemperatur"
    plan = _replace_unique_fact_substring(plan, old, new)
    return plan, _replace_unique_document_substring(document, old, new)


def _repair_malformed_mixed_language_compounds(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    replacements = (
        (
            "API gateway-Rückmeldung",
            "Rückmeldung zum „API gateway“",
        ),
        (
            "service-level agreement-Grenze",
            "Grenze des „service-level agreement“",
        ),
    )
    for old, new in replacements:
        plan = _replace_unique_fact_substring(plan, old, new)
        document = _replace_unique_document_substring(document, old, new)
    return plan, document


def _repair_redundant_review_phrase(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    fact_text = (
        "Die Rückmeldung kann nach Prüfung der Unterlagen erfolgen; eine "
        "Vorfestlegung wird nicht getroffen."
    )
    old_target = (
        "Nach vollständiger Prüfung kann die Rückmeldung nach Prüfung der "
        "Unterlagen erfolgen; eine Vorfestlegung wird nicht getroffen."
    )
    new_text = (
        "Nach vollständiger Prüfung der Unterlagen kann die Rückmeldung "
        "erfolgen; eine Vorfestlegung wird nicht getroffen."
    )
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("redundant review repair requires semantic_plan.facts")
    matching_facts = [
        fact
        for fact in semantic_plan["facts"]
        if isinstance(fact, dict) and fact.get("text") == fact_text
    ]
    if len(matching_facts) != 1:
        raise ValueError("redundant review repair requires one matching semantic fact")
    matching_facts[0]["text"] = new_text
    return plan, _replace_unique_document_text(document, old_target, new_text)


def _repair_redundant_completion_condition(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    required_fact = (
        "Die Prüfung ist vollständig, sodass die Freigabe erfolgen kann."
    )
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("completion-condition repair requires semantic_plan.facts")
    matching_facts = [
        fact
        for fact in semantic_plan["facts"]
        if isinstance(fact, dict) and fact.get("text") == required_fact
    ]
    if len(matching_facts) != 1:
        raise ValueError("completion-condition repair requires one matching fact")
    return plan, _replace_unique_document_substring(
        document,
        " Dies gilt, wenn die Prüfung abgeschlossen ist.",
        "",
    )


def _repair_artificial_registration_language(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("registration-language repair requires semantic_plan.facts")
    match: tuple[dict[str, Any], str, str] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("registration-language repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 1 or not isinstance(
            values[0],
            str,
        ):
            continue
        reference = values[0]
        candidates = (
            (
                f"Registrations use only {reference}.",
                f"Registration is available only at {reference}.",
            ),
            (
                f"Anmeldungen verwenden ausschließlich {reference}.",
                f"Die Anmeldung ist ausschließlich unter {reference} möglich.",
            ),
        )
        for old, new in candidates:
            if fact.get("text") == old:
                if match is not None:
                    raise ValueError(
                        "registration-language repair matched more than one fact"
                    )
                match = (fact, old, new)
    if match is None:
        raise ValueError(
            "registration-language repair did not match its closed source phrase"
        )

    fact, old, new = match
    fact["text"] = new
    return plan, _replace_unique_document_text(document, old, new)


def _repair_artificial_ticket_language(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("ticket-language repair requires semantic_plan.facts")
    match: tuple[dict[str, Any], str, str] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("ticket-language repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 3 or not all(
            isinstance(value, str) for value in values
        ):
            continue
        ticket, owner, version = values
        old = (
            f"Die Übergabe ordnet das Ticket {ticket} {owner} zu und schützt "
            f"{version}."
        )
        new = (
            f"Die Übergabe ordnet das Ticket {ticket} {owner} zu; die Version "
            f"{version} bleibt geschützt."
        )
        if fact.get("text") == old:
            if match is not None:
                raise ValueError("ticket-language repair matched more than one fact")
            match = (fact, old, new)
    if match is None:
        raise ValueError("ticket-language repair did not match its closed source phrase")

    fact, old, new = match
    fact["text"] = new
    return plan, _replace_unique_document_text(document, old, new)


def _restore_code_switch_language(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("code-switch repair requires semantic_plan.facts")
    match: tuple[str, str] | None = None
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("code-switch repair encountered a malformed fact")
        values = fact.get("protected_values")
        if not isinstance(values, list) or len(values) != 2 or not all(
            isinstance(value, str) for value in values
        ):
            continue
        term, reference = values
        plan_text = (
            f"Bitte keep the technical term {term} and reference {reference} "
            "exactly as written."
        )
        if fact.get("text") != plan_text:
            continue
        translated_candidates = (
            (
                f"Der technische Begriff „{term}“ sowie die Referenz {reference} "
                "sind exakt beizubehalten."
            ),
            (
                f"Bitte behalten Sie den technischen Begriff „{term}“ sowie die "
                f"Referenz {reference} exakt bei."
            ),
        )
        translated = [
            candidate
            for candidate in translated_candidates
            if any(block.text == candidate for block in document.blocks)
        ]
        if len(translated) != 1 or match is not None:
            raise ValueError("code-switch repair requires one closed translated surface")
        expected = plan_text
        match = (translated[0], expected)
    if match is None:
        raise ValueError("code-switch repair did not match its closed source phrase")
    return plan, _replace_unique_document_text(document, match[0], match[1])


def _repair_missing_general_article(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document, int]:
    replacements = (
        ("bestätigt für Vorgang ", "bestätigt für den Vorgang "),
        ("hält für Vorgang ", "hält für den Vorgang "),
        ("bestätigt für Referenz ", "bestätigt zur Referenz "),
        ("zu Vorgang OP-", "zum Vorgang OP-"),
    )
    matches = [
        replacement
        for replacement in replacements
        if any(
            isinstance(fact, dict)
            and isinstance(fact.get("text"), str)
            and replacement[0] in fact["text"]
            for fact in plan.get("semantic_plan", {}).get("facts", [])
        )
    ]
    if len(matches) != 1:
        raise ValueError("general article repair requires one closed source pattern")
    old, new = matches[0]
    plan, plan_occurrences = _replace_fact_substring_occurrences(plan, old, new)
    document = _replace_unique_document_substring(document, old, new)
    return plan, document, plan_occurrences


def _repair_missing_ree_list_article(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    replacements = (
        ("Zuordnung zu Nachtrag ", "Zuordnung zum Nachtrag "),
        ("Zuordnung zu Prüfvermerk ", "Zuordnung zum Prüfvermerk "),
        ("Zuordnung zu Vorgang ", "Zuordnung zum Vorgang "),
    )
    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("REE list article repair requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, dict) or not isinstance(
        structure.get("list_items"),
        list,
    ):
        raise ValueError("REE list article repair requires structure.list_items")
    list_items = structure["list_items"]
    matches = [
        (item_index, old, new)
        for item_index, item in enumerate(list_items)
        if isinstance(item, str)
        for old, new in replacements
        if item.count(old) == 1
    ]
    if len(matches) != 1:
        raise ValueError("REE list article repair requires one closed list item")
    item_index, old, new = matches[0]
    list_items[item_index] = list_items[item_index].replace(old, new)

    document_matches: list[tuple[int, int]] = []
    for block_index, block in enumerate(document.blocks):
        if block.type not in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}:
            continue
        for target_index, item in enumerate(block.items):
            if item.text.count(old) == 1:
                document_matches.append((block_index, target_index))
    if len(document_matches) != 1:
        raise ValueError("REE list article repair requires one target list item")
    block_index, target_index = document_matches[0]
    blocks = list(document.blocks)
    block = blocks[block_index]
    items = list(block.items)
    item = items[target_index]
    inlines = tuple(
        Inline(part.text.replace(old, new), part.style) for part in item.inlines
    )
    items[target_index] = ListItem(
        level=item.level,
        text=item.text.replace(old, new),
        inlines=inlines,
    )
    blocks[block_index] = Block(type=block.type, items=tuple(items))
    return plan, Document(blocks=tuple(blocks))


def _repair_missing_tax_article(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    replacements = {
        "Abschreibungsbasis": "zur Abschreibungsbasis",
        "Aktennotiz": "zur Aktennotiz",
        "Bemessungsgrundlage": "zur Bemessungsgrundlage",
        "Bescheidvergleich": "zum Bescheidvergleich",
        "Kontenabstimmung": "zur Kontenabstimmung",
        "Mietaufwand": "zum Mietaufwand",
        "Rückfrage": "zur Rückfrage",
        "Verlustvortrag": "zum Verlustvortrag",
        "Vorsteuerabzug": "zum Vorsteuerabzug",
        "Zahlungsstand": "zum Zahlungsstand",
    }
    matches = [
        (f"zu {noun}", contracted)
        for noun, contracted in replacements.items()
        if any(
            isinstance(fact, dict)
            and isinstance(fact.get("text"), str)
            and f"Erläuterung zu {noun}" in fact["text"]
            for fact in plan.get("semantic_plan", {}).get("facts", [])
        )
    ]
    if len(matches) != 1:
        raise ValueError("tax article repair requires one closed explanation term")
    old, new = matches[0]
    plan = _replace_unique_fact_substring(plan, old, new)
    return plan, _replace_unique_document_substring(document, old, new)


def _repair_artificial_causal_noun(
    plan: dict[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    old = "der Wert ist keine Ursachebehauptung."
    new = "damit ist keine Aussage über eine Ursache verbunden."
    plan = _replace_unique_fact_substring(plan, old, new)
    return plan, _replace_unique_document_substring(document, old, new)


def apply_final_critic_repairs(
    plan: Mapping[str, Any],
    document: Document,
    finding_codes: frozenset[str],
) -> FinalCriticRepair:
    """Apply only explicitly authorized closed findings, failing on unknown codes."""

    unknown = finding_codes.difference(_KNOWN_FINDINGS)
    if unknown:
        raise ValueError(f"unknown final critic finding: {sorted(unknown)[0]}")

    repaired_plan = copy.deepcopy(dict(plan))
    repaired_document = document
    applied: list[tuple[str, int]] = []
    if ENGLISH_RECORD_REFERENCE in finding_codes:
        repaired_plan, repaired_document = _naturalize_english_record_reference(
            repaired_plan,
            repaired_document,
        )
        applied.append((ENGLISH_RECORD_REFERENCE, 1))
    if REDUNDANT_RELEASE_CONDITION in finding_codes:
        repaired_plan, repaired_document = _repair_redundant_release_condition(
            repaired_plan,
            repaired_document,
        )
        applied.append((REDUNDANT_RELEASE_CONDITION, 1))
    if ARTIFICIAL_MEETING_VALUE in finding_codes:
        repaired_plan, repaired_document = _repair_artificial_meeting_value(
            repaired_plan,
            repaired_document,
        )
        applied.append((ARTIFICIAL_MEETING_VALUE, 1))
    if UNSUPPORTED_UNCHANGED_BLOCK in finding_codes:
        repaired_document = _remove_unsupported_unchanged_block(repaired_document)
        applied.append((UNSUPPORTED_UNCHANGED_BLOCK, 1))
    if HEADING_INITIAL_LOWERCASE in finding_codes:
        repaired_plan, repaired_document = _repair_heading_initial(
            repaired_plan,
            repaired_document,
        )
        applied.append((HEADING_INITIAL_LOWERCASE, 1))
    if GRAMMAR_CASE_AGREEMENT in finding_codes:
        repaired_plan, repaired_document = _repair_grammar_case_agreement(
            repaired_plan,
            repaired_document,
        )
        applied.append((GRAMMAR_CASE_AGREEMENT, 1))
    if MISSING_REQUIRED_ARTICLE in finding_codes:
        repaired_plan, repaired_document = _repair_missing_required_article(
            repaired_plan,
            repaired_document,
        )
        applied.append((MISSING_REQUIRED_ARTICLE, 1))
    if MALFORMED_MIXED_LANGUAGE_COMPOUND in finding_codes:
        repaired_plan, repaired_document = _repair_malformed_mixed_language_compounds(
            repaired_plan,
            repaired_document,
        )
        applied.append((MALFORMED_MIXED_LANGUAGE_COMPOUND, 2))
    if REDUNDANT_REVIEW_PHRASE in finding_codes:
        repaired_plan, repaired_document = _repair_redundant_review_phrase(
            repaired_plan,
            repaired_document,
        )
        applied.append((REDUNDANT_REVIEW_PHRASE, 1))
    if REDUNDANT_COMPLETION_CONDITION in finding_codes:
        repaired_plan, repaired_document = _repair_redundant_completion_condition(
            repaired_plan,
            repaired_document,
        )
        applied.append((REDUNDANT_COMPLETION_CONDITION, 1))
    if ARTIFICIAL_REGISTRATION_LANGUAGE in finding_codes:
        repaired_plan, repaired_document = _repair_artificial_registration_language(
            repaired_plan,
            repaired_document,
        )
        applied.append((ARTIFICIAL_REGISTRATION_LANGUAGE, 1))
    if ARTIFICIAL_TICKET_LANGUAGE in finding_codes:
        repaired_plan, repaired_document = _repair_artificial_ticket_language(
            repaired_plan,
            repaired_document,
        )
        applied.append((ARTIFICIAL_TICKET_LANGUAGE, 1))
    if PRESERVE_CODE_SWITCH_LANGUAGE in finding_codes:
        repaired_plan, repaired_document = _restore_code_switch_language(
            repaired_plan,
            repaired_document,
        )
        applied.append((PRESERVE_CODE_SWITCH_LANGUAGE, 1))
    if MISSING_GENERAL_ARTICLE in finding_codes:
        repaired_plan, repaired_document, occurrences = (
            _repair_missing_general_article(
                repaired_plan,
                repaired_document,
            )
        )
        applied.append((MISSING_GENERAL_ARTICLE, occurrences))
    if MISSING_REE_LIST_ARTICLE in finding_codes:
        repaired_plan, repaired_document = _repair_missing_ree_list_article(
            repaired_plan,
            repaired_document,
        )
        applied.append((MISSING_REE_LIST_ARTICLE, 1))
    if MISSING_TAX_ARTICLE in finding_codes:
        repaired_plan, repaired_document = _repair_missing_tax_article(
            repaired_plan,
            repaired_document,
        )
        applied.append((MISSING_TAX_ARTICLE, 1))
    if ARTIFICIAL_CAUSAL_NOUN in finding_codes:
        repaired_plan, repaired_document = _repair_artificial_causal_noun(
            repaired_plan,
            repaired_document,
        )
        applied.append((ARTIFICIAL_CAUSAL_NOUN, 1))
    return FinalCriticRepair(
        plan=repaired_plan,
        document=repaired_document,
        applied_occurrences=tuple(applied),
    )

"""Closed, review-scoped plan and target repairs for adversarial findings."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .document_ast import Block, BlockType, Document, Inline, ListItem

_POSITION_FACT = re.compile(
    r"\bRückmeldung zur Position (?P<position>.+?) bis zum \d{2}\.\d{2}\.\d{4}\."
)
_OBLIGATION_DUE_DATE = re.compile(
    r"\bbis zum (?P<date>\d{2}\.\d{2}\.\d{4}) übermitteln\."
)
_FEEDBACK_LIST_DATE = re.compile(r"^Rückmeldung bis (?P<date>\d{2}\.\d{2}\.\d{4})$")
_ENGLISH_UNCERTAIN_DATE = re.compile(
    r"^.+ cannot yet confirm the date (?P<date>\d{4}-\d{2}-\d{2})\.$"
)
_GERMAN_UNCERTAIN_DATE = re.compile(
    r"^.+ kann den Termin (?P<date>\d{4}-\d{2}-\d{2}) noch nicht bestätigen\.$"
)
_TECHNICAL_COMPOUND = re.compile(
    r"\beine (?P<term>API gateway|observability dashboard|rate limit|rollback window|"
    r"service-level objective|webhook signature)-Prüfung\b"
)
_PERSON_NAME = (
    r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+"
    r"(?: [A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)+"
)
_EXPLICIT_ACTOR_PATTERNS = (
    re.compile(
        rf"(?P<name>{_PERSON_NAME}), damit "
        r"(?P<pronoun>er|sie)(?P<tail> Euch\b)"
    ),
    re.compile(
        rf"(?P<name>{_PERSON_NAME})(?P<intro> hält fest, dass )"
        r"(?P<pronoun>er|sie)(?P<tail> den Wortlaut\b)"
    ),
)
_LEXICAL_FICTION_MARKER = re.compile(
    r"\b(?:fictional|fiktiv(?:e|en|er|es)?)\b",
    re.IGNORECASE,
)
_COORDINATION_FICTION_FACT = re.compile(
    r"^Die fiktive (?P<first>.+?) stimmt sich mit der fiktiven "
    r"(?P<second>.+?) zu .+ ab\.$"
)
_GENERAL_CONDITION_FICTION = re.compile(
    r"wenn die fiktive (?P<entity>.+?) die Unterlagen vollständig bestätigt"
)
_TAX_ORGANIZATION_FICTION = re.compile(
    r"\. Die fiktive (?P<entity>.+?) bezieht sich auf die Mitteilung"
)
_REAL_ESTATE_FICTION_FACT = re.compile(
    r"^Die fiktive Organisation (?P<organization>.+?) bearbeitet für die "
    r"fiktive Liegenschaft (?P<property>.+?) in (?P<location>.+?) den "
    r"fiktiven (?P<operation>.+?)\.$"
)
_GERMAN_DOCUMENTATION_FICTION_FACT = re.compile(
    r"^Die fiktive (?P<first>.+?) dokumentiert .+? "
    r"(?:gemeinsam )?mit der fiktiven (?P<second>.+?)\.$"
)
_GERMAN_PERSON_FICTION_FACT = re.compile(
    r"^Die fiktive Ansprechperson (?P<person>.+?) "
    r"(?P<verb>empfiehlt|kann|muss|ordnet)\b"
)
_GERMAN_DIRECT_FICTION_FACT = re.compile(
    r"^Die fiktive (?P<entity>.+?) "
    r"(?P<verb>bestätigt|empfiehlt|schlägt|liefert)\b"
)
_ENGLISH_RECORD_FICTION_FACT = re.compile(
    r"^Fictional (?P<first>.+?) records .+? with fictional (?P<second>.+?)\.$"
)
_ENGLISH_DIRECT_FICTION_FACT = re.compile(
    r"^Fictional (?P<entity>.+?) (?P<verb>will provide)\b"
)
_OPERATION_FICTION_CASE = {
    "Abruf": ("den", "fiktiven"),
    "Los": ("das", "fiktive"),
    "Nachtrag": ("den", "fiktiven"),
    "Projektakte": ("die", "fiktive"),
    "Prüfvermerk": ("den", "fiktiven"),
    "Vorgang": ("den", "fiktiven"),
}
_GERMAN_TOPIC_TRANSLATIONS = {
    "Agreed actions and owners": "Vereinbarte Maßnahmen und Zuständigkeiten",
    "Billing reference and approval": "Abrechnungsreferenz und Freigabe",
    "Customer status and next step": "Kundenstatus und nächster Schritt",
    "Delivery status and dependencies": "Lieferstatus und Abhängigkeiten",
    "Forecast and budget control": "Prognose und Budgetkontrolle",
    "Incident context and handover": "Störungskontext und Übergabe",
    "Next step": "Nächster Schritt",
    "Protected references": "Geschützte Referenzen",
    "Protected wording and identifiers": "Geschützter Wortlaut und Kennungen",
    "Release scope and validation": "Freigabeumfang und Validierung",
    "Risk assessment and decision": "Risikobewertung und Entscheidung",
    "Session details and registration": "Termindetails und Anmeldung",
    "Supplier interface and timing": "Lieferantenschnittstelle und Zeitplanung",
}
_GERMAN_SIGNATURE_TRANSLATIONS = {
    "Fictional business operations": "Fiktiver Geschäftsbetrieb",
}


def _replace_block_surface(block: Block, old: str, new: str) -> Block:
    if old not in block.text:
        return block
    if block.text.count(old) != 1 or block.items or block.lines:
        raise ValueError("review-scoped phrase repair requires one text-block occurrence")
    if not block.inlines:
        return Block(type=block.type, text=block.text.replace(old, new))
    matching = [index for index, inline in enumerate(block.inlines) if old in inline.text]
    if len(matching) != 1:
        raise ValueError("review-scoped phrase repair cannot cross styled inline boundaries")
    index = matching[0]
    if block.inlines[index].text.count(old) != 1:
        raise ValueError("review-scoped phrase repair requires one inline occurrence")
    inlines = list(block.inlines)
    inline = inlines[index]
    inlines[index] = Inline(inline.text.replace(old, new), inline.style)
    return Block(type=block.type, text=block.text.replace(old, new), inlines=tuple(inlines))


def _replace_unique_document_surface(document: Document, old: str, new: str) -> Document:
    matches = [index for index, block in enumerate(document.blocks) if old in block.text]
    if len(matches) != 1:
        raise ValueError("review-scoped phrase is not present in exactly one text block")
    blocks = list(document.blocks)
    index = matches[0]
    blocks[index] = _replace_block_surface(blocks[index], old, new)
    return Document(blocks=tuple(blocks))


def _document_fiction_marker_count(document: Document) -> int:
    surfaces = [block.text for block in document.blocks if block.text]
    surfaces.extend(
        item.text
        for block in document.blocks
        if block.items
        for item in block.items
    )
    surfaces.extend(
        line
        for block in document.blocks
        if block.lines
        for line in block.lines
    )
    return len(_LEXICAL_FICTION_MARKER.findall("\n".join(surfaces)))


def _replace_unique_list_item(document: Document, old: str, new: str) -> Document:
    matches = [
        (block_index, item_index)
        for block_index, block in enumerate(document.blocks)
        if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
        for item_index, item in enumerate(block.items)
        if item.text == old
    ]
    if len(matches) != 1:
        raise ValueError("review-scoped list item is not present exactly once")
    block_index, item_index = matches[0]
    blocks = list(document.blocks)
    block = blocks[block_index]
    items = list(block.items)
    source = items[item_index]
    if len(source.inlines) > 1:
        raise ValueError("review-scoped list-item repair cannot cross styled boundaries")
    inlines = (Inline(new, source.inlines[0].style),) if source.inlines else ()
    items[item_index] = ListItem(level=source.level, text=new, inlines=inlines)
    blocks[block_index] = Block(type=block.type, items=tuple(items))
    return Document(blocks=tuple(blocks))


def _replace_plan_list_item(
    plan: Mapping[str, Any],
    old: str,
    new: str,
) -> dict[str, Any]:
    repaired = copy.deepcopy(dict(plan))
    semantic_plan = repaired.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("review-scoped plan repair requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, dict):
        raise ValueError("review-scoped plan repair requires semantic_plan.structure")
    list_items = structure.get("list_items")
    if not isinstance(list_items, list) or list_items.count(old) != 1:
        raise ValueError("review-scoped plan list item is not present exactly once")
    structure["list_items"] = [new if item == old else item for item in list_items]
    return repaired


def _localize_german_structural_text(value: str) -> str:
    for english, german in _GERMAN_TOPIC_TRANSLATIONS.items():
        if value == english:
            return german
        if value.startswith(english + " – "):
            return german + value[len(english) :]
    prefix_translations = (
        ("Reference ", "Referenz "),
        ("Owner ", "Verantwortlich: "),
        ("Deadline ", "Frist "),
        ("Sub-item: ", "Unterpunkt: "),
    )
    for english, german in prefix_translations:
        if value.startswith(english):
            return german + value[len(english) :]
    return _GERMAN_SIGNATURE_TRANSLATIONS.get(value, value)


def _contains_unlocalized_german_structure(value: str) -> bool:
    return (
        any(
            value == english or value.startswith(english + " – ")
            for english in _GERMAN_TOPIC_TRANSLATIONS
        )
        or value.startswith(("Reference ", "Owner ", "Deadline ", "Sub-item: "))
        or value in _GERMAN_SIGNATURE_TRANSLATIONS
    )


def preserve_feedback_position_role(
    plan: Mapping[str, Any],
    document: Document,
) -> Document:
    """Restore the exact declared position role in one reviewed feedback sentence."""

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("feedback-position repair requires semantic_plan")
    facts = semantic_plan.get("facts")
    if not isinstance(facts, list):
        raise ValueError("feedback-position repair requires semantic_plan.facts")
    positions: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping) or not isinstance(fact.get("text"), str):
            raise ValueError("feedback-position repair encountered a malformed fact")
        match = _POSITION_FACT.search(fact["text"])
        if match:
            positions.append(match.group("position"))
    if len(positions) != 1:
        raise ValueError("feedback-position repair requires exactly one declared position")
    position = positions[0]
    return _replace_unique_document_surface(
        document,
        f"Rückmeldung zum Thema „{position}“",
        f"Rückmeldung zur Position „{position}“",
    )


def bind_feedback_list_to_obligation_due_date(
    plan: Mapping[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    """Bind one feedback-list deadline to its declared obligation due date."""

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("deadline repair requires semantic_plan")
    facts = semantic_plan.get("facts")
    structure = semantic_plan.get("structure")
    if not isinstance(facts, list) or not isinstance(structure, Mapping):
        raise ValueError("deadline repair requires facts and structure")
    due_dates: list[str] = []
    fact_texts: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping) or not isinstance(fact.get("text"), str):
            raise ValueError("deadline repair encountered a malformed fact")
        fact_texts.append(fact["text"])
        if fact.get("modality") == "obligation":
            due_dates.extend(
                match.group("date") for match in _OBLIGATION_DUE_DATE.finditer(fact["text"])
            )
    if len(due_dates) != 1:
        raise ValueError("deadline repair requires exactly one obligation due date")
    list_items = structure.get("list_items")
    if not isinstance(list_items, list):
        raise ValueError("deadline repair requires declared list items")
    current_items = [
        (item, match.group("date"))
        for item in list_items
        if isinstance(item, str) and (match := _FEEDBACK_LIST_DATE.fullmatch(item))
    ]
    if len(current_items) != 1:
        raise ValueError("deadline repair requires exactly one feedback-list date")
    current_item, current_date = current_items[0]
    due_date = due_dates[0]
    if current_date == due_date:
        raise ValueError("feedback-list date is already bound to the obligation")
    if not any(current_date in text for text in fact_texts):
        raise ValueError("conflicting feedback-list date is not backed by another declared fact")
    replacement = f"Rückmeldung bis {due_date}"
    repaired_plan = _replace_plan_list_item(plan, current_item, replacement)
    repaired_document = _replace_unique_list_item(document, current_item, replacement)
    return repaired_plan, repaired_document


def mark_uncertain_list_date(
    plan: Mapping[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    """Label one plan/list date as unconfirmed when its fact is uncertain."""

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("uncertain-date repair requires semantic_plan")
    facts = semantic_plan.get("facts")
    if not isinstance(facts, list):
        raise ValueError("uncertain-date repair requires semantic_plan.facts")
    matches: list[tuple[str, str]] = []
    for fact in facts:
        if not isinstance(fact, Mapping) or not isinstance(fact.get("text"), str):
            raise ValueError("uncertain-date repair encountered a malformed fact")
        if fact.get("modality") != "uncertainty":
            continue
        english = _ENGLISH_UNCERTAIN_DATE.fullmatch(fact["text"])
        german = _GERMAN_UNCERTAIN_DATE.fullmatch(fact["text"])
        if english:
            matches.append(("en", english.group("date")))
        if german:
            matches.append(("de", german.group("date")))
    if len(matches) != 1:
        raise ValueError("uncertain-date repair requires one reviewed uncertainty fact")
    language, date = matches[0]
    current = f"Deadline {date}"
    replacement = (
        f"Date {date} not yet confirmed"
        if language == "en"
        else f"Termin {date} noch nicht bestätigt"
    )
    repaired_plan = _replace_plan_list_item(plan, current, replacement)
    repaired_document = _replace_unique_list_item(document, current, replacement)
    return repaired_plan, repaired_document


def naturalize_technical_compound(document: Document) -> Document:
    """Naturalize one reviewed compound without altering its English term."""

    matches = [
        match
        for block in document.blocks
        if block.text
        for match in _TECHNICAL_COMPOUND.finditer(block.text)
    ]
    if len(matches) != 1:
        raise ValueError("technical-compound repair requires exactly one reviewed scaffold")
    term = matches[0].group("term")
    return _replace_unique_document_surface(
        document,
        f"eine {term}-Prüfung",
        f"eine Prüfung des „{term}“",
    )


def paragraphize_single_unchanged_item(
    plan: Mapping[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    """Represent the reviewed one-item list as a normal paragraph."""

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping):
        raise ValueError("single-item-list repair requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, Mapping):
        raise ValueError("single-item-list repair requires semantic_plan.structure")
    if structure.get("list_kind") not in {"ordered", "unordered"}:
        raise ValueError("single-item-list repair requires a flat declared list")
    if structure.get("list_items") != ["Inhalt bleibt unverändert."]:
        raise ValueError("single-item-list repair is limited to the reviewed invariant item")

    list_blocks = [
        (index, block)
        for index, block in enumerate(document.blocks)
        if block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
    ]
    if len(list_blocks) != 1:
        raise ValueError("single-item-list repair requires exactly one list block")
    block_index, list_block = list_blocks[0]
    if len(list_block.items) != 1 or list_block.items[0].text != "Inhalt bleibt unverändert.":
        raise ValueError("single-item-list target differs from the reviewed invariant item")
    item = list_block.items[0]
    blocks = list(document.blocks)
    blocks[block_index] = Block(
        type=BlockType.PARAGRAPH,
        text=item.text,
        inlines=item.inlines,
    )

    repaired_plan = copy.deepcopy(dict(plan))
    repaired_structure = repaired_plan["semantic_plan"]["structure"]
    repaired_structure["list_kind"] = "none"
    repaired_structure["list_items"] = []
    return repaired_plan, Document(blocks=tuple(blocks))


def _promote_actors_in_text(value: str) -> tuple[str, tuple[str, ...], int]:
    repaired = value
    removed_pronouns: list[str] = []
    count = 0
    for pattern in _EXPLICIT_ACTOR_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            removed_pronouns.append(match.group("pronoun"))
            intro = match.groupdict().get("intro")
            if intro is None:
                return f"{match.group('name')}, damit {match.group('name')}{match.group('tail')}"
            return (
                f"{match.group('name')}{intro}{match.group('name')}"
                f"{match.group('tail')}"
            )

        repaired = pattern.sub(replace, repaired)
    return repaired, tuple(removed_pronouns), count


def promote_explicit_named_actors(
    plan: Mapping[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    """Prepare an ambiguity-free actor promotion using names already in each sentence."""

    repaired_plan = copy.deepcopy(dict(plan))
    semantic_plan = repaired_plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict) or not isinstance(semantic_plan.get("facts"), list):
        raise ValueError("actor-promotion repair requires semantic_plan.facts")
    plan_count = 0
    for fact in semantic_plan["facts"]:
        if (
            not isinstance(fact, dict)
            or not isinstance(fact.get("text"), str)
            or not isinstance(fact.get("protected_values"), list)
        ):
            raise ValueError("actor-promotion repair encountered a malformed fact")
        updated, removed, count = _promote_actors_in_text(fact["text"])
        if not count:
            continue
        fact["text"] = updated
        fact["protected_values"] = [
            value
            for value in fact["protected_values"]
            if not (
                isinstance(value, str)
                and value in removed
                and re.search(rf"\b{re.escape(value)}\b", updated) is None
            )
        ]
        plan_count += count

    blocks: list[Block] = []
    document_count = 0
    for block in document.blocks:
        if not block.text:
            blocks.append(block)
            continue
        updated, _, count = _promote_actors_in_text(block.text)
        if not count:
            blocks.append(block)
            continue
        if len(block.inlines) > 1:
            raise ValueError("actor-promotion repair cannot cross styled inline boundaries")
        inlines = (Inline(updated, block.inlines[0].style),) if block.inlines else ()
        blocks.append(Block(type=block.type, text=updated, inlines=inlines))
        document_count += count
    if not plan_count or plan_count != document_count:
        raise ValueError("actor-promotion plan and target occurrences do not match")
    return repaired_plan, Document(blocks=tuple(blocks))


def restore_lexical_fictionality(
    plan: Mapping[str, Any],
    document: Document,
) -> Document:
    """Restore every literal fiction qualifier declared in fact text."""

    semantic_plan = plan.get("semantic_plan")
    if not isinstance(semantic_plan, Mapping) or not isinstance(
        semantic_plan.get("facts"),
        list,
    ):
        raise ValueError("fictionality repair requires semantic_plan.facts")
    fact_texts: list[str] = []
    for fact in semantic_plan["facts"]:
        if not isinstance(fact, Mapping) or not isinstance(fact.get("text"), str):
            raise ValueError("fictionality repair encountered a malformed fact")
        fact_texts.append(fact["text"])
    expected_markers = sum(
        len(_LEXICAL_FICTION_MARKER.findall(text))
        for text in fact_texts
    )
    if not expected_markers:
        raise ValueError("fictionality repair requires literal fact-text markers")

    repaired = document
    applied_markers = 0
    for text in fact_texts:
        coordination = _COORDINATION_FICTION_FACT.fullmatch(text)
        real_estate = _REAL_ESTATE_FICTION_FACT.fullmatch(text)
        documentation = _GERMAN_DOCUMENTATION_FICTION_FACT.fullmatch(text)
        person = _GERMAN_PERSON_FICTION_FACT.search(text)
        direct_german = _GERMAN_DIRECT_FICTION_FACT.search(text)
        english_record = _ENGLISH_RECORD_FICTION_FACT.fullmatch(text)
        english_direct = _ENGLISH_DIRECT_FICTION_FACT.search(text)

        if coordination:
            first = coordination.group("first")
            second = coordination.group("second")
            repaired = _replace_unique_document_surface(
                repaired,
                f"{first} stimmt sich",
                f"Die fiktive {first} stimmt sich",
            )
            repaired = _replace_unique_document_surface(
                repaired,
                f"mit {second} zum Thema",
                f"mit der fiktiven {second} zum Thema",
            )
            applied_markers += 2
        elif real_estate:
            organization = real_estate.group("organization")
            property_name = real_estate.group("property")
            location = real_estate.group("location")
            operation = real_estate.group("operation")
            repaired = _replace_unique_document_surface(
                repaired,
                f"{organization} bearbeitet",
                f"Die fiktive Organisation {organization} bearbeitet",
            )
            repaired = _replace_unique_document_surface(
                repaired,
                f"für die Liegenschaft {property_name} in {location}",
                f"für die fiktive Liegenschaft {property_name} in {location}",
            )
            operation_noun = operation.split(" ", maxsplit=1)[0]
            operation_case = _OPERATION_FICTION_CASE.get(operation_noun)
            if operation_case is None:
                raise ValueError("fictionality repair encountered an unknown operation noun")
            article, adjective = operation_case
            repaired = _replace_unique_document_surface(
                repaired,
                f"{article} {operation}",
                f"{article} {adjective} {operation}",
            )
            applied_markers += 3
        elif documentation:
            first = documentation.group("first")
            second = documentation.group("second")
            repaired = _replace_unique_document_surface(
                repaired,
                f"Die {first} dokumentiert",
                f"Die fiktive {first} dokumentiert",
            )
            repaired = _replace_unique_document_surface(
                repaired,
                f"mit der {second}",
                f"mit der fiktiven {second}",
            )
            applied_markers += 2
        elif english_record:
            first = english_record.group("first")
            second = english_record.group("second")
            repaired = _replace_unique_document_surface(
                repaired,
                f"{first} records",
                f"Fictional {first} records",
            )
            repaired = _replace_unique_document_surface(
                repaired,
                f"together with {second}",
                f"together with fictional {second}",
            )
            applied_markers += 2
        elif person:
            name = person.group("person")
            verb = person.group("verb")
            repaired = _replace_unique_document_surface(
                repaired,
                f"Die Ansprechperson {name} {verb}",
                f"Die fiktive Ansprechperson {name} {verb}",
            )
            applied_markers += 1
        elif direct_german:
            entity = direct_german.group("entity")
            verb = direct_german.group("verb")
            repaired = _replace_unique_document_surface(
                repaired,
                f"Die {entity} {verb}",
                f"Die fiktive {entity} {verb}",
            )
            applied_markers += 1
        elif english_direct:
            entity = english_direct.group("entity")
            verb = english_direct.group("verb")
            repaired = _replace_unique_document_surface(
                repaired,
                f"{entity} {verb}",
                f"Fictional {entity} {verb}",
            )
            applied_markers += 1

        tax_organization = _TAX_ORGANIZATION_FICTION.search(text)
        if tax_organization:
            entity = tax_organization.group("entity")
            repaired = _replace_unique_document_surface(
                repaired,
                f"Die {entity} bezieht sich auf die Mitteilung",
                f"Die fiktive {entity} bezieht sich auf die Mitteilung",
            )
            applied_markers += 1
        general_condition = _GENERAL_CONDITION_FICTION.search(text)
        if general_condition:
            entity = general_condition.group("entity")
            repaired = _replace_unique_document_surface(
                repaired,
                f"wenn die {entity} die Unterlagen vollständig bestätigt",
                f"wenn die fiktive {entity} die Unterlagen vollständig bestätigt",
            )
            applied_markers += 1
        if "anhand der fiktiven Belegliste" in text:
            repaired = _replace_unique_document_surface(
                repaired,
                "anhand der Belegliste",
                "anhand der fiktiven Belegliste",
            )
            applied_markers += 1
        if "wenn die fiktive Freigabe in der Projektakte dokumentiert ist" in text:
            repaired = _replace_unique_document_surface(
                repaired,
                "wenn die Freigabe in der Projektakte dokumentiert ist",
                "wenn die fiktive Freigabe in der Projektakte dokumentiert ist",
            )
            applied_markers += 1
        if "wenn die fiktive Abnahme für " in text:
            repaired = _replace_unique_document_surface(
                repaired,
                "wenn die Abnahme für ",
                "wenn die fiktive Abnahme für ",
            )
            applied_markers += 1

    if applied_markers != expected_markers:
        raise ValueError(
            "fictionality repair did not classify every literal fact-text marker"
        )
    added_markers = (
        _document_fiction_marker_count(repaired)
        - _document_fiction_marker_count(document)
    )
    if added_markers != expected_markers:
        raise ValueError("fictionality repair did not restore every classified marker")
    return repaired


def localize_german_structure(
    plan: Mapping[str, Any],
    document: Document,
) -> tuple[dict[str, Any], Document]:
    """Localize the closed de-DE structural vocabulary while preserving values."""

    if plan.get("language") != "de-DE":
        raise ValueError("German structural localization requires language de-DE")
    repaired_plan = copy.deepcopy(dict(plan))
    semantic_plan = repaired_plan.get("semantic_plan")
    if not isinstance(semantic_plan, dict):
        raise ValueError("German structural localization requires semantic_plan")
    structure = semantic_plan.get("structure")
    if not isinstance(structure, dict):
        raise ValueError("German structural localization requires semantic_plan.structure")
    for field in ("paragraph_topics", "list_items", "signature_lines"):
        values = structure.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"German structural localization requires string {field}")
        structure[field] = [_localize_german_structural_text(value) for value in values]
    if any(
        _contains_unlocalized_german_structure(value)
        for field in ("paragraph_topics", "list_items", "signature_lines")
        for value in structure[field]
    ):
        raise ValueError("German plan structure contains an unlocalized reviewed surface")

    changed_surfaces = 0
    blocks: list[Block] = []
    for block in document.blocks:
        if block.type in {BlockType.SUBJECT, BlockType.HEADING_1, BlockType.HEADING_2}:
            updated = _localize_german_structural_text(block.text)
            if updated != block.text:
                if len(block.inlines) > 1:
                    raise ValueError(
                        "German structural localization cannot cross styled boundaries"
                    )
                inlines = (
                    (Inline(updated, block.inlines[0].style),)
                    if block.inlines
                    else ()
                )
                block = Block(type=block.type, text=updated, inlines=inlines)
                changed_surfaces += 1
        elif block.type in {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}:
            items: list[ListItem] = []
            for item in block.items:
                updated = _localize_german_structural_text(item.text)
                if updated != item.text:
                    if len(item.inlines) > 1:
                        raise ValueError(
                            "German list localization cannot cross styled boundaries"
                        )
                    inlines = (
                        (Inline(updated, item.inlines[0].style),)
                        if item.inlines
                        else ()
                    )
                    item = ListItem(level=item.level, text=updated, inlines=inlines)
                    changed_surfaces += 1
                items.append(item)
            block = Block(type=block.type, items=tuple(items))
        elif block.type is BlockType.SIGNATURE:
            lines = tuple(
                _localize_german_structural_text(line)
                for line in block.lines
            )
            changed_surfaces += sum(
                left != right for left, right in zip(block.lines, lines, strict=True)
            )
            block = Block(type=block.type, lines=lines)
        blocks.append(block)
    repaired_document = Document(blocks=tuple(blocks))
    residuals = [
        value
        for block in repaired_document.blocks
        for value in (
            [block.text]
            if block.text
            else [item.text for item in block.items]
            if block.items
            else list(block.lines)
        )
        if _contains_unlocalized_german_structure(value)
    ]
    if residuals or not changed_surfaces:
        raise ValueError("German target structure was not completely localized")
    return repaired_plan, repaired_document


__all__ = [
    "bind_feedback_list_to_obligation_due_date",
    "localize_german_structure",
    "mark_uncertain_list_date",
    "naturalize_technical_compound",
    "paragraphize_single_unchanged_item",
    "preserve_feedback_position_role",
    "promote_explicit_named_actors",
    "restore_lexical_fictionality",
]

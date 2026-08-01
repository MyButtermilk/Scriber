"""Deterministically expand one validated canonical target into local variants."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ast_codec import document_from_dict, document_to_dict
from .document_ast import Block, BlockType, Document, Inline, InlineStyle, ListItem
from .sst_renderer import render_sst
from .target_renderer import render_html, render_markdown, render_plain_text
from .validators import validate_canonical_record

VARIANT_COUNT = 7
EXPANDER_VERSION = "local_canonical_expander_v1"
CANONICAL_GENERATOR_ID = "local_canonical_expander"
CANONICAL_GENERATOR_VERSION = "1"

_PROJECT_ROOT = Path(__file__).parents[2]
_PLAN_SCHEMA_PATH = _PROJECT_ROOT / "contracts" / "seed_plan_schema.json"

# These intentionally fictional banks are local, typed, and never call a model or service.
_ENTITY_BANKS: dict[str, tuple[str, ...]] = {
    "person": (
        "Alina Morgen",
        "Benedikt Falk",
        "Clara Wendt",
        "David Kern",
        "Elena Voss",
        "Felix Dorn",
        "Greta Hain",
        "Hannes Ried",
        "Ida Stein",
        "Jonas Tal",
    ),
    "organization": (
        "Birkental Projekt GmbH",
        "Dämmwerk Koordination GmbH",
        "Eichenraum Service GmbH",
        "Feldlinie Planung GmbH",
        "Glasbogen Systeme GmbH",
        "Hafenquell Beratung GmbH",
        "Inselwerk Prozesse GmbH",
        "Jadepfad Technik GmbH",
        "Kornfeld Lösungen GmbH",
        "Lichttal Verwaltung GmbH",
    ),
    "location": (
        "Birkenau Mitte",
        "Dämmerhafen",
        "Eichenfeld",
        "Falkenbogen",
        "Glaswiese",
        "Hafenrain",
        "Inselgrund",
        "Jadehöhe",
        "Kornbrück",
        "Lichtau",
    ),
    "product": (
        "Projekt Birke",
        "Projekt Dämmerung",
        "Projekt Eiche",
        "Projekt Falkenflug",
        "Projekt Glasfaden",
        "Projekt Hafenlicht",
        "Projekt Inselpfad",
        "Projekt Jadebogen",
        "Projekt Kornfeld",
        "Projekt Lichtspur",
    ),
    "legal_term": (
        "Freigabeklausel Birke",
        "Prüfregel Dämmerung",
        "Dokumentationsregel Eiche",
        "Nachweisregel Falkenflug",
        "Abstimmungsklausel Glasfaden",
        "Prüfhinweis Hafenlicht",
        "Freigaberegel Inselpfad",
        "Dokumentationsklausel Jadebogen",
        "Nachweisregel Kornfeld",
        "Abstimmungsregel Lichtspur",
    ),
    "technical_term": (
        "Schnittstelle Birke",
        "Prüfmodul Dämmerung",
        "Datenpfad Eiche",
        "Statusmodul Falkenflug",
        "Freigabeadapter Glasfaden",
        "Prüfkanal Hafenlicht",
        "Prozessmodul Inselpfad",
        "Datenadapter Jadebogen",
        "Statuspfad Kornfeld",
        "Prüfmodul Lichtspur",
    ),
}

_NORM_PATTERN = re.compile(r"(?:§\s*\d|\bArt\.\s*\d|\bArtikel\s+\d|\bDIN\s+|\bISO\s+|\bEN\s+|\b[A-Z]{2,6}-\d)")
_COORDINATION_PATTERN = re.compile(
    r"(?P<subject>Die fiktive [^.]+?) stimmt sich mit (?P<counterpart>der fiktiven [^.]+?) zu "
    r"(?P<topic>[^.]+?) ab\."
)
_LOCATION_COORDINATION_PATTERN = re.compile(
    r"(?P<person>[^.]+?) koordiniert die Rückmeldung am Standort (?P<location>[^.]+?) "
    r"in der vereinbarten Reihenfolge\."
)


def _load_plan_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_PLAN_SCHEMA_PATH.read_text(encoding="utf-8")))


def _validate_plan(plan: Mapping[str, Any]) -> None:
    errors = sorted(_load_plan_validator().iter_errors(dict(plan)), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"seed plan is invalid: {errors[0].message}")


def _entity_substitutions(plan: Mapping[str, Any], variant_index: int) -> dict[str, dict[str, str]]:
    semantic_plan = plan["semantic_plan"]
    entities = semantic_plan["entities"]
    by_type: dict[str, list[str]] = {}
    for entity in entities:
        entity_type = entity["type"]
        value = entity["value"]
        if value not in by_type.setdefault(entity_type, []):
            by_type[entity_type].append(value)

    substitutions: dict[str, dict[str, str]] = {}
    for entity_type, values in by_type.items():
        substitutable_values = [value for value in values if not _NORM_PATTERN.search(value)]
        if not substitutable_values:
            continue
        bank = _ENTITY_BANKS[entity_type]
        seed_material = f"{plan['seed_id']}:{plan['seed']}:{entity_type}".encode()
        offset = (int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big") + variant_index - 1) % len(bank)
        if len(substitutable_values) > len(bank):
            raise ValueError(f"too many {entity_type} entities for the local entity bank")
        replacements: dict[str, str] = {}
        used: set[str] = set()
        for position, value in enumerate(substitutable_values):
            replacement = bank[(offset + position) % len(bank)]
            while replacement in used or replacement in values:
                position += 1
                replacement = bank[(offset + position) % len(bank)]
            used.add(replacement)
            replacements[value] = replacement
        substitutions[entity_type] = replacements
    return substitutions


def _substitute_text(text: str, substitutions: Mapping[str, Mapping[str, str]]) -> str:
    flat = {original: replacement for typed in substitutions.values() for original, replacement in typed.items()}
    for original in sorted(flat, key=len, reverse=True):
        text = text.replace(original, flat[original])
    return text


def _phrase_variant(text: str, variant_index: int) -> str:
    mode = (variant_index - 1) % 3
    if mode == 0:
        return text

    def coordination(match: re.Match[str]) -> str:
        subject = match["subject"]
        counterpart = match["counterpart"]
        topic = match["topic"]
        if mode == 1:
            return f"{subject} stimmt die Abstimmung zu {topic} mit {counterpart} ab."
        return f"{subject} nimmt mit {counterpart} die Abstimmung zu {topic} vor."

    def location(match: re.Match[str]) -> str:
        if mode == 1:
            return (
                f"{match['person']} koordiniert die Rückmeldung in der vereinbarten Reihenfolge "
                f"am Standort {match['location']}."
            )
        return (
            f"{match['person']} übernimmt die Koordination der Rückmeldung am Standort "
            f"{match['location']} in der vereinbarten Reihenfolge."
        )

    text = _COORDINATION_PATTERN.sub(coordination, text)
    return _LOCATION_COORDINATION_PATTERN.sub(location, text)


def _transform_semantic_value(
    value: Any,
    substitutions: Mapping[str, Mapping[str, str]],
    variant_index: int,
) -> Any:
    if isinstance(value, str):
        return _phrase_variant(_substitute_text(value, substitutions), variant_index)
    if isinstance(value, list):
        return [_transform_semantic_value(item, substitutions, variant_index) for item in value]
    if isinstance(value, dict):
        return {key: _transform_semantic_value(item, substitutions, variant_index) for key, item in value.items()}
    return copy.deepcopy(value)


def _semantic_plan_hash(semantic_plan: Mapping[str, Any]) -> str:
    payload = json.dumps(
        semantic_plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _replace_inlines(
    inlines: tuple[Inline, ...], substitutions: Mapping[str, Mapping[str, str]], variant_index: int
) -> tuple[Inline, ...]:
    return tuple(
        Inline(_phrase_variant(_substitute_text(part.text, substitutions), variant_index), part.style)
        for part in inlines
    )


def _transform_document(
    document: Document, substitutions: Mapping[str, Mapping[str, str]], variant_index: int
) -> Document:
    blocks: list[Block] = []
    for block in document.blocks:
        if block.items:
            items = tuple(
                ListItem(
                    item.level,
                    _phrase_variant(_substitute_text(item.text, substitutions), variant_index),
                    _replace_inlines(item.inlines, substitutions, variant_index),
                )
                for item in block.items
            )
            blocks.append(Block(block.type, items=items))
            continue
        if block.lines:
            blocks.append(
                Block(
                    block.type,
                    lines=tuple(
                        _phrase_variant(_substitute_text(line, substitutions), variant_index) for line in block.lines
                    ),
                )
            )
            continue
        text = _phrase_variant(_substitute_text(block.text, substitutions), variant_index)
        inlines = _replace_inlines(block.inlines, substitutions, variant_index)
        blocks.append(Block(block.type, text=text, inlines=inlines))
    return Document(blocks=tuple(blocks))


def _marked_inlines(text: str, inlines: tuple[Inline, ...], position: int, marker: InlineStyle) -> tuple[Inline, ...]:
    source = inlines or (Inline(text),)
    rendered: list[Inline] = []
    cursor = 0
    for part in source:
        local_position = position - cursor
        if not 0 <= local_position < len(part.text):
            rendered.append(part)
            cursor += len(part.text)
            continue
        if local_position:
            rendered.append(Inline(part.text[:local_position], part.style))
        rendered.append(Inline(part.text[local_position], marker))
        if local_position + 1 < len(part.text):
            rendered.append(Inline(part.text[local_position + 1 :], part.style))
        cursor += len(part.text)
    return tuple(rendered)


def _with_format_marker(
    document: Document, block_index: int, item_index: int | None, position: int, marker: InlineStyle
) -> Document:
    blocks = list(document.blocks)
    block = blocks[block_index]
    if item_index is None:
        blocks[block_index] = Block(
            block.type,
            text=block.text,
            inlines=_marked_inlines(block.text, block.inlines, position, marker),
        )
    else:
        items = list(block.items)
        item = items[item_index]
        items[item_index] = ListItem(
            item.level,
            item.text,
            _marked_inlines(item.text, item.inlines, position, marker),
        )
        blocks[block_index] = Block(block.type, items=tuple(items))
    return Document(blocks=tuple(blocks))


def _format_only_variant(document: Document, *, ordinal: int, excluded_sst: frozenset[str] = frozenset()) -> Document:
    """Make an AST-only distinction when a valid seed intentionally has no entities."""
    options: list[tuple[int, int | None, int]] = []
    for block_index, block in enumerate(document.blocks):
        if block.items:
            for item_index, item in enumerate(block.items):
                options.extend((block_index, item_index, position) for position in range(len(item.text)))
        elif block.text:
            options.extend((block_index, None, position) for position in range(len(block.text)))

    seen = {render_sst(document), *excluded_sst}
    available = 0
    for marker in (InlineStyle.BOLD, InlineStyle.UNDERLINE, InlineStyle.BOLD_UNDERLINE):
        for block_index, item_index, position in options:
            candidate = _with_format_marker(document, block_index, item_index, position, marker)
            rendered = render_sst(candidate)
            if rendered not in seen:
                seen.add(rendered)
                available += 1
                if available == ordinal:
                    return candidate
    raise ValueError("baseline target cannot yield seven AST-only variants")


def _protected_spans(
    baseline: Sequence[object], substitutions: Mapping[str, Mapping[str, str]], plan: Mapping[str, Any]
) -> list[str]:
    spans = [_substitute_text(str(span), substitutions) for span in baseline]
    for fact in plan["semantic_plan"]["facts"]:
        spans.extend(_substitute_text(value, substitutions) for value in fact["protected_values"])
    return list(dict.fromkeys(spans))


def _assert_norms_are_unchanged(plan: Mapping[str, Any], substitutions: Mapping[str, Mapping[str, str]]) -> None:
    for typed in substitutions.values():
        for original, replacement in typed.items():
            if _NORM_PATTERN.search(original) and original != replacement:
                raise ValueError(f"normative entity must not be substituted: {original!r}")


def _assert_structure_allowed(plan: Mapping[str, Any], document: Document) -> None:
    structure = plan["semantic_plan"]["structure"]
    blocks_by_type: dict[BlockType, list[Block]] = {}
    for block in document.blocks:
        blocks_by_type.setdefault(block.type, []).append(block)

    switches = {
        BlockType.SUBJECT: ("has_subject", "subject"),
        BlockType.SALUTATION: ("has_salutation", "salutation"),
        BlockType.QUOTE: ("has_quote", "quote"),
        BlockType.ATTACHMENTS: ("has_attachments", "attachments"),
        BlockType.POST_SCRIPT: ("has_post_script", "post script"),
    }
    for block_type, (plan_key, label) in switches.items():
        if bool(blocks_by_type.get(block_type)) != bool(structure.get(plan_key, False)):
            raise ValueError(f"baseline target {label} structure is not allowed by the seed plan")

    heading_levels = [
        "h1" if block.type is BlockType.HEADING_1 else "h2"
        for block in document.blocks
        if block.type in (BlockType.HEADING_1, BlockType.HEADING_2)
    ]
    declared_heading_levels = list(dict.fromkeys(structure["heading_levels"]))
    observed_heading_levels = list(dict.fromkeys(heading_levels))
    if observed_heading_levels != declared_heading_levels:
        raise ValueError("baseline target heading structure is not allowed by the seed plan")

    list_blocks = [
        block for block in document.blocks if block.type in (BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST)
    ]
    list_kind = structure["list_kind"]
    if list_kind == "none" and list_blocks:
        raise ValueError("baseline target list structure is not allowed by the seed plan")
    if list_kind != "none" and len(list_blocks) != 1:
        raise ValueError("baseline target list structure is not allowed by the seed plan")
    if list_blocks:
        list_block = list_blocks[0]
        if list_kind == "ordered" and list_block.type is not BlockType.ORDERED_LIST:
            raise ValueError("baseline target ordered list structure is not allowed by the seed plan")
        if list_kind == "unordered" and list_block.type is not BlockType.UNORDERED_LIST:
            raise ValueError("baseline target unordered list structure is not allowed by the seed plan")
        if list_kind == "nested" and not any(item.level > 1 for item in list_block.items):
            raise ValueError("baseline target nested list structure is not allowed by the seed plan")
        if len(list_block.items) != len(structure["list_items"]):
            raise ValueError("baseline target list item count differs from the seed plan")

    signatures = blocks_by_type.get(BlockType.SIGNATURE, [])
    signature_lines = structure["signature_lines"]
    if bool(signatures) != bool(signature_lines):
        raise ValueError("baseline target signature structure is not allowed by the seed plan")
    if signatures and list(signatures[0].lines) != signature_lines:
        raise ValueError("baseline target signature lines differ from the seed plan")
    if bool(blocks_by_type.get(BlockType.CLOSING)) != bool(structure["has_closing"]):
        raise ValueError("baseline target closing structure is not allowed by the seed plan")


def _critic_approval_fields(approval: Mapping[str, Any] | None) -> dict[str, Any]:
    if approval is None:
        return {}
    package_sha256 = approval.get("package_sha256")
    critic_ids = approval.get("critic_ids")
    if not isinstance(package_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", package_sha256):
        raise ValueError("critic approval requires a plain SHA-256 package hash")
    if not isinstance(critic_ids, Mapping) or not critic_ids:
        raise ValueError("critic approval requires critic_ids")
    roles = [str(role) for role in critic_ids.values()]
    if roles.count("fact") < 1 or roles.count("style") < 1 or roles.count("adversarial") < 2:
        raise ValueError("critic approval requires fact, style, and two adversarial critics")
    decisions = [
        {"critic_id": str(critic_id), "critic_role": str(role), "acceptable": True}
        for critic_id, role in sorted(critic_ids.items())
    ]
    return {
        "reviewed_package_sha256": package_sha256,
        "critic_agents": [decision["critic_id"] for decision in decisions],
        "critic_decisions": decisions,
    }


def expand_canonical_variants(
    plan: Mapping[str, Any],
    baseline_target: Mapping[str, Any],
    *,
    critic_approval: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the seven deterministic, local variants for a seed and its base target."""
    _validate_plan(plan)
    if baseline_target.get("seed_id") != plan["seed_id"]:
        raise ValueError("baseline target seed_id does not match seed plan")
    validate_canonical_record(baseline_target)
    document = document_from_dict(baseline_target["target_ast"])
    _assert_structure_allowed(plan, document)
    parent_id = baseline_target.get("canonical_document_id")
    if not isinstance(parent_id, str) or not parent_id:
        raise ValueError("baseline target has no canonical_document_id")
    approval_fields = _critic_approval_fields(critic_approval)

    variants: list[dict[str, Any]] = []
    rendered_sst: set[str] = set()
    for variant_index in range(1, VARIANT_COUNT + 1):
        substitutions = _entity_substitutions(plan, variant_index)
        _assert_norms_are_unchanged(plan, substitutions)
        expanded_document = _transform_document(document, substitutions, variant_index)
        if not substitutions:
            expanded_document = _format_only_variant(expanded_document, ordinal=variant_index)
        elif render_sst(expanded_document) in rendered_sst:
            expanded_document = _format_only_variant(expanded_document, ordinal=1, excluded_sst=frozenset(rendered_sst))
        record = copy.deepcopy(dict(baseline_target))
        semantic_plan = _transform_semantic_value(plan["semantic_plan"], substitutions, variant_index)
        record["canonical_document_id"] = f"{parent_id}__variant_{variant_index:02d}"
        record["parent_canonical_document_id"] = parent_id
        record["canonical_generator_id"] = CANONICAL_GENERATOR_ID
        record["canonical_generator_version"] = CANONICAL_GENERATOR_VERSION
        record["variant_index"] = variant_index
        record["semantic_plan"] = semantic_plan
        record["semantic_plan_sha256"] = _semantic_plan_hash(semantic_plan)
        record["domain"] = plan["domain"]
        record["document_type"] = plan["document_type"]
        record["risk_tags"] = copy.deepcopy(plan["risk_tags"])
        record.update(copy.deepcopy(approval_fields))
        record["target_ast"] = document_to_dict(expanded_document)
        record["target_sst"] = render_sst(expanded_document)
        record["target_plain_text"] = render_plain_text(expanded_document)
        record["target_markdown"] = render_markdown(expanded_document)
        record["target_html"] = render_html(expanded_document)
        record["source_text"] = _phrase_variant(
            _substitute_text(str(baseline_target["source_text"]), substitutions), variant_index
        )
        record["protected_spans"] = _protected_spans(baseline_target["protected_spans"], substitutions, plan)
        validate_canonical_record(record)
        rendered_sst.add(record["target_sst"])
        variants.append(record)
    return variants


def _read_jsonl(root: Path, file_name: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob(file_name)):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL in {path}:{line_number}") from error
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record in {path}:{line_number} must be an object")
            records.append(value)
    if not records:
        raise ValueError(f"no {file_name} records found below {root}")
    return records


def expand_roots(
    seed_root: str | Path | Sequence[str | Path],
    target_root: str | Path | Sequence[str | Path],
    output: str | Path,
    manifest: str | Path,
    *,
    minimum_count: int = VARIANT_COUNT,
    approval: str | Path | None = None,
) -> dict[str, Any]:
    """Join explicit plan/target roots by seed_id and write checked local JSONL."""
    seed_paths = (
        (Path(seed_root),) if isinstance(seed_root, str | Path) else tuple(Path(path) for path in seed_root)
    )
    if not seed_paths:
        raise ValueError("at least one seed_root is required")
    target_paths = (
        (Path(target_root),) if isinstance(target_root, str | Path) else tuple(Path(path) for path in target_root)
    )
    if not target_paths:
        raise ValueError("at least one target_root is required")
    plans = [plan for seed_path in seed_paths for plan in _read_jsonl(seed_path, "plans.jsonl")]
    targets = [target for target_path in target_paths for target in _read_jsonl(target_path, "targets.jsonl")]
    approval_record: dict[str, Any] | None = None
    if approval is not None:
        loaded_approval = json.loads(Path(approval).read_text(encoding="utf-8"))
        if not isinstance(loaded_approval, dict):
            raise ValueError("critic approval must be an object")
        accepted_seed_ids = loaded_approval.get("accepted_seed_ids")
        if not isinstance(accepted_seed_ids, list) or not all(
            isinstance(seed_id, str) and seed_id for seed_id in accepted_seed_ids
        ):
            raise ValueError("critic approval requires accepted_seed_ids")
        if len(accepted_seed_ids) != len(set(accepted_seed_ids)):
            raise ValueError("critic approval contains duplicate accepted_seed_ids")
        accepted = set(accepted_seed_ids)
        available_target_ids = {target.get("seed_id") for target in targets}
        missing = sorted(accepted.difference(available_target_ids))
        if missing:
            raise ValueError(f"accepted critic seed has no target: {missing[0]}")
        targets = [target for target in targets if target.get("seed_id") in accepted]
        approval_record = loaded_approval
    plans_by_seed = {record.get("seed_id"): record for record in plans}
    if len(plans_by_seed) != len(plans):
        raise ValueError("seed_root contains duplicate seed_id values")
    target_seed_ids = [record.get("seed_id") for record in targets]
    if len(set(target_seed_ids)) != len(target_seed_ids):
        raise ValueError("target_root contains duplicate seed_id values")

    expanded: list[dict[str, Any]] = []
    for target in targets:
        seed_id = target.get("seed_id")
        plan = plans_by_seed.get(seed_id)
        if plan is None:
            raise ValueError(f"target has no matching seed plan: {seed_id!r}")
        expanded.extend(
            expand_canonical_variants(
                plan,
                target,
                critic_approval=approval_record,
            )
        )
    if len(expanded) < minimum_count:
        raise ValueError(f"expanded output has {len(expanded)} records; expected at least {minimum_count}")

    output_path = Path(output)
    manifest_path = Path(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in expanded
    )
    output_path.write_text(payload, encoding="utf-8", newline="\n")
    result = {
        "schema_version": 1,
        "expander": EXPANDER_VERSION,
        "variant_count_per_seed": VARIANT_COUNT,
        "record_count": len(expanded),
        "sha256": "sha256:" + hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "critic_approved": approval_record is not None,
    }
    if approval_record is not None:
        result["reviewed_package_sha256"] = approval_record["package_sha256"]
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return result

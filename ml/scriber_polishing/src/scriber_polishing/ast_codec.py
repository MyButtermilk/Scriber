"""Closed JSON codec for the canonical Scriber document AST."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .document_ast import Block, BlockType, Document, Inline, InlineStyle, ListItem

_DOCUMENT_FIELDS = frozenset({"schema_version", "blocks"})
_BLOCK_FIELDS = frozenset({"type", "text", "inlines", "items", "lines"})
_INLINE_FIELDS = frozenset({"text", "style"})
_ITEM_FIELDS = frozenset({"level", "text", "inlines"})


def _closed_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    unexpected = set(value).difference(fields)
    if unexpected:
        raise ValueError(f"{label} has unexpected field: {sorted(unexpected)[0]}")
    missing = fields.difference(value)
    if missing:
        raise ValueError(f"{label} is missing field: {sorted(missing)[0]}")
    return value


def _list(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be {'a string' if allow_empty else 'a non-empty string'}")
    return value


def _inline_from_dict(value: object) -> Inline:
    record = _closed_mapping(value, _INLINE_FIELDS, "inline")
    try:
        style = InlineStyle(_text(record["style"], "inline style"))
    except ValueError as error:
        raise ValueError("unknown inline style") from error
    return Inline(text=_text(record["text"], "inline text"), style=style)


def _item_from_dict(value: object) -> ListItem:
    record = _closed_mapping(value, _ITEM_FIELDS, "list item")
    level = record["level"]
    if isinstance(level, bool) or not isinstance(level, int):
        raise ValueError("list item level must be an integer")
    return ListItem(
        level=level,
        text=_text(record["text"], "list item text"),
        inlines=tuple(_inline_from_dict(item) for item in _list(record["inlines"], "list item inlines")),
    )


def _block_from_dict(value: object) -> Block:
    record = _closed_mapping(value, _BLOCK_FIELDS, "block")
    try:
        block_type = BlockType(_text(record["type"], "block type"))
    except ValueError as error:
        raise ValueError("unknown block type") from error
    return Block(
        type=block_type,
        text=_text(record["text"], "block text", allow_empty=True),
        inlines=tuple(_inline_from_dict(item) for item in _list(record["inlines"], "block inlines")),
        items=tuple(_item_from_dict(item) for item in _list(record["items"], "block items")),
        lines=tuple(_text(item, "signature line") for item in _list(record["lines"], "block lines")),
    )


def document_from_dict(value: object) -> Document:
    """Decode a versioned AST record while rejecting unknown fields and enum values."""

    record = _closed_mapping(value, _DOCUMENT_FIELDS, "document")
    if record["schema_version"] != 1:
        raise ValueError("unsupported document schema_version")
    return Document(blocks=tuple(_block_from_dict(item) for item in _list(record["blocks"], "document blocks")))


def _inline_to_dict(value: Inline) -> dict[str, object]:
    return {"text": value.text, "style": value.style.value}


def _item_to_dict(value: ListItem) -> dict[str, object]:
    return {
        "level": value.level,
        "text": value.text,
        "inlines": [_inline_to_dict(item) for item in value.inlines],
    }


def _block_to_dict(value: Block) -> dict[str, object]:
    return {
        "type": value.type.value,
        "text": value.text,
        "inlines": [_inline_to_dict(item) for item in value.inlines],
        "items": [_item_to_dict(item) for item in value.items],
        "lines": list(value.lines),
    }


def document_to_dict(document: Document) -> dict[str, object]:
    """Encode a canonical document into the stable AST v1 JSON shape."""

    return {"schema_version": 1, "blocks": [_block_to_dict(block) for block in document.blocks]}

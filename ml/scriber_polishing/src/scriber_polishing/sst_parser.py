"""Strict parser for the safe, closed SST v1 language."""

from __future__ import annotations

import re

from .document_ast import Block, BlockType, Document, Inline, InlineStyle, ListItem


class SSTParseError(ValueError):
    """Raised when input is not a well-formed SST v1 document."""


_TEXT_TAGS = {
    "SUBJECT": BlockType.SUBJECT,
    "H1": BlockType.HEADING_1,
    "H2": BlockType.HEADING_2,
    "SALUTATION": BlockType.SALUTATION,
    "DATE_LINE": BlockType.DATE_LINE,
    "P": BlockType.PARAGRAPH,
    "QUOTE": BlockType.QUOTE,
    "CLOSING": BlockType.CLOSING,
    "ATTACHMENTS": BlockType.ATTACHMENTS,
    "PS": BlockType.POST_SCRIPT,
}
_TOKEN = re.compile(r"\[(/?)([A-Z0-9_]+)\]")
_INLINE = {"B": InlineStyle.BOLD, "U": InlineStyle.UNDERLINE, "BU": InlineStyle.BOLD_UNDERLINE}


def _parse_inline(value: str) -> tuple[str, tuple[Inline, ...]]:
    pieces: list[Inline] = []
    position = 0
    style = InlineStyle.PLAIN
    open_tag: str | None = None
    for match in _TOKEN.finditer(value):
        if match.start() > position:
            pieces.append(Inline(value[position : match.start()], style))
        closing, tag = match.groups()
        if tag == "BR" and not closing and open_tag is None:
            pieces.append(Inline("\n", style))
        elif tag in _INLINE:
            if closing:
                if open_tag != tag:
                    raise SSTParseError(f"mismatched closing inline tag: {tag}")
                open_tag, style = None, InlineStyle.PLAIN
            elif open_tag is None:
                open_tag, style = tag, _INLINE[tag]
            else:
                raise SSTParseError("nested inline tags are not allowed")
        else:
            raise SSTParseError(f"unknown or misplaced tag: {tag}")
        position = match.end()
    if position < len(value):
        pieces.append(Inline(value[position:], style))
    if open_tag is not None:
        raise SSTParseError(f"unclosed inline tag: {open_tag}")
    text = "".join(piece.text for piece in pieces)
    if not text:
        raise SSTParseError("text content must not be empty")
    return text, tuple(pieces)


def _expect_line(lines: list[str], index: int, expected: str) -> int:
    if index >= len(lines) or lines[index] != expected:
        raise SSTParseError(f"expected {expected}")
    return index + 1


def parse_sst(text: str) -> Document:
    """Parse one complete SST document; unknown tags and malformed nesting fail closed."""
    if not text or not text.startswith("[DOC]\n") or not text.endswith("\n[/DOC]"):
        raise SSTParseError("document must be enclosed by [DOC] and [/DOC]")
    lines = text.split("\n")
    index = _expect_line(lines, 0, "[DOC]")
    blocks: list[Block] = []
    while index < len(lines) - 1:
        line = lines[index]
        if line in ("[OL]", "[UL]"):
            block_type = BlockType.ORDERED_LIST if line == "[OL]" else BlockType.UNORDERED_LIST
            index += 1
            items: list[ListItem] = []
            while index < len(lines) and lines[index] != ("[/OL]" if block_type is BlockType.ORDERED_LIST else "[/UL]"):
                item = re.fullmatch(r"\[LI([123])\](.*)\[/LI\1\]", lines[index])
                if not item:
                    raise SSTParseError("lists may contain only correctly closed LI1, LI2, or LI3 items")
                item_text, inlines = _parse_inline(item.group(2))
                items.append(ListItem(level=int(item.group(1)), text=item_text, inlines=inlines))
                index += 1
            index = _expect_line(lines, index, "[/OL]" if block_type is BlockType.ORDERED_LIST else "[/UL]")
            blocks.append(Block(type=block_type, items=tuple(items)))
            continue
        if line == "[SIGNATURE]":
            index += 1
            signature_lines: list[str] = []
            while index < len(lines) and lines[index] != "[/SIGNATURE]":
                match = re.fullmatch(r"\[LINE\](.*)\[/LINE\]", lines[index])
                if not match or "[" in match.group(1):
                    raise SSTParseError("signature may contain only non-empty LINE elements")
                signature_lines.append(match.group(1))
                index += 1
            index = _expect_line(lines, index, "[/SIGNATURE]")
            blocks.append(Block(type=BlockType.SIGNATURE, lines=tuple(signature_lines)))
            continue
        match = re.match(r"^\[([A-Z0-9_]+)\](.*)$", line)
        if not match or match.group(1) not in _TEXT_TAGS:
            raise SSTParseError("unknown or misplaced SST block")
        tag, value = match.groups()
        closing = f"[/{tag}]"
        index += 1
        content = [value]
        while not content[-1].endswith(closing):
            if index >= len(lines) - 1:
                raise SSTParseError(f"unclosed block: {tag}")
            content.append(lines[index])
            index += 1
        content[-1] = content[-1][: -len(closing)]
        raw = "\n".join(content)
        block_text, inlines = _parse_inline(raw)
        blocks.append(Block(type=_TEXT_TAGS[tag], text=block_text, inlines=inlines))
    _expect_line(lines, index, "[/DOC]")
    return Document(blocks=tuple(blocks))

"""Canonical SST v1 serializer."""

from __future__ import annotations

from .document_ast import Block, BlockType, Document, Inline, InlineStyle

_TAGS = {
    BlockType.SUBJECT: "SUBJECT",
    BlockType.HEADING_1: "H1",
    BlockType.HEADING_2: "H2",
    BlockType.SALUTATION: "SALUTATION",
    BlockType.DATE_LINE: "DATE_LINE",
    BlockType.PARAGRAPH: "P",
    BlockType.QUOTE: "QUOTE",
    BlockType.CLOSING: "CLOSING",
    BlockType.ATTACHMENTS: "ATTACHMENTS",
    BlockType.POST_SCRIPT: "PS",
}
_STYLE_TAGS = {InlineStyle.BOLD: "B", InlineStyle.UNDERLINE: "U", InlineStyle.BOLD_UNDERLINE: "BU"}


def _inline_text(text: str, inlines: tuple[Inline, ...]) -> str:
    source = inlines or (Inline(text),)
    rendered: list[str] = []
    for part in source:
        value = part.text
        if part.style is not InlineStyle.PLAIN:
            tag = _STYLE_TAGS[part.style]
            value = f"[{tag}]{value}[/{tag}]"
        rendered.append(value)
    return "".join(rendered)


def _block(block: Block) -> list[str]:
    if block.type in (BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST):
        tag = "OL" if block.type is BlockType.ORDERED_LIST else "UL"
        return [
            f"[{tag}]",
            *[f"[LI{item.level}]{_inline_text(item.text, item.inlines)}[/LI{item.level}]" for item in block.items],
            f"[/{tag}]",
        ]
    if block.type is BlockType.SIGNATURE:
        return ["[SIGNATURE]", *[f"[LINE]{line}[/LINE]" for line in block.lines], "[/SIGNATURE]"]
    tag = _TAGS[block.type]
    return [f"[{tag}]{_inline_text(block.text, block.inlines)}[/{tag}]"]


def render_sst(document: Document) -> str:
    return "\n".join(["[DOC]", *[line for block in document.blocks for line in _block(block)], "[/DOC]"])

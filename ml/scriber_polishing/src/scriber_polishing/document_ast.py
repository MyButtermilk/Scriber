"""Canonical, deliberately small document representation for SST v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockType(StrEnum):
    SUBJECT = "subject"
    HEADING_1 = "heading_1"
    HEADING_2 = "heading_2"
    SALUTATION = "salutation"
    DATE_LINE = "date_line"
    PARAGRAPH = "paragraph"
    QUOTE = "quote"
    ORDERED_LIST = "ordered_list"
    UNORDERED_LIST = "unordered_list"
    CLOSING = "closing"
    SIGNATURE = "signature"
    ATTACHMENTS = "attachments"
    POST_SCRIPT = "post_script"


class InlineStyle(StrEnum):
    PLAIN = "plain"
    BOLD = "bold"
    UNDERLINE = "underline"
    BOLD_UNDERLINE = "bold_underline"


@dataclass(frozen=True, slots=True)
class Inline:
    text: str
    style: InlineStyle = InlineStyle.PLAIN

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("inline text must not be empty")


@dataclass(frozen=True, slots=True)
class ListItem:
    level: int
    text: str
    inlines: tuple[Inline, ...] = ()

    def __post_init__(self) -> None:
        if self.level not in (1, 2, 3):
            raise ValueError("list item level must be between 1 and 3")
        if not self.text:
            raise ValueError("list item text must not be empty")


@dataclass(frozen=True, slots=True)
class Block:
    type: BlockType
    text: str = ""
    inlines: tuple[Inline, ...] = ()
    items: tuple[ListItem, ...] = ()
    lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        list_types = {BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST}
        if self.type in list_types:
            if not self.items or self.text or self.lines:
                raise ValueError("list blocks require items and no text or signature lines")
            if self.items[0].level != 1:
                raise ValueError("a list must start at level 1")
            if any(
                current.level > previous.level + 1
                for previous, current in zip(self.items, self.items[1:], strict=False)
            ):
                raise ValueError("list nesting may increase by only one level at a time")
        elif self.type is BlockType.SIGNATURE:
            if not self.lines or self.text or self.items:
                raise ValueError("signature blocks require lines and no text or list items")
            if any(not line for line in self.lines):
                raise ValueError("signature lines must not be empty")
        elif not self.text or self.items or self.lines:
            raise ValueError("text blocks require text and no list items or signature lines")
        if self.inlines and "".join(part.text for part in self.inlines) != self.text:
            raise ValueError("inline text must exactly reconstruct block text")


@dataclass(frozen=True, slots=True)
class Document:
    blocks: tuple[Block, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.blocks:
            raise ValueError("document must contain at least one block")

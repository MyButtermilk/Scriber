"""Deterministic, safe projections of the canonical document AST."""

from __future__ import annotations

import re
from html import escape

from .document_ast import Block, BlockType, Document, Inline, InlineStyle

_RICH_QUANTITY_SPACE = re.compile(
    r"(?P<number>(?<![\w.])[-+]?\d[\d.]*(?:,\d+)?) "
    r"(?P<unit>kWh/\(m²·a\)|l/\(m²·a\)|€(?:/[A-Za-z0-9²³·()]+)?|"
    r"Mbit/s|m³/h|km/h|dB\(A\)|m/s|GiB|MiB|MWh|kWh|kWp|vCPU|"
    r"MW|kW|Wh|mm|cm|km|kg|m²|m³|°C|EUR|USD|CHF|Uhr|TB|"
    r"min|ms|h|s|W|l|m|%)(?=$|[\s.,;:!?…)\]}<])"
)
_RICH_LEGAL_SPACE = re.compile(
    r"(?P<label>(?<!\w)(?:§§?|Artikel|Art\.|Absatz|Abs\.|Satz|Nummer|Nr\.|Buchstabe)) "
    r"(?P<value>\d+[a-z]?)(?=$|[\s.,;:!?…)\]}<])"
)
_RICH_PREFIX_CURRENCY_SPACE = re.compile(
    r"(?P<unit>(?<!\w)(?:EUR|USD|CHF|\$|£|€)) "
    r"(?P<number>[-+]?\d[\d.]*(?:,\d+)?)(?=$|[\s.,;:!?…)\]}<])"
)


def _parts(text: str, inlines: tuple[Inline, ...]) -> tuple[Inline, ...]:
    return inlines or (Inline(text),)


def _plain(text: str, inlines: tuple[Inline, ...]) -> str:
    return "".join(part.text for part in _parts(text, inlines))


def _markdown(text: str, inlines: tuple[Inline, ...]) -> str:
    result: list[str] = []
    for part in _parts(text, inlines):
        value = part.text
        if part.style is InlineStyle.BOLD:
            value = f"**{value}**"
        elif part.style is InlineStyle.UNDERLINE:
            value = f"__{value}__"
        elif part.style is InlineStyle.BOLD_UNDERLINE:
            value = f"__**{value}**__"
        result.append(value)
    return "".join(result)


def _escaped_rich_text(text: str) -> str:
    """Escape model text and apply the contract's required rich-text spacing."""

    value = escape(text, quote=False).replace("\n", "<br>")
    value = _RICH_QUANTITY_SPACE.sub(r"\g<number>&nbsp;\g<unit>", value)
    value = _RICH_LEGAL_SPACE.sub(r"\g<label>&nbsp;\g<value>", value)
    return _RICH_PREFIX_CURRENCY_SPACE.sub(r"\g<unit>&nbsp;\g<number>", value)


def _html(text: str, inlines: tuple[Inline, ...]) -> str:
    result: list[str] = []
    for part in _parts(text, inlines):
        value = _escaped_rich_text(part.text)
        if part.style is InlineStyle.BOLD:
            value = f"<strong>{value}</strong>"
        elif part.style is InlineStyle.UNDERLINE:
            value = f"<u>{value}</u>"
        elif part.style is InlineStyle.BOLD_UNDERLINE:
            value = f"<strong><u>{value}</u></strong>"
        result.append(value)
    return "".join(result)


def _list_lines(block: Block, content) -> list[str]:
    marker = "1." if block.type is BlockType.ORDERED_LIST else "-"
    return [f"{'  ' * (item.level - 1)}{marker} {content(item.text, item.inlines)}" for item in block.items]


def _html_list(block: Block) -> str:
    tag = "ol" if block.type is BlockType.ORDERED_LIST else "ul"
    roots: list[list] = []
    stack: list[list] = []
    for item in block.items:
        node: list = [_html(item.text, item.inlines), []]
        while len(stack) >= item.level:
            stack.pop()
        if item.level == 1:
            roots.append(node)
        else:
            stack[-1][1].append(node)
        stack.append(node)

    def render_nodes(nodes: list[list]) -> str:
        return (
            f"<{tag}>"
            + "".join(f"<li>{text}{render_nodes(children) if children else ''}</li>" for text, children in nodes)
            + f"</{tag}>"
        )

    return render_nodes(roots)


def render_plain_text(document: Document) -> str:
    """Render text suitable for applications that accept no rich content."""
    sections: list[str] = []
    for block in document.blocks:
        if block.type in (BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST):
            sections.append("\n".join(_list_lines(block, _plain)))
        elif block.type is BlockType.SIGNATURE:
            sections.append("\n".join(block.lines))
        else:
            sections.append(_plain(block.text, block.inlines))
    return "\n\n".join(sections)


def render_markdown(document: Document) -> str:
    """Render GitHub-compatible Markdown without accepting source Markdown."""
    sections: list[str] = []
    for block in document.blocks:
        if block.type in (BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST):
            sections.append("\n".join(_list_lines(block, _markdown)))
        elif block.type is BlockType.SIGNATURE:
            sections.append("\n".join(block.lines))
        else:
            value = _markdown(block.text, block.inlines)
            if block.type in (BlockType.SUBJECT, BlockType.HEADING_1):
                value = f"__**{value}**__"
            elif block.type is BlockType.HEADING_2:
                value = f"**{value}**"
            elif block.type is BlockType.QUOTE:
                value = "\n".join(f"> {line}" for line in value.split("\n"))
            sections.append(value)
    return "\n\n".join(sections)


def render_html(document: Document) -> str:
    """Render a whitelist-only HTML fragment; all model text is escaped."""
    output: list[str] = []
    for block in document.blocks:
        if block.type in (BlockType.ORDERED_LIST, BlockType.UNORDERED_LIST):
            output.append(_html_list(block))
        elif block.type is BlockType.SIGNATURE:
            output.append("<p>" + "<br>".join(_escaped_rich_text(line) for line in block.lines) + "</p>")
        else:
            value = _html(block.text, block.inlines)
            if block.type in (BlockType.SUBJECT, BlockType.HEADING_1):
                value = f"<strong><u>{value}</u></strong>"
            elif block.type is BlockType.HEADING_2:
                value = f"<strong>{value}</strong>"
            tag = "blockquote" if block.type is BlockType.QUOTE else "p"
            output.append(f"<{tag}>{value}</{tag}>")
    return "".join(output)


def render_html_clipboard(document: Document) -> str:
    """Wrap safe HTML in the byte-indexed Windows HTML Clipboard Format."""
    fragment = render_html(document)
    prefix = "<html><body><!--StartFragment-->"
    suffix = "<!--EndFragment--></body></html>"
    html = f"{prefix}{fragment}{suffix}"
    header_template = (
        "Version:0.9\r\nStartHTML:{start_html:010d}\r\nEndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_fragment:010d}\r\nEndFragment:{end_fragment:010d}\r\n"
    )
    header_size = len(
        header_template.format(start_html=0, end_html=0, start_fragment=0, end_fragment=0).encode("ascii")
    )
    start_html = header_size
    start_fragment = start_html + len(prefix.encode("utf-8"))
    end_fragment = start_fragment + len(fragment.encode("utf-8"))
    end_html = start_html + len(html.encode("utf-8"))
    return (
        header_template.format(
            start_html=start_html, end_html=end_html, start_fragment=start_fragment, end_fragment=end_fragment
        )
        + html
    )


render_windows_html_clipboard = render_html_clipboard

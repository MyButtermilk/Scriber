"""Deterministic, safe projections of the canonical document AST."""

from __future__ import annotations

from html import escape

from .document_ast import Block, BlockType, Document, Inline, InlineStyle


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


def _html(text: str, inlines: tuple[Inline, ...]) -> str:
    result: list[str] = []
    for part in _parts(text, inlines):
        value = escape(part.text, quote=False).replace("\n", "<br>")
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
            output.append("<p>" + "<br>".join(escape(line, quote=False) for line in block.lines) + "</p>")
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

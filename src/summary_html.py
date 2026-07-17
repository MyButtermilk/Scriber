"""Safe semantic HTML handling for transcript summaries.

The model is allowed to describe document structure, but Scriber owns all
presentation and interaction.  Generated fragments therefore use a small,
static HTML vocabulary with no executable or style-bearing attributes.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re


SUMMARY_HTML_TAGS = frozenset(
    {
        "blockquote",
        "br",
        "code",
        "dd",
        "dl",
        "dt",
        "em",
        "h2",
        "h3",
        "h4",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "strong",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_VOID_TAGS = frozenset({"br", "hr"})
_DROP_WITH_CONTENT_TAGS = frozenset(
    {
        "analysis",
        "embed",
        "form",
        "head",
        "iframe",
        "math",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
        "think",
    }
)
_SUPPORTED_HTML_RE = re.compile(
    r"</?(?:section|h[1-4]|p|ul|ol|li|blockquote|pre|code|table|thead|tbody|tfoot|tr|th|td|dl|dt|dd|strong|em|hr|br)\b",
    re.IGNORECASE,
)
_ANY_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_FENCED_BLOCK_RE = re.compile(r"```(?:html)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class _SummaryHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._blocked_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._blocked_stack:
            if tag in _DROP_WITH_CONTENT_TAGS:
                self._blocked_stack.append(tag)
            return
        if tag in _DROP_WITH_CONTENT_TAGS:
            self._blocked_stack.append(tag)
            return
        if tag == "h1":
            tag = "h2"
        if tag not in SUMMARY_HTML_TAGS:
            return

        safe_attrs: list[str] = []
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""
            if tag in {"th", "td"} and name in {"colspan", "rowspan"}:
                if value.isdigit() and 1 <= int(value) <= 24:
                    safe_attrs.append(f' {name}="{value}"')
            elif tag == "th" and name == "scope" and value.lower() in {"row", "col"}:
                safe_attrs.append(f' scope="{value.lower()}"')

        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._blocked_stack:
            if tag in self._blocked_stack:
                while self._blocked_stack:
                    blocked = self._blocked_stack.pop()
                    if blocked == tag:
                        break
            return
        if tag == "h1":
            tag = "h2"
        if tag in SUMMARY_HTML_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_stack:
            self.parts.append(escape(data, quote=False))


def _inline_markdown_to_html(value: str) -> str:
    safe = escape(value.strip(), quote=False)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)
    safe = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", safe)
    return safe


def _markdown_to_html_fragment(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"(?m)^(?P<indent>[ \t]*)[•●◦▪▫‣⁃]\s+", r"\g<indent>- ", normalized)
    parts: list[str] = []
    paragraph: list[str] = []
    list_tag = ""

    def flush_paragraph() -> None:
        if paragraph:
            parts.append(f"<p>{_inline_markdown_to_html(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = ""

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        bullet = re.match(r"^[-*+]\s+(.+)$", line)
        numbered = re.match(r"^\d+[.)]\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = max(2, len(heading.group(1)))
            parts.append(f"<h{level}>{_inline_markdown_to_html(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_paragraph()
            requested_tag = "ul" if bullet else "ol"
            if list_tag != requested_tag:
                close_list()
                list_tag = requested_tag
                parts.append(f"<{list_tag}>")
            item = (bullet or numbered).group(1)
            parts.append(f"<li>{_inline_markdown_to_html(item)}</li>")
        elif line.startswith(">"):
            flush_paragraph()
            close_list()
            parts.append(f"<blockquote>{_inline_markdown_to_html(line[1:])}</blockquote>")
        elif re.fullmatch(r"-{3,}", line):
            flush_paragraph()
            close_list()
            parts.append("<hr>")
        else:
            close_list()
            paragraph.append(line)

    flush_paragraph()
    close_list()
    return "".join(parts)


def normalize_summary_html(value: str) -> str:
    """Return a safe static HTML fragment, tolerating common model drift."""
    raw = (value or "").replace("\u00a0", " ").replace("\u200b", "").strip()
    fenced = _FENCED_BLOCK_RE.fullmatch(raw)
    if fenced:
        raw = fenced.group(1).strip()
    else:
        # Less capable models sometimes add a short prose preface around one
        # otherwise valid fenced fragment. Accept exactly one HTML-bearing
        # fence; ambiguous multi-block output still goes through validation
        # and is rejected instead of guessing which block is authoritative.
        fenced_blocks = _FENCED_BLOCK_RE.findall(raw)
        html_fences = [block.strip() for block in fenced_blocks if _SUPPORTED_HTML_RE.search(block)]
        if len(html_fences) == 1:
            raw = html_fences[0]
    if not _SUPPORTED_HTML_RE.search(raw) and not _ANY_HTML_TAG_RE.search(raw):
        raw = _markdown_to_html_fragment(raw)

    sanitizer = _SummaryHtmlSanitizer()
    sanitizer.feed(raw)
    sanitizer.close()
    return "".join(sanitizer.parts).strip()


class _HtmlToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"section", "p", "blockquote", "pre", "table", "tr"}:
            self.parts.append("\n")
        elif re.fullmatch(r"h[1-4]", tag):
            self.parts.append(f"\n{'#' * int(tag[1])} ")
        elif tag in {"ul", "ol"}:
            self.lists.append(tag)
            self.parts.append("\n")
        elif tag == "li":
            marker = "1. " if self.lists and self.lists[-1] == "ol" else "- "
            self.parts.append(f"\n{marker}")
        elif tag == "strong":
            self.parts.append("**")
        elif tag == "em":
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"th", "td"}:
            self.parts.append(" | ")
        elif tag == "hr":
            self.parts.append("\n---\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"strong", "em"}:
            self.parts.append("**" if tag == "strong" else "*")
        elif tag == "code":
            self.parts.append("`")
        elif tag in {"section", "p", "blockquote", "pre", "tr"} or re.fullmatch(r"h[1-4]", tag):
            self.parts.append("\n")
        elif tag in {"ul", "ol"}:
            if self.lists:
                self.lists.pop()
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def summary_html_to_markdown(value: str) -> str:
    """Project safe HTML into the existing DOCX/PDF Markdown export path."""
    parser = _HtmlToMarkdown()
    parser.feed(normalize_summary_html(value))
    parser.close()
    markdown = "".join(parser.parts)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


class _VisibleTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "blockquote",
            "br",
            "dd",
            "dl",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "hr",
            "li",
            "p",
            "pre",
            "section",
            "table",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def summary_visible_text(value: str, summary_format: str = "markdown") -> str:
    """Return only human-visible text for search indexing and previews."""
    raw = value or ""
    if (summary_format or "").strip().lower() == "html":
        raw = normalize_summary_html(raw)
    else:
        raw = _markdown_to_html_fragment(raw)
    parser = _VisibleTextExtractor()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts).replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


_SECTION_LEAD_RE = re.compile(
    r"^\s*<section>\s*<h2>(?P<title>.*?)</h2>\s*<p>(?P<standfirst>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_UNWRAPPED_LEAD_RE = re.compile(
    r"^\s*<h2>(?P<title>.*?)</h2>\s*<p>(?P<standfirst>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)


class _SummaryDocumentStructureValidator(HTMLParser):
    """Validate exact tag balance and the sanitized top-level shape."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.top_level_tags: list[str] = []
        self.has_top_level_text = False
        self.valid = True

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if not self.stack:
            self.top_level_tags.append(tag)
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        if not self.stack:
            self.top_level_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS or not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.stack and data.strip():
            self.has_top_level_text = True


def _validated_top_level_tags(value: str) -> tuple[str, ...] | None:
    parser = _SummaryDocumentStructureValidator()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return None
    if not parser.valid or parser.stack or parser.has_top_level_text:
        return None
    return tuple(parser.top_level_tags)


def normalize_summary_document_html(value: str) -> str:
    """Return safe HTML only when it has the minimum premium document shape.

    Markdown drift with a real heading and standfirst is repaired by wrapping
    the fragment in one section. A plain paragraph, forbidden-only markup, or a
    section without its required title/standfirst is rejected so callers do not
    persist an empty or visibly broken summary as completed.
    """
    raw = (value or "").replace("\u00a0", " ").replace("\u200b", "").strip()
    if len(_FENCED_BLOCK_RE.findall(raw)) > 1:
        return ""

    normalized = normalize_summary_html(raw)
    if not normalized:
        return ""

    # A provider may leak a short untagged preface before the requested HTML
    # fragment (for example after hidden reasoning). It is not part of the
    # document contract. Keep strict rejection when that prefix itself contains
    # supported structure, but safely discard plain chatter before a complete
    # section tree.
    first_section = re.search(r"<section>", normalized, flags=re.IGNORECASE)
    if first_section is not None and first_section.start() > 0:
        prefix = normalized[: first_section.start()]
        if not _SUPPORTED_HTML_RE.search(prefix):
            normalized = normalized[first_section.start() :]

    match = _SECTION_LEAD_RE.match(normalized)
    if match is None:
        match = _UNWRAPPED_LEAD_RE.match(normalized)
        if match is None:
            return ""
        top_level_tags = _validated_top_level_tags(normalized)
        if not top_level_tags or "section" in top_level_tags:
            return ""
        normalized = f"<section>{normalized}</section>"
    else:
        top_level_tags = _validated_top_level_tags(normalized)
        if not top_level_tags or any(tag != "section" for tag in top_level_tags):
            return ""

    if not summary_visible_text(match.group("title"), "html"):
        return ""
    if not summary_visible_text(match.group("standfirst"), "html"):
        return ""
    return normalized

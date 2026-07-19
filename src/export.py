"""Dependency-free export utilities for transcripts (PDF, DOCX)."""

from __future__ import annotations

import html
import io
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from src.summary_html import summary_html_to_markdown


_DEFAULT_DOCUMENT_LABELS = {
    "date": "Date",
    "duration": "Duration",
    "summary": "Summary",
    "transcript": "Transcript",
}

_INLINE_MARKDOWN_RE = re.compile(
    r"(\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)"
)


@dataclass(frozen=True)
class _InlineSpan:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass(frozen=True)
class _ExportBlock:
    kind: str
    spans: tuple[_InlineSpan, ...] = ()
    heading_level: int = 0


def _document_labels(labels: Optional[Mapping[str, str]]) -> dict[str, str]:
    resolved = dict(_DEFAULT_DOCUMENT_LABELS)
    if labels:
        resolved.update({key: str(value) for key, value in labels.items() if str(value)})
    return resolved


def _summary_for_export(summary: Optional[str], summary_format: str) -> Optional[str]:
    if not summary:
        return summary
    if (summary_format or "markdown").strip().lower() == "html":
        return summary_html_to_markdown(summary)
    return summary


def _parse_markdown_line(line: str) -> Tuple[str, dict]:
    """Parse one Markdown line into its text and block-level metadata."""

    metadata = {"heading_level": 0, "is_bullet": False, "is_bold": False}
    text = line.strip()

    heading_match = re.match(r"^(#{1,6})\s+(.+)$", text)
    if heading_match:
        metadata["heading_level"] = len(heading_match.group(1))
        return heading_match.group(2), metadata

    bullet_match = re.match(r"^[-*+]\s+(.+)$", text)
    if bullet_match:
        metadata["is_bullet"] = True
        text = bullet_match.group(1)

    numbered_match = re.match(r"^\d+\.\s+(.+)$", text)
    if numbered_match:
        metadata["is_bullet"] = True
        text = numbered_match.group(1)

    return text, metadata


def _parse_inline_markdown(text: str) -> tuple[_InlineSpan, ...]:
    """Return the small inline-Markdown subset supported by both exporters."""

    spans: list[_InlineSpan] = []
    last_end = 0
    for match in _INLINE_MARKDOWN_RE.finditer(text):
        if match.start() > last_end:
            spans.append(_InlineSpan(text[last_end : match.start()]))
        if match.group(2) is not None:
            spans.append(_InlineSpan(match.group(2), bold=True, italic=True))
        elif match.group(3) is not None:
            spans.append(_InlineSpan(match.group(3), bold=True))
        elif match.group(4) is not None:
            spans.append(_InlineSpan(match.group(4), italic=True))
        else:
            spans.append(_InlineSpan(match.group(5) or "", code=True))
        last_end = match.end()
    if last_end < len(text):
        spans.append(_InlineSpan(text[last_end:]))
    if not spans and text:
        spans.append(_InlineSpan(text))
    return tuple(spans)


def _convert_markdown_to_html(text: str) -> str:
    """Retain the historical helper for callers that need basic safe HTML."""

    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<i>\1</i>", escaped)
    return re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', escaped)


def _build_export_blocks(
    *,
    title: str,
    content: str,
    summary: Optional[str],
    date: Optional[str],
    duration: Optional[str],
    labels: Mapping[str, str],
) -> list[_ExportBlock]:
    blocks = [_ExportBlock("title", (_InlineSpan(str(title)),))]
    metadata: list[str] = []
    if date:
        metadata.append(f"{labels['date']}: {date}")
    if duration:
        metadata.append(f"{labels['duration']}: {duration}")
    if metadata:
        blocks.append(_ExportBlock("metadata", (_InlineSpan(" | ".join(metadata)),)))
    blocks.append(_ExportBlock("spacer"))

    if summary:
        blocks.append(_ExportBlock("heading1", (_InlineSpan(labels["summary"]),)))
        for raw_line in summary.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            text, line_metadata = _parse_markdown_line(line)
            heading_level = int(line_metadata["heading_level"])
            if heading_level:
                blocks.append(
                    _ExportBlock(
                        "summary_heading",
                        _parse_inline_markdown(text),
                        heading_level=heading_level,
                    )
                )
            elif line_metadata["is_bullet"]:
                blocks.append(_ExportBlock("bullet", _parse_inline_markdown(text)))
            else:
                blocks.append(_ExportBlock("body", _parse_inline_markdown(text)))
        blocks.append(_ExportBlock("spacer"))

    blocks.append(_ExportBlock("heading1", (_InlineSpan(labels["transcript"]),)))
    for raw_paragraph in content.split("\n\n"):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        if paragraph.startswith("[Speaker"):
            parts = paragraph.split(":", 1)
            if len(parts) == 2:
                speaker, speaker_text = parts[0].strip(), parts[1].strip()
                blocks.append(
                    _ExportBlock(
                        "speaker",
                        (
                            _InlineSpan(f"{speaker}: ", bold=True),
                            _InlineSpan(speaker_text),
                        ),
                    )
                )
                continue
        blocks.append(_ExportBlock("body", (_InlineSpan(paragraph),)))
    return blocks


def _clean_xml_text(value: str) -> str:
    """Drop characters forbidden by XML 1.0 while preserving all normal Unicode."""

    return "".join(
        character
        for character in str(value).replace("\r\n", "\n").replace("\r", "\n")
        if character in "\t\n"
        or "\x20" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
    )


def _xml(value: str) -> str:
    return html.escape(_clean_xml_text(value), quote=True)


def _docx_run(span: _InlineSpan) -> str:
    properties: list[str] = []
    if span.bold:
        properties.append("<w:b/>")
    if span.italic:
        properties.append("<w:i/>")
    if span.code:
        properties.append(
            '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
            'w:eastAsia="Consolas" w:cs="Consolas"/>'
        )
    run_properties = f"<w:rPr>{''.join(properties)}</w:rPr>" if properties else ""
    content: list[str] = []
    parts = _clean_xml_text(span.text).split("\n")
    for index, part in enumerate(parts):
        if index:
            content.append("<w:br/>")
        if part:
            content.append(f'<w:t xml:space="preserve">{_xml(part)}</w:t>')
    if not content:
        content.append("<w:t/>")
    return f"<w:r>{run_properties}{''.join(content)}</w:r>"


def _docx_paragraph(
    spans: tuple[_InlineSpan, ...] = (),
    *,
    style: Optional[str] = None,
    bullet: bool = False,
) -> str:
    properties: list[str] = []
    if style:
        properties.append(f'<w:pStyle w:val="{style}"/>')
    if bullet:
        properties.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
    paragraph_properties = (
        f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
    )
    return f"<w:p>{paragraph_properties}{''.join(_docx_run(span) for span in spans)}</w:p>"


def _docx_document_xml(blocks: list[_ExportBlock]) -> str:
    paragraphs: list[str] = []
    for block in blocks:
        if block.kind == "spacer":
            paragraphs.append("<w:p/>")
        elif block.kind == "title":
            paragraphs.append(_docx_paragraph(block.spans, style="Title"))
        elif block.kind == "metadata":
            paragraphs.append(_docx_paragraph(block.spans, style="Metadata"))
        elif block.kind == "heading1":
            paragraphs.append(_docx_paragraph(block.spans, style="Heading1"))
        elif block.kind == "summary_heading":
            level = min(block.heading_level + 1, 4)
            paragraphs.append(_docx_paragraph(block.spans, style=f"Heading{level}"))
        elif block.kind == "bullet":
            paragraphs.append(_docx_paragraph(block.spans, style="ListBullet", bullet=True))
        else:
            paragraphs.append(_docx_paragraph(block.spans, style="Normal"))

    section = (
        "<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/>'
        '<w:cols w:space="708"/>'
        '<w:docGrid w:linePitch="360"/>'
        "</w:sectPr>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{''.join(paragraphs)}{section}</w:body>"
        "</w:document>"
    )


def _docx_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:jc w:val="center"/><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:color w:val="1F2937"/><w:sz w:val="52"/><w:szCs w:val="52"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="Metadata"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/><w:spacing w:after="160"/></w:pPr><w:rPr><w:i/><w:color w:val="666666"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="320" w:after="160"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="2F5496"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="2F5496"/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="200" w:after="100"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:color w:val="365F91"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="180" w:after="80"/><w:outlineLvl w:val="3"/></w:pPr><w:rPr><w:b/><w:i/><w:color w:val="365F91"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="80"/></w:pPr></w:style>
</w:styles>"""


def _docx_numbering_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:ind w:left="720" w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:hint="default"/></w:rPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""


def _zip_entry(archive: ZipFile, name: str, payload: str) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload.encode("utf-8"))


def _build_docx(blocks: list[_ExportBlock], *, title: str) -> bytes:
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    package_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    document_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>"""
    core_properties = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{_xml(title)}</dc:title><dc:creator>Scriber</dc:creator><cp:lastModifiedBy>Scriber</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""
    app_properties = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Scriber</Application><AppVersion>1.0</AppVersion></Properties>"""

    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        _zip_entry(archive, "[Content_Types].xml", content_types)
        _zip_entry(archive, "_rels/.rels", package_relationships)
        _zip_entry(archive, "docProps/core.xml", core_properties)
        _zip_entry(archive, "docProps/app.xml", app_properties)
        _zip_entry(archive, "word/document.xml", _docx_document_xml(blocks))
        _zip_entry(archive, "word/styles.xml", _docx_styles_xml())
        _zip_entry(archive, "word/numbering.xml", _docx_numbering_xml())
        _zip_entry(archive, "word/_rels/document.xml.rels", document_relationships)
    return buffer.getvalue()


@dataclass(frozen=True)
class _PdfStyle:
    size: float
    leading: float
    space_before: float = 0.0
    space_after: float = 0.0
    left_indent: float = 0.0
    bullet_indent: float = 0.0
    alignment: str = "left"
    base_bold: bool = False
    gray: float = 0.0
    keep_with_next: bool = False


_PDF_STYLES = {
    "title": _PdfStyle(18, 22, space_after=12, alignment="center", base_bold=True),
    "metadata": _PdfStyle(10, 14, space_after=20, alignment="center", gray=0.45),
    "heading1": _PdfStyle(14, 18, space_before=16, space_after=8, base_bold=True, keep_with_next=True),
    "heading2": _PdfStyle(12, 15, space_before=12, space_after=6, base_bold=True, keep_with_next=True),
    "heading3": _PdfStyle(11, 14, space_before=10, space_after=4, base_bold=True, keep_with_next=True),
    "body": _PdfStyle(10, 14, space_after=6, alignment="justify"),
    "speaker": _PdfStyle(10, 14, space_after=6),
    "bullet": _PdfStyle(10, 14, space_after=4, left_indent=20, bullet_indent=10),
    "spacer": _PdfStyle(1, 12),
}


def _pdf_style(block: _ExportBlock) -> _PdfStyle:
    if block.kind == "summary_heading":
        return _PDF_STYLES["heading2" if block.heading_level <= 2 else "heading3"]
    return _PDF_STYLES.get(block.kind, _PDF_STYLES["body"])


def _pdf_font(span: _InlineSpan, style: _PdfStyle) -> str:
    if span.code:
        return "F5"
    bold = style.base_bold or span.bold
    if bold and span.italic:
        return "F4"
    if bold:
        return "F2"
    if span.italic:
        return "F3"
    return "F1"


def _pdf_clean_text(value: str) -> str:
    return (
        str(value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", "    ")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )


def _pdf_width(text: str, font: str, size: float) -> float:
    if font == "F5":
        return len(text.encode("cp1252", errors="replace")) * size * 0.60
    units = 0.0
    for character in text:
        if character in " \u00a0":
            units += 0.278
        elif character in "ilI.,'`:;!|":
            units += 0.28
        elif character in "mwMW@%&":
            units += 0.84
        elif character.isupper():
            units += 0.68
        elif character.isdigit():
            units += 0.56
        else:
            units += 0.54
    return units * size


def _append_pdf_chunk(
    chunks: list[tuple[str, str]], text: str, font: str
) -> None:
    if not text:
        return
    if chunks and chunks[-1][1] == font:
        chunks[-1] = (chunks[-1][0] + text, font)
    else:
        chunks.append((text, font))


def _split_pdf_word(text: str, font: str, size: float, width: float) -> list[str]:
    pieces: list[str] = []
    current = ""
    for character in text:
        if current and _pdf_width(current + character, font, size) > width:
            pieces.append(current)
            current = character
        else:
            current += character
    if current:
        pieces.append(current)
    return pieces or [""]


def _wrap_pdf_spans(
    spans: tuple[_InlineSpan, ...], style: _PdfStyle, width: float
) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = []
    line: list[tuple[str, str]] = []
    line_width = 0.0
    pending_space: Optional[tuple[str, str]] = None

    def finish_line(*, keep_empty: bool = False) -> None:
        nonlocal line, line_width, pending_space
        if line or keep_empty:
            lines.append(line)
        line = []
        line_width = 0.0
        pending_space = None

    for span in spans:
        font = _pdf_font(span, style)
        for token in re.split(r"(\n|[ ]+)", _pdf_clean_text(span.text)):
            if not token:
                continue
            if token == "\n":
                finish_line(keep_empty=True)
                continue
            if token.isspace():
                pending_space = (" ", font)
                continue

            space_width = (
                _pdf_width(pending_space[0], pending_space[1], style.size)
                if pending_space and line
                else 0.0
            )
            token_width = _pdf_width(token, font, style.size)
            if line and line_width + space_width + token_width > width:
                finish_line()
                space_width = 0.0
            if token_width <= width:
                if pending_space and line:
                    _append_pdf_chunk(line, pending_space[0], pending_space[1])
                    line_width += space_width
                _append_pdf_chunk(line, token, font)
                line_width += token_width
                pending_space = None
                continue

            for piece_index, piece in enumerate(
                _split_pdf_word(token, font, style.size, width)
            ):
                piece_width = _pdf_width(piece, font, style.size)
                if line:
                    finish_line()
                _append_pdf_chunk(line, piece, font)
                line_width = piece_width
                if piece_index < len(_split_pdf_word(token, font, style.size, width)) - 1:
                    finish_line()
            pending_space = None

    finish_line(keep_empty=not lines)
    return lines


def _pdf_literal(value: str) -> bytes:
    encoded = _pdf_clean_text(value).encode("cp1252", errors="replace")
    escaped = bytearray(b"(")
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):
            escaped.extend(b"\\" + bytes((byte,)))
        elif byte < 0x20 or byte >= 0x7F:
            escaped.extend(f"\\{byte:03o}".encode("ascii"))
        else:
            escaped.append(byte)
    escaped.append(0x29)
    return bytes(escaped)


def _pdf_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _pdf_line_width(line: list[tuple[str, str]], size: float) -> float:
    return sum(_pdf_width(text, font, size) for text, font in line)


def _draw_pdf_line(
    commands: list[bytes],
    line: list[tuple[str, str]],
    *,
    x: float,
    y: float,
    available_width: float,
    style: _PdfStyle,
    justify: bool,
) -> None:
    line_width = _pdf_line_width(line, style.size)
    if style.alignment == "center":
        cursor_x = x + max(0.0, (available_width - line_width) / 2)
    else:
        cursor_x = x
    spaces = sum(text.count(" ") for text, _font in line)
    extra_space = (
        max(0.0, available_width - line_width) / spaces
        if justify and spaces
        else 0.0
    )
    gray = _pdf_number(style.gray)
    for text, font in line:
        fragments = re.split(r"( +)", text)
        for fragment in fragments:
            if not fragment:
                continue
            width = _pdf_width(fragment, font, style.size)
            if fragment.isspace():
                cursor_x += width + extra_space * len(fragment)
                continue
            commands.append(
                b"BT /"
                + font.encode("ascii")
                + b" "
                + _pdf_number(style.size).encode("ascii")
                + b" Tf "
                + gray.encode("ascii")
                + b" g 1 0 0 1 "
                + _pdf_number(cursor_x).encode("ascii")
                + b" "
                + _pdf_number(y).encode("ascii")
                + b" Tm "
                + _pdf_literal(fragment)
                + b" Tj ET"
            )
            cursor_x += width


def _layout_pdf(blocks: list[_ExportBlock]) -> list[bytes]:
    page_width, page_height = 595.276, 841.89
    left_margin = right_margin = top_margin = bottom_margin = 72.0
    body_width = page_width - left_margin - right_margin
    pages: list[list[bytes]] = [[]]
    cursor_top = page_height - top_margin
    page_has_content = False

    def new_page() -> None:
        nonlocal cursor_top, page_has_content
        pages.append([])
        cursor_top = page_height - top_margin
        page_has_content = False

    for index, block in enumerate(blocks):
        style = _pdf_style(block)
        if block.kind == "spacer":
            cursor_top -= style.leading
            if cursor_top < bottom_margin:
                new_page()
            continue

        available_width = body_width - style.left_indent
        lines = _wrap_pdf_spans(block.spans, style, available_width)
        before = style.space_before if page_has_content else 0.0
        minimum_lines = min(2, len(lines)) if style.keep_with_next else 1
        minimum_height = before + minimum_lines * style.leading
        if style.keep_with_next and index + 1 < len(blocks):
            next_block = blocks[index + 1]
            if next_block.kind != "spacer":
                minimum_height += _pdf_style(next_block).leading
        if cursor_top - minimum_height < bottom_margin:
            new_page()
            before = 0.0
        cursor_top -= before

        for line_index, line in enumerate(lines):
            if cursor_top - style.leading < bottom_margin:
                new_page()
            baseline = cursor_top - style.size
            x = left_margin + style.left_indent
            if block.kind == "bullet" and line_index == 0:
                bullet_style = _PdfStyle(style.size, style.leading)
                _draw_pdf_line(
                    pages[-1],
                    [("•", "F1")],
                    x=left_margin + style.bullet_indent,
                    y=baseline,
                    available_width=style.left_indent - style.bullet_indent,
                    style=bullet_style,
                    justify=False,
                )
            _draw_pdf_line(
                pages[-1],
                line,
                x=x,
                y=baseline,
                available_width=available_width,
                style=style,
                justify=style.alignment == "justify" and line_index < len(lines) - 1,
            )
            cursor_top -= style.leading
            page_has_content = True
        cursor_top -= style.space_after
    return [b"\n".join(commands) for commands in pages]


def _pdf_stream(payload: bytes) -> bytes:
    compressed = zlib.compress(payload, 9)
    return (
        f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("ascii")
        + compressed
        + b"\nendstream"
    )


def _build_pdf(blocks: list[_ExportBlock], *, title: str) -> bytes:
    page_streams = _layout_pdf(blocks)
    objects: dict[int, bytes] = {
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>",
        6: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-BoldOblique /Encoding /WinAnsiEncoding >>",
        7: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    }
    page_ids: list[int] = []
    next_id = 8
    font_resources = b"<< /F1 3 0 R /F2 4 0 R /F3 5 0 R /F4 6 0 R /F5 7 0 R >>"
    for stream in page_streams:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        objects[content_id] = _pdf_stream(stream)
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.276 841.89] "
            b"/Resources << /Font "
            + font_resources
            + b" >> /Contents "
            + f"{content_id} 0 R >>".encode("ascii")
        )
    info_id = next_id
    creation = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    objects[info_id] = (
        b"<< /Title "
        + _pdf_literal(title)
        + b" /Author (Scriber) /Creator (Scriber) /Producer (Scriber stdlib PDF) "
        + b"/CreationDate "
        + _pdf_literal(creation)
        + b" >>"
    )
    objects[2] = (
        b"<< /Type /Pages /Count "
        + str(len(page_ids)).encode("ascii")
        + b" /Kids ["
        + b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_ids)
        + b"] >>"
    )
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max(objects) + 1)
    for object_id in range(1, max(objects) + 1):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        b"trailer\n<< /Size "
        + str(len(offsets)).encode("ascii")
        + b" /Root 1 0 R /Info "
        + f"{info_id} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def export_to_docx(
    title: str,
    content: str,
    summary: Optional[str] = None,
    date: Optional[str] = None,
    duration: Optional[str] = None,
    labels: Optional[Mapping[str, str]] = None,
    summary_format: str = "markdown",
) -> bytes:
    """Export a transcript as a self-contained, standards-based OOXML document."""

    document_labels = _document_labels(labels)
    normalized_summary = _summary_for_export(summary, summary_format)
    blocks = _build_export_blocks(
        title=title,
        content=content,
        summary=normalized_summary,
        date=date,
        duration=duration,
        labels=document_labels,
    )
    return _build_docx(blocks, title=title)


def export_to_pdf(
    title: str,
    content: str,
    summary: Optional[str] = None,
    date: Optional[str] = None,
    duration: Optional[str] = None,
    labels: Optional[Mapping[str, str]] = None,
    summary_format: str = "markdown",
) -> bytes:
    """Export a transcript as a paginated A4 PDF using standard PDF fonts."""

    document_labels = _document_labels(labels)
    normalized_summary = _summary_for_export(summary, summary_format)
    blocks = _build_export_blocks(
        title=title,
        content=content,
        summary=normalized_summary,
        date=date,
        duration=duration,
        labels=document_labels,
    )
    return _build_pdf(blocks, title=title)

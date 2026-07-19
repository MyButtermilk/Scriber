from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
import io
import re
import subprocess
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from scripts.check_backend_runtime_imports import (
    BLOCKED_FROZEN_EXPORT_IMPORTS,
    FROZEN_EXPORT_COMPAT_IMPORTS,
    REQUIRED_FROZEN_EXPORT_IMPORTS,
    check_frozen_text_export_graph,
)
from src.export import export_to_docx, export_to_pdf
from src.meeting_export import (
    build_eml_draft,
    build_meeting_markdown,
    build_meeting_summary_markdown,
    build_meeting_transcript_text,
    meeting_export_labels,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _decode_pdf_literal(value: bytes) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        byte = value[index]
        if byte != 0x5C:
            decoded.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        escaped = value[index]
        replacements = {
            ord("n"): ord("\n"),
            ord("r"): ord("\r"),
            ord("t"): ord("\t"),
            ord("b"): ord("\b"),
            ord("f"): ord("\f"),
            ord("("): ord("("),
            ord(")"): ord(")"),
            ord("\\"): ord("\\"),
        }
        if escaped in replacements:
            decoded.append(replacements[escaped])
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            end = index + 1
            while end < min(index + 3, len(value)) and ord("0") <= value[end] <= ord("7"):
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        if escaped in (ord("\r"), ord("\n")):
            if escaped == ord("\r") and index + 1 < len(value) and value[index + 1] == ord("\n"):
                index += 1
            index += 1
            continue
        decoded.append(escaped)
        index += 1
    return decoded.decode("cp1252")


def _pdf_text_and_page_count(payload: bytes) -> tuple[str, int]:
    assert payload.startswith(b"%PDF-")
    streams: list[bytes] = []
    for match in re.finditer(
        rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\n?endstream",
        payload,
        flags=re.DOTALL,
    ):
        dictionary, data = match.groups()
        try:
            if b"/ASCII85Decode" in dictionary:
                data = base64.a85decode(data.strip(), adobe=True)
            if b"/FlateDecode" in dictionary:
                data = zlib.decompress(data)
        except (ValueError, zlib.error):
            continue
        streams.append(data)

    text_parts: list[str] = []
    for stream in streams:
        for literal in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", stream):
            text_parts.append(_decode_pdf_literal(literal))
    page_count = len(re.findall(rb"/Type\s*/Page\b", payload))
    return " ".join(text_parts), page_count


def _docx_text_and_paragraph_count(payload: bytes) -> tuple[str, int]:
    with ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        assert "word/styles.xml" in names
        document_xml = archive.read("word/document.xml")
        ElementTree.fromstring(archive.read("[Content_Types].xml"))
        ElementTree.fromstring(archive.read("word/styles.xml"))
    root = ElementTree.fromstring(document_xml)
    text = " ".join(node.text or "" for node in root.iter(f"{WORD_NAMESPACE}t"))
    paragraph_count = sum(1 for _ in root.iter(f"{WORD_NAMESPACE}p"))
    return text, paragraph_count


def _meeting_detail(language: str) -> dict[str, object]:
    german = language == "de"
    marker = "Prüfpunkt Größe Straße café" if german else "Review point size café"
    segments = [
        {
            "startMs": index * 4_000,
            "endMs": (index + 1) * 4_000,
            "speakerLabel": "Sprecherin" if german else "Speaker",
            "source": "microphone",
            "alignmentQuality": "exact",
            "text": f"{marker} {index:03d}: " + ("verlässlicher Exporttext. " * 5),
        }
        for index in range(140)
    ]
    return {
        "title": "Planungsprüfung München" if german else "Planning review London",
        "language": language,
        "startedAt": "2026-07-19T14:00:00Z",
        "segments": segments,
        "outputs": [
            {
                "kind": "analysis",
                "status": "completed",
                "payload": {
                    "outputLanguage": language,
                    "executiveSummary": (
                        "Größe und Qualität wurden bestätigt."
                        if german
                        else "Size and quality were confirmed."
                    ),
                    "decisions": [{"text": marker}],
                    "openQuestions": [],
                    "risks": [],
                },
            }
        ],
        "actionItems": [
            {
                "text": marker,
                "owner": "Jörg" if german else "Zoë",
                "dueDate": "2026-07-31",
                "status": "open",
            }
        ],
        "notes": [{"atMs": 12_000, "body": marker}],
        "captureMetadata": {
            "calendarEvent": {
                "organizer": {"name": "Zoë", "address": "zoe@example.com"},
                "participants": [],
            }
        },
    }


def test_frozen_export_graph_requires_stdlib_renderer_and_rejects_legacy_graphs() -> None:
    required = set(REQUIRED_FROZEN_EXPORT_IMPORTS)
    blocked = set(BLOCKED_FROZEN_EXPORT_IMPORTS)
    compat = set(FROZEN_EXPORT_COMPAT_IMPORTS)

    assert required == {"src.export"}
    assert compat == {"PIL", "docx", "reportlab.platypus"}
    assert blocked == {"lxml"}
    assert required.isdisjoint(blocked)

    imported: list[str] = []

    def fake_import(module_name: str) -> object:
        imported.append(module_name)
        if module_name in blocked:
            raise ModuleNotFoundError(name=module_name)
        if module_name == "src.export":
            return SimpleNamespace(
                export_to_pdf=export_to_pdf,
                export_to_docx=export_to_docx,
            )
        if module_name in compat:
            return SimpleNamespace(SCRIBER_STDLIB_EXPORT_COMPAT=True)
        return SimpleNamespace()

    assert check_frozen_text_export_graph(frozen=True, import_module=fake_import) == []
    assert imported == [
        *REQUIRED_FROZEN_EXPORT_IMPORTS,
        *FROZEN_EXPORT_COMPAT_IMPORTS,
        *BLOCKED_FROZEN_EXPORT_IMPORTS,
    ]
    assert check_frozen_text_export_graph(frozen=False, import_module=fake_import) == []


def test_spec_excludes_the_complete_legacy_export_dependency_graph() -> None:
    spec = (REPO_ROOT / "packaging" / "scriber-backend.spec").read_text(encoding="utf-8")

    excludes = spec.split("excludes=[", 1)[1].split("],\n    noarchive", 1)[0]
    for root in ("lxml",):
        assert f'"{root}",' in excludes
    for root in ("PIL", "docx", "reportlab"):
        assert f'"{root}",' not in excludes
    assert 'pathex=[str(stdlib_export_compat_root), str(repo_root)]' in spec
    assert 'hookspath=[str(pyinstaller_hook_root)]' in spec
    for hook in ("hook-PIL.py", "hook-PIL.Image.py", "hook-docx.py", "hook-reportlab.py"):
        assert (REPO_ROOT / "packaging" / "pyinstaller_hooks" / hook).is_file()
    assert "PILLOW_TEXT_EXPORT_MODULES" not in spec
    assert "LXML_TEXT_EXPORT_MODULES" not in spec
    assert 'collect_submodules("PIL")' not in spec
    assert 'collect_submodules("lxml")' not in spec
    generic_collection = spec.split("for package in (", 1)[1].split("):", 1)[0]
    assert '"PIL"' not in generic_collection

    installer_hooks = (
        REPO_ROOT / "Frontend" / "src-tauri" / "windows" / "installer-hooks.nsh"
    ).read_text(encoding="utf-8")
    for root in ("PIL", "docx", "lxml", "reportlab"):
        assert f'RMDir "$INSTDIR\\backend\\_internal\\{root}"' in installer_hooks
    assert "RMDir /r" not in installer_hooks


@pytest.mark.parametrize(
    ("language", "summary_label", "transcript_label", "marker"),
    (
        ("de", "Zusammenfassung", "Transkript", "Prüfpunkt Größe Straße café"),
        ("en", "Summary", "Transcript", "Review point size café"),
    ),
)
def test_localized_multipage_meeting_pdf_docx_and_eml_exports(
    language: str,
    summary_label: str,
    transcript_label: str,
    marker: str,
) -> None:
    detail = _meeting_detail(language)
    labels = meeting_export_labels(detail, fallback_language="en")
    summary = build_meeting_summary_markdown(detail, fallback_language="en")
    transcript = build_meeting_transcript_text(detail, fallback_language="en")
    meeting_markdown = build_meeting_markdown(detail, fallback_language="en")

    assert marker in summary
    assert marker in transcript
    assert marker in meeting_markdown
    assert labels["summary"] == summary_label
    assert labels["transcript"] == transcript_label

    export_labels = {
        "date": labels["date"],
        "duration": labels["duration"],
        "summary": summary_label,
        "transcript": transcript_label,
    }
    title = str(detail["title"])
    pdf = export_to_pdf(
        title,
        transcript,
        summary=summary,
        date="19.07.2026" if language == "de" else "2026-07-19",
        duration="9:20",
        labels=export_labels,
    )
    docx = export_to_docx(
        title,
        transcript,
        summary=summary,
        date="19.07.2026" if language == "de" else "2026-07-19",
        duration="9:20",
        labels=export_labels,
    )

    pdf_text, page_count = _pdf_text_and_page_count(pdf)
    assert page_count >= 5
    assert title in pdf_text
    assert summary_label in pdf_text
    assert transcript_label in pdf_text
    assert marker in pdf_text

    docx_text, paragraph_count = _docx_text_and_paragraph_count(docx)
    assert paragraph_count >= 140
    assert title in docx_text
    assert summary_label in docx_text
    assert transcript_label in docx_text
    assert marker in docx_text

    attachment_name = f"meeting-{language}.pdf"
    eml = build_eml_draft(
        detail,
        attachment=pdf,
        attachment_name=attachment_name,
        attachment_type="application/pdf",
        fallback_language="en",
    )
    assert b"\r\n" in eml
    assert b"\n" not in eml.replace(b"\r\n", b"")
    parsed = BytesParser(policy=policy.default).parsebytes(eml)
    assert parsed["X-Unsent"] == "1"
    body = parsed.get_body(preferencelist=("plain",)).get_content()
    assert marker in body
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == attachment_name
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == pdf


def test_stdlib_exports_escape_markup_and_preserve_formatting_and_unicode() -> None:
    title = "Prüfung (A) \\ & <Größe>"
    summary = "# Abschnitt\n- **Straße** *café* `x(y)\\z`"
    content = "[Speaker 1]: Jörg & Zoë (ok) \\ <tag>\n\nUnicode: € 漢字 🙂"

    docx = export_to_docx(title, content, summary=summary)
    docx_text, paragraph_count = _docx_text_and_paragraph_count(docx)
    assert paragraph_count >= 7
    assert title in docx_text
    assert "Straße" in docx_text
    assert "Jörg & Zoë (ok) \\ <tag>" in docx_text
    assert "€ 漢字 🙂" in docx_text
    with ZipFile(io.BytesIO(docx)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels")
        numbering = archive.read("word/numbering.xml")
    ElementTree.fromstring(relationships)
    ElementTree.fromstring(numbering)
    assert "<w:b/>" in document_xml
    assert "<w:i/>" in document_xml
    assert 'w:ascii="Consolas"' in document_xml

    pdf = export_to_pdf(title, content, summary=summary)
    pdf_text, page_count = _pdf_text_and_page_count(pdf)
    assert page_count >= 1
    assert title in pdf_text
    assert "Straße" in pdf_text
    assert "Jörg & Zoë (ok) \\ <tag>" in pdf_text
    assert "€" in pdf_text


def test_stdlib_exports_accept_html_summary_without_legacy_renderers() -> None:
    summary = "<h2>Größe &amp; Qualität</h2><p>Die Straße bleibt offen.</p>"
    docx = export_to_docx(
        "Planungsprüfung",
        "Deutsch bleibt aktiv.",
        summary=summary,
        summary_format="html",
    )
    docx_text, _paragraph_count = _docx_text_and_paragraph_count(docx)
    assert "Größe & Qualität" in docx_text
    assert "Die Straße bleibt offen." in docx_text


def test_supported_pyautogui_injection_functions_do_not_require_pillow() -> None:
    probe = r"""
import importlib.abc
import json
import sys

class BlockPillow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PIL" or fullname.startswith("PIL."):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None

sys.meta_path.insert(0, BlockPillow())
import pyautogui
print(json.dumps({
    "getActiveWindowTitle": callable(pyautogui.getActiveWindowTitle),
    "hotkey": callable(pyautogui.hotkey),
    "write": callable(pyautogui.write),
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert completed.stderr == ""
    assert completed.stdout.splitlines()[-1] == (
        '{"getActiveWindowTitle": true, "hotkey": true, "write": true}'
    )

from __future__ import annotations

import io
from xml.etree import ElementTree
from zipfile import ZipFile

from src.export import TranscriptBlock, export_to_docx, export_to_pdf, parse_transcript_blocks

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _transcript_paragraphs(docx: bytes) -> list[list[tuple[str, bool]]]:
    """Return (text, bold) runs for every paragraph after the Transcript heading."""
    with ZipFile(io.BytesIO(docx)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[list[tuple[str, bool]]] = []
    for paragraph in root.iter(f"{W}p"):
        runs: list[tuple[str, bool]] = []
        for run in paragraph.iter(f"{W}r"):
            text = ""
            for child in run:
                if child.tag == f"{W}t":
                    text += child.text or ""
                elif child.tag == f"{W}br":
                    text += "\n"
            bold = run.find(f"{W}rPr/{W}b") is not None
            runs.append((text, bold))
        paragraphs.append(runs)
    headings = [index for index, runs in enumerate(paragraphs) if runs == [("Transcript", False)]]
    assert headings, "transcript heading missing"
    return [runs for runs in paragraphs[headings[-1] + 1 :] if runs]


def test_canonical_timestamped_speakers_group_consecutive_segments() -> None:
    content = (
        "[0:00] Speaker 0: Hallo zusammen.\n"
        "[0:04] Speaker 0: Wir beginnen.\n"
        "[0:09] Speaker 1: Danke.\n"
        "[75:03] Speaker 0: Später.\n"
        "[1:02:03] Speaker 1: Mit Stunden."
    )
    assert parse_transcript_blocks(content) == [
        TranscriptBlock("Hallo zusammen. Wir beginnen.", "0:00", 0, "Speaker 0"),
        TranscriptBlock("Danke.", "0:09", 9, "Speaker 1"),
        TranscriptBlock("Später.", "1:15:03", 4503, "Speaker 0"),
        TranscriptBlock("Mit Stunden.", "1:02:03", 3723, "Speaker 1"),
    ]
    assert _transcript_paragraphs(export_to_docx("Call", content)) == [
        [("[0:00] ", False), ("Speaker 0: ", True), ("Hallo zusammen. Wir beginnen.", False)],
        [("[0:09] ", False), ("Speaker 1: ", True), ("Danke.", False)],
        [("[1:15:03] ", False), ("Speaker 0: ", True), ("Später.", False)],
        [("[1:02:03] ", False), ("Speaker 1: ", True), ("Mit Stunden.", False)],
    ]


def test_canonical_timestamps_without_speakers_keep_text_and_untimed_line_breaks() -> None:
    content = "[0:05] Erster Satz.\nFortsetzung ohne Zeit\n[0:12] Zweiter Satz.\n\n[0:20] Neuer Absatz."
    blocks = parse_transcript_blocks(content)
    assert [(block.start_label, block.speaker, block.text) for block in blocks] == [
        ("0:05", None, "Erster Satz.\nFortsetzung ohne Zeit Zweiter Satz."),
        ("0:20", None, "Neuer Absatz."),
    ]
    assert _transcript_paragraphs(export_to_docx("Call", content)) == [
        [("[0:05] ", False), ("Erster Satz.\nFortsetzung ohne Zeit Zweiter Satz.", False)],
        [("[0:20] ", False), ("Neuer Absatz.", False)],
    ]


def test_named_labels_require_full_diarization_or_recurrence() -> None:
    lone = parse_transcript_blocks("[0:01] Here is the thing: it works.\n[0:05] Another line.")
    assert [block.speaker for block in lone] == [None]
    assert lone[0].text == "Here is the thing: it works. Another line."

    named = parse_transcript_blocks("[0:01] Alice: Hi.\n[0:03] You: Hello.\n[0:07] Alice: Bye.")
    assert [(block.speaker, block.text) for block in named] == [("Alice", "Hi."), ("You", "Hello."), ("Alice", "Bye.")]
    # Invalid clock values are text, not timestamps.
    assert parse_transcript_blocks("[0:75] Speaker 0: no") == [TranscriptBlock("[0:75] Speaker 0: no")]


def test_speaker_zero_is_a_real_speaker_in_docx_and_pdf() -> None:
    content = "[0:00] Speaker 0: Nullter Sprecher spricht.\n[0:03] Speaker 2: Antwort."
    paragraphs = _transcript_paragraphs(export_to_docx("Call", content))
    assert paragraphs[0] == [("[0:00] ", False), ("Speaker 0: ", True), ("Nullter Sprecher spricht.", False)]
    pdf = export_to_pdf("Call", content)
    assert pdf.startswith(b"%PDF")


def test_long_same_speaker_turns_split_into_continued_paragraphs() -> None:
    sentence = "Ein ziemlich langer Satz für die Absatzgrenze. "
    content = "\n".join(f"[0:{index:02d}] Speaker 1: {sentence.strip()}" for index in range(30))
    blocks = parse_transcript_blocks(content)
    assert len(blocks) > 1
    assert all(block.speaker == "Speaker 1" for block in blocks)
    assert [block.continued for block in blocks] == [False] + [True] * (len(blocks) - 1)
    assert sum(block.text.count("Absatzgrenze") for block in blocks) == 30
    assert all(len(block.text) < 700 + len(sentence) for block in blocks)
    paragraphs = _transcript_paragraphs(export_to_docx("Call", content))
    assert len(paragraphs) == len(blocks)
    assert paragraphs[0][1] == ("Speaker 1: ", True)
    assert all(all(not bold for _, bold in paragraph) for paragraph in paragraphs[1:])


def test_legacy_speaker_paragraphs_keep_bracketed_labels() -> None:
    content = "[Speaker 1]: Guten Tag.\n\n[Speaker 2]: Hallo\nzweite Zeile\n\nOhne Sprecher\n\n[Speaker x]: bleibt Text"
    blocks = parse_transcript_blocks(content)
    assert [(block.speaker, block.text) for block in blocks] == [
        ("Speaker 1", "Guten Tag."),
        ("Speaker 2", "Hallo\nzweite Zeile"),
        (None, "Ohne Sprecher"),
        (None, "[Speaker x]: bleibt Text"),
    ]
    assert _transcript_paragraphs(export_to_docx("Call", content)) == [
        [("[Speaker 1]: ", True), ("Guten Tag.", False)],
        [("[Speaker 2]: ", True), ("Hallo\nzweite Zeile", False)],
        [("Ohne Sprecher", False)],
        [("[Speaker x]: bleibt Text", False)],
    ]


def test_plain_text_preserves_line_breaks_and_paragraphs() -> None:
    content = "Erste Zeile\r\nzweite Zeile\n\n\nDritter Absatz  \n"
    assert parse_transcript_blocks(content) == [
        TranscriptBlock("Erste Zeile\nzweite Zeile"),
        TranscriptBlock("Dritter Absatz"),
    ]
    assert _transcript_paragraphs(export_to_docx("Notes", content)) == [
        [("Erste Zeile\nzweite Zeile", False)],
        [("Dritter Absatz", False)],
    ]
    assert parse_transcript_blocks("") == parse_transcript_blocks(" \n ") == []

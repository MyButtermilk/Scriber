"""Structured Meeting Workspace export and email templates."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr
import re
from typing import Any


def format_offset(milliseconds: int | None) -> str:
    seconds = max(0, int(milliseconds or 0) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _analysis(detail: dict[str, Any]) -> dict[str, Any]:
    return next(
        (
            item.get("payload", {})
            for item in detail.get("outputs", [])
            if item.get("kind") == "analysis" and item.get("status") == "completed"
        ),
        {},
    )


def _item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item).strip()
    return str(item.get("text") or item.get("summary") or item.get("title") or "").strip()


def _single_line(value: Any, *, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def meeting_duration_ms(detail: dict[str, Any]) -> int:
    return max((int(item.get("endMs", 0)) for item in detail.get("segments", [])), default=0)


def build_meeting_summary_markdown(detail: dict[str, Any]) -> str:
    analysis = _analysis(detail)
    lines: list[str] = []
    summary = str(analysis.get("executiveSummary") or "").strip()
    if summary:
        lines.extend(["## Executive summary", "", summary, ""])

    sections = (
        ("Decisions", analysis.get("decisions", [])),
        ("Action items", detail.get("actionItems", [])),
        ("Open questions", analysis.get("openQuestions", [])),
        ("Risks", analysis.get("risks", [])),
    )
    for heading, items in sections:
        if not isinstance(items, list) or not items:
            continue
        lines.extend([f"## {heading}", ""])
        for item in items:
            text = _item_text(item)
            if not text:
                continue
            metadata: list[str] = []
            if isinstance(item, dict):
                if item.get("owner"):
                    metadata.append(f"Owner: {item['owner']}")
                if item.get("dueDate"):
                    metadata.append(f"Due: {item['dueDate']}")
                if heading == "Action items":
                    metadata.append(f"Status: {item.get('status', 'open')}")
            suffix = f" ({'; '.join(metadata)})" if metadata else ""
            lines.append(f"- {text}{suffix}")
        lines.append("")

    notes = [item for item in detail.get("notes", []) if str(item.get("body", "")).strip()]
    if notes:
        lines.extend(["## Notes", ""])
        for note in notes:
            lines.append(f"- **{format_offset(note.get('atMs'))}:** {str(note['body']).strip()}")
        lines.append("")

    return "\n".join(lines).strip()


def build_meeting_transcript_text(detail: dict[str, Any]) -> str:
    paragraphs: list[str] = []
    for segment in detail.get("segments", []):
        start_ms, end_ms = int(segment.get("startMs", 0)), int(segment.get("endMs", 0))
        speaker = segment.get("speakerLabel") or segment.get("source") or "Meeting"
        timing_note = " (estimated timing)" if segment.get("alignmentQuality") == "estimated" else ""
        paragraphs.append(
            f"{format_offset(start_ms)} to {format_offset(end_ms)}{timing_note} | {speaker}\n"
            f"{str(segment.get('text', '')).strip()}"
        )
    return "\n\n".join(paragraphs)


def build_meeting_markdown(detail: dict[str, Any], *, include_transcript: bool = True) -> str:
    segments = detail.get("segments", [])
    estimated_count = sum(
        1 for segment in segments if segment.get("alignmentQuality") == "estimated"
    )
    lines = [
        f"# {detail.get('title') or 'Meeting'}",
        "",
        f"**Date:** {detail.get('startedAt') or detail.get('createdAt') or 'Unknown'}  ",
        f"**Duration:** {format_offset(meeting_duration_ms(detail))}  ",
        f"**Transcript:** {len(segments)} timestamped segments",
        *(
            [f"**Timing quality:** {estimated_count} segment(s) use estimated intervals"]
            if estimated_count else []
        ),
        "",
    ]
    summary_markdown = build_meeting_summary_markdown(detail)
    if summary_markdown:
        lines.extend([summary_markdown, ""])

    if include_transcript:
        lines.extend(["## Timestamped transcript", ""])
        for segment in segments:
            start_ms, end_ms = int(segment.get("startMs", 0)), int(segment.get("endMs", 0))
            speaker = segment.get("speakerLabel") or segment.get("source") or "Meeting"
            timing_note = " · estimated timing" if segment.get("alignmentQuality") == "estimated" else ""
            lines.extend([
                f"### {format_offset(start_ms)} → {format_offset(end_ms)} · {speaker}{timing_note}",
                "",
                str(segment.get("text", "")).strip(),
                "",
            ])
    return "\n".join(lines).strip() + "\n"


def meeting_email_recipients(detail: dict[str, Any]) -> list[dict[str, str]]:
    event = detail.get("captureMetadata", {}).get("calendarEvent", {})
    candidates = event.get("participants", []) if isinstance(event, dict) else []
    recipients: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        address = _single_line(candidate.get("address"), limit=320).lower()
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address) or address in seen:
            continue
        seen.add(address)
        recipients.append({"name": _single_line(candidate.get("name"), limit=200), "address": address})
    return recipients


def build_meeting_email(detail: dict[str, Any], *, attachment_name: str = "") -> dict[str, Any]:
    analysis = _analysis(detail)
    title = _single_line(detail.get("title") or "Meeting", limit=180) or "Meeting"
    subject = f"Meeting follow-up: {title}"
    lines = ["Hello,", "", f"Here is the follow-up for {title}.", ""]
    lines.extend([
        "Meeting details",
        f"Date: {detail.get('startedAt') or detail.get('createdAt') or 'Unknown'}",
        f"Duration: {format_offset(meeting_duration_ms(detail))}",
        "",
    ])
    summary = str(analysis.get("executiveSummary") or "").strip()
    if summary:
        lines.extend(["Summary", summary, ""])
    for heading, items in (
        ("Decisions", analysis.get("decisions", [])),
        ("Action items", detail.get("actionItems", [])),
        ("Open questions", analysis.get("openQuestions", [])),
    ):
        if isinstance(items, list) and items:
            lines.append(heading)
            for item in items:
                text = _item_text(item)
                if text:
                    owner = f" [{item.get('owner')}]" if isinstance(item, dict) and item.get("owner") else ""
                    due = f" (due {item.get('dueDate')})" if isinstance(item, dict) and item.get("dueDate") else ""
                    lines.append(f"- {text}{owner}{due}")
            lines.append("")
    if attachment_name:
        lines.extend([f"Attached: {attachment_name} contains the full timestamped transcript.", ""])
    else:
        lines.extend(["The full timestamped transcript remains available in Scriber.", ""])
    lines.append("Best regards")
    return {
        "recipients": meeting_email_recipients(detail),
        "subject": subject,
        "body": "\n".join(lines).strip(),
    }


def build_eml_draft(
    detail: dict[str, Any],
    *,
    attachment: bytes | None = None,
    attachment_name: str = "",
    attachment_type: str = "application/octet-stream",
) -> bytes:
    template = build_meeting_email(detail, attachment_name=attachment_name if attachment is not None else "")
    message = EmailMessage()
    message["Subject"] = template["subject"]
    recipients = template["recipients"]
    if recipients:
        message["To"] = ", ".join(formataddr((item["name"], item["address"])) for item in recipients)
    message.set_content(template["body"])
    if attachment is not None and attachment_name:
        maintype, subtype = (attachment_type.split("/", 1) + ["octet-stream"])[:2]
        message.add_attachment(attachment, maintype=maintype, subtype=subtype, filename=attachment_name)
    return message.as_bytes()

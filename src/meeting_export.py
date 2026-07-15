"""Structured Meeting Workspace export and email templates."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.utils import formataddr
import re
from typing import Any


_EXPORT_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "meeting": "Meeting", "unknown": "Unknown", "executive_summary": "Executive summary",
        "decisions": "Decisions", "action_items": "Action items", "open_questions": "Open questions",
        "risks": "Risks", "notes": "Notes", "owner": "Owner", "due": "Due", "status": "Status",
        "status_open": "open", "status_done": "done", "status_dismissed": "dismissed",
        "date": "Date", "duration": "Duration", "transcript": "Transcript",
        "timestamped_segments": "timestamped segments", "timing_quality": "Timing quality",
        "estimated_segments": "{count} segment(s) use estimated intervals",
        "timestamped_transcript": "Timestamped transcript", "estimated_timing": "estimated timing",
        "email_subject": "Meeting follow-up: {title}", "hello": "Hello,",
        "email_intro": "Here is the follow-up for {title}.", "meeting_details": "Meeting details",
        "summary": "Summary", "due_inline": "due {date}",
        "attached": "Attached: {name} contains the full timestamped transcript.",
        "available_in_scriber": "The full timestamped transcript remains available in Scriber.",
        "regards": "Best regards",
    },
    "de": {
        "meeting": "Besprechung", "unknown": "Unbekannt", "executive_summary": "Kurzfassung",
        "decisions": "Entscheidungen", "action_items": "Aufgaben", "open_questions": "Offene Fragen",
        "risks": "Risiken", "notes": "Notizen", "owner": "Verantwortlich", "due": "Fällig",
        "status": "Status", "status_open": "offen", "status_done": "erledigt",
        "status_dismissed": "verworfen", "date": "Datum", "duration": "Dauer",
        "transcript": "Transkript", "timestamped_segments": "Segmente mit Zeitstempeln",
        "timing_quality": "Zeitstempelqualität",
        "estimated_segments": "{count} Segment(e) verwenden geschätzte Zeitintervalle",
        "timestamped_transcript": "Transkript mit Zeitstempeln", "estimated_timing": "geschätzte Zeit",
        "email_subject": "Besprechungsnachbereitung: {title}", "hello": "Hallo,",
        "email_intro": "hier ist die Nachbereitung zu {title}.", "meeting_details": "Besprechungsdetails",
        "summary": "Zusammenfassung", "due_inline": "fällig am {date}",
        "attached": "Im Anhang: {name} enthält das vollständige Transkript mit Zeitstempeln.",
        "available_in_scriber": "Das vollständige Transkript mit Zeitstempeln bleibt in Scriber verfügbar.",
        "regards": "Viele Grüße",
    },
    "es": {
        "meeting": "Reunión", "unknown": "Desconocido", "executive_summary": "Resumen ejecutivo",
        "decisions": "Decisiones", "action_items": "Tareas", "open_questions": "Preguntas abiertas",
        "risks": "Riesgos", "notes": "Notas", "owner": "Responsable", "due": "Fecha límite",
        "status": "Estado", "status_open": "abierta", "status_done": "completada",
        "status_dismissed": "descartada", "date": "Fecha", "duration": "Duración",
        "transcript": "Transcripción", "timestamped_segments": "segmentos con marcas de tiempo",
        "timing_quality": "Calidad de las marcas de tiempo",
        "estimated_segments": "{count} segmento(s) usan intervalos estimados",
        "timestamped_transcript": "Transcripción con marcas de tiempo", "estimated_timing": "tiempo estimado",
        "email_subject": "Seguimiento de la reunión: {title}", "hello": "Hola,",
        "email_intro": "Aquí está el seguimiento de {title}.", "meeting_details": "Detalles de la reunión",
        "summary": "Resumen", "due_inline": "fecha límite {date}",
        "attached": "Adjunto: {name} contiene la transcripción completa con marcas de tiempo.",
        "available_in_scriber": "La transcripción completa con marcas de tiempo sigue disponible en Scriber.",
        "regards": "Saludos",
    },
    "fr": {
        "meeting": "Réunion", "unknown": "Inconnu", "executive_summary": "Synthèse",
        "decisions": "Décisions", "action_items": "Actions", "open_questions": "Questions ouvertes",
        "risks": "Risques", "notes": "Notes", "owner": "Responsable", "due": "Échéance",
        "status": "Statut", "status_open": "ouverte", "status_done": "terminée",
        "status_dismissed": "écartée", "date": "Date", "duration": "Durée",
        "transcript": "Transcription", "timestamped_segments": "segments horodatés",
        "timing_quality": "Qualité de l’horodatage",
        "estimated_segments": "{count} segment(s) utilisent des intervalles estimés",
        "timestamped_transcript": "Transcription horodatée", "estimated_timing": "horaire estimé",
        "email_subject": "Suivi de la réunion : {title}", "hello": "Bonjour,",
        "email_intro": "Voici le suivi de {title}.", "meeting_details": "Détails de la réunion",
        "summary": "Résumé", "due_inline": "échéance {date}",
        "attached": "Pièce jointe : {name} contient la transcription horodatée complète.",
        "available_in_scriber": "La transcription horodatée complète reste disponible dans Scriber.",
        "regards": "Cordialement",
    },
    "it": {
        "meeting": "Riunione", "unknown": "Sconosciuto", "executive_summary": "Sintesi",
        "decisions": "Decisioni", "action_items": "Attività", "open_questions": "Domande aperte",
        "risks": "Rischi", "notes": "Note", "owner": "Responsabile", "due": "Scadenza",
        "status": "Stato", "status_open": "aperta", "status_done": "completata",
        "status_dismissed": "scartata", "date": "Data", "duration": "Durata",
        "transcript": "Trascrizione", "timestamped_segments": "segmenti con marcatori temporali",
        "timing_quality": "Qualità dei marcatori temporali",
        "estimated_segments": "{count} segmento/i usa/usano intervalli stimati",
        "timestamped_transcript": "Trascrizione con marcatori temporali", "estimated_timing": "tempo stimato",
        "email_subject": "Seguito della riunione: {title}", "hello": "Buongiorno,",
        "email_intro": "Ecco il seguito di {title}.", "meeting_details": "Dettagli della riunione",
        "summary": "Riepilogo", "due_inline": "scadenza {date}",
        "attached": "In allegato: {name} contiene la trascrizione completa con marcatori temporali.",
        "available_in_scriber": "La trascrizione completa con marcatori temporali resta disponibile in Scriber.",
        "regards": "Cordiali saluti",
    },
}

_LANGUAGE_MARKERS = {
    "de": {"aber", "auch", "dass", "der", "die", "ein", "eine", "für", "ist", "mit", "nicht", "und", "von", "wir"},
    "en": {"and", "are", "for", "from", "have", "is", "not", "that", "the", "this", "to", "we", "with", "you"},
    "es": {"con", "de", "el", "es", "la", "las", "los", "para", "por", "que", "una", "y"},
    "fr": {"avec", "de", "des", "est", "et", "la", "le", "les", "nous", "pas", "pour", "que", "une"},
    "it": {"che", "con", "del", "di", "e", "il", "la", "le", "non", "per", "una", "è"},
}


def _language_code(value: Any) -> str:
    code = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in _EXPORT_LABELS else ""


def _conservative_transcript_language(detail: dict[str, Any]) -> str:
    text = " ".join(str(item.get("text") or "") for item in detail.get("segments", []))
    words = re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE)
    if len(words) < 20:
        return ""
    scores = sorted(
        ((sum(word in markers for word in words), language) for language, markers in _LANGUAGE_MARKERS.items()),
        reverse=True,
    )
    best_score, best_language = scores[0]
    runner_up = scores[1][0]
    return best_language if best_score >= 5 and best_score >= runner_up + 2 else ""


def meeting_export_language(detail: dict[str, Any], *, fallback_language: str = "en") -> str:
    analysis = _analysis(detail)
    for candidate in (
        _conservative_transcript_language(detail),
        analysis.get("outputLanguage"),
        detail.get("language"),
        fallback_language,
        "en",
    ):
        code = _language_code(candidate)
        if code:
            return code
    return "en"


def meeting_export_labels(detail: dict[str, Any], *, fallback_language: str = "en") -> dict[str, str]:
    return _EXPORT_LABELS[meeting_export_language(detail, fallback_language=fallback_language)]


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


def build_meeting_summary_markdown(
    detail: dict[str, Any], *, fallback_language: str = "en"
) -> str:
    analysis = _analysis(detail)
    labels = meeting_export_labels(detail, fallback_language=fallback_language)
    lines: list[str] = []
    summary = str(analysis.get("executiveSummary") or "").strip()
    if summary:
        lines.extend([f"## {labels['executive_summary']}", "", summary, ""])

    sections = (
        (labels["decisions"], "decisions", analysis.get("decisions", [])),
        (labels["action_items"], "action_items", detail.get("actionItems", [])),
        (labels["open_questions"], "open_questions", analysis.get("openQuestions", [])),
        (labels["risks"], "risks", analysis.get("risks", [])),
    )
    for heading, section_key, items in sections:
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
                    metadata.append(f"{labels['owner']}: {item['owner']}")
                if item.get("dueDate"):
                    metadata.append(f"{labels['due']}: {item['dueDate']}")
                if section_key == "action_items":
                    raw_status = str(item.get("status") or "open").strip().lower()
                    status = labels.get(f"status_{raw_status}", raw_status)
                    metadata.append(f"{labels['status']}: {status}")
            suffix = f" ({'; '.join(metadata)})" if metadata else ""
            lines.append(f"- {text}{suffix}")
        lines.append("")

    notes = [item for item in detail.get("notes", []) if str(item.get("body", "")).strip()]
    if notes:
        lines.extend([f"## {labels['notes']}", ""])
        for note in notes:
            lines.append(f"- **{format_offset(note.get('atMs'))}:** {str(note['body']).strip()}")
        lines.append("")

    return "\n".join(lines).strip()


def build_meeting_transcript_text(
    detail: dict[str, Any], *, fallback_language: str = "en"
) -> str:
    labels = meeting_export_labels(detail, fallback_language=fallback_language)
    paragraphs: list[str] = []
    for segment in detail.get("segments", []):
        start_ms, end_ms = int(segment.get("startMs", 0)), int(segment.get("endMs", 0))
        speaker = segment.get("speakerLabel") or segment.get("source") or labels["meeting"]
        timing_note = (
            f" ({labels['estimated_timing']})"
            if segment.get("alignmentQuality") == "estimated" else ""
        )
        paragraphs.append(
            f"{format_offset(start_ms)} to {format_offset(end_ms)}{timing_note} | {speaker}\n"
            f"{str(segment.get('text', '')).strip()}"
        )
    return "\n\n".join(paragraphs)


def build_meeting_markdown(
    detail: dict[str, Any], *, include_transcript: bool = True, fallback_language: str = "en"
) -> str:
    labels = meeting_export_labels(detail, fallback_language=fallback_language)
    segments = detail.get("segments", [])
    estimated_count = sum(
        1 for segment in segments if segment.get("alignmentQuality") == "estimated"
    )
    lines = [
        f"# {detail.get('title') or labels['meeting']}",
        "",
        f"**{labels['date']}:** {detail.get('startedAt') or detail.get('createdAt') or labels['unknown']}  ",
        f"**{labels['duration']}:** {format_offset(meeting_duration_ms(detail))}  ",
        f"**{labels['transcript']}:** {len(segments)} {labels['timestamped_segments']}",
        *(
            [f"**{labels['timing_quality']}:** {labels['estimated_segments'].format(count=estimated_count)}"]
            if estimated_count else []
        ),
        "",
    ]
    summary_markdown = build_meeting_summary_markdown(
        detail, fallback_language=fallback_language
    )
    if summary_markdown:
        lines.extend([summary_markdown, ""])

    if include_transcript:
        lines.extend([f"## {labels['timestamped_transcript']}", ""])
        for segment in segments:
            start_ms, end_ms = int(segment.get("startMs", 0)), int(segment.get("endMs", 0))
            speaker = segment.get("speakerLabel") or segment.get("source") or labels["meeting"]
            timing_note = (
                f" · {labels['estimated_timing']}"
                if segment.get("alignmentQuality") == "estimated" else ""
            )
            lines.extend([
                f"### {format_offset(start_ms)} → {format_offset(end_ms)} · {speaker}{timing_note}",
                "",
                str(segment.get("text", "")).strip(),
                "",
            ])
    return "\n".join(lines).strip() + "\n"


def meeting_email_recipients(detail: dict[str, Any]) -> list[dict[str, str]]:
    event = detail.get("captureMetadata", {}).get("calendarEvent", {})
    raw_participants = event.get("participants") if isinstance(event, dict) else None
    participants = raw_participants if isinstance(raw_participants, list) else []
    candidates = [event.get("organizer"), *participants] if isinstance(event, dict) else []
    current_user = event.get("currentUser") if isinstance(event, dict) else None
    current_user_addresses: set[str] = set()
    if isinstance(current_user, dict):
        raw_aliases = current_user.get("aliases")
        aliases = raw_aliases if isinstance(raw_aliases, list) else []
        for value in [current_user.get("address"), *aliases]:
            address = _single_line(value, limit=320).casefold()
            if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address):
                current_user_addresses.add(address)
    recipients: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        attendee_type = _single_line(
            candidate.get("type") or candidate.get("role"), limit=40
        ).casefold()
        response = _single_line(
            candidate.get("response") or candidate.get("responseStatus"), limit=40
        ).casefold()
        # Calendar rooms/resources are not people, declined attendees should not
        # receive an unsolicited recap, and the connected account should not be
        # addressed in its own follow-up draft. Older Meeting snapshots omit
        # these fields and intentionally retain the previous include behavior.
        if (
            bool(candidate.get("isCurrentUser"))
            or attendee_type == "resource"
            or response in {"declined", "decline"}
        ):
            continue
        address = _single_line(candidate.get("address"), limit=320).lower()
        if (
            not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address)
            or address in seen
            or address.casefold() in current_user_addresses
        ):
            continue
        seen.add(address)
        recipients.append({"name": _single_line(candidate.get("name"), limit=200), "address": address})
    return recipients


def build_meeting_email(
    detail: dict[str, Any], *, attachment_name: str = "", fallback_language: str = "en"
) -> dict[str, Any]:
    analysis = _analysis(detail)
    labels = meeting_export_labels(detail, fallback_language=fallback_language)
    title = _single_line(detail.get("title") or labels["meeting"], limit=180) or labels["meeting"]
    subject = labels["email_subject"].format(title=title)
    lines = [labels["hello"], "", labels["email_intro"].format(title=title), ""]
    lines.extend([
        labels["meeting_details"],
        f"{labels['date']}: {detail.get('startedAt') or detail.get('createdAt') or labels['unknown']}",
        f"{labels['duration']}: {format_offset(meeting_duration_ms(detail))}",
        "",
    ])
    summary = str(analysis.get("executiveSummary") or "").strip()
    if summary:
        lines.extend([labels["summary"], summary, ""])
    for heading, items in (
        (labels["decisions"], analysis.get("decisions", [])),
        (labels["action_items"], detail.get("actionItems", [])),
        (labels["open_questions"], analysis.get("openQuestions", [])),
    ):
        if isinstance(items, list) and items:
            lines.append(heading)
            for item in items:
                text = _item_text(item)
                if text:
                    owner = f" [{item.get('owner')}]" if isinstance(item, dict) and item.get("owner") else ""
                    due = (
                        f" ({labels['due_inline'].format(date=item.get('dueDate'))})"
                        if isinstance(item, dict) and item.get("dueDate") else ""
                    )
                    lines.append(f"- {text}{owner}{due}")
            lines.append("")
    if attachment_name:
        lines.extend([labels["attached"].format(name=attachment_name), ""])
    else:
        lines.extend([labels["available_in_scriber"], ""])
    lines.append(labels["regards"])
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
    fallback_language: str = "en",
) -> bytes:
    template = build_meeting_email(
        detail,
        attachment_name=attachment_name if attachment is not None else "",
        fallback_language=fallback_language,
    )
    message = EmailMessage()
    # Outlook treats RFC 822 files without this marker as received messages.
    # Marking the message as unsent opens an editable draft and preserves the
    # selected MIME attachments when the saved .eml file is opened.
    message["X-Unsent"] = "1"
    message["Subject"] = template["subject"]
    recipients = template["recipients"]
    if recipients:
        message["To"] = ", ".join(formataddr((item["name"], item["address"])) for item in recipients)
    # Keep the container itself ASCII-safe. New Outlook has had regressions
    # when opening X-Unsent drafts containing raw UTF-8/8bit MIME bodies; a
    # standards-compliant quoted-printable UTF-8 body retains every locale
    # while avoiding that parser edge case.
    message.set_content(template["body"], cte="quoted-printable")
    if attachment is not None and attachment_name:
        maintype, subtype = (attachment_type.split("/", 1) + ["octet-stream"])[:2]
        message.add_attachment(attachment, maintype=maintype, subtype=subtype, filename=attachment_name)
    # Windows mail clients are stricter than Python's parser about RFC 5322
    # line endings. The old path emitted LF-only bytes; SMTP policy guarantees
    # the CRLF framing expected by Outlook's MIME importer.
    return message.as_bytes(policy=policy.SMTP)

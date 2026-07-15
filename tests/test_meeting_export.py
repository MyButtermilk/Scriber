from __future__ import annotations

import copy
from email import policy
from email.parser import BytesParser

from src.meeting_export import (
    build_eml_draft,
    build_meeting_email,
    build_meeting_markdown,
    build_meeting_summary_markdown,
    build_meeting_transcript_text,
    meeting_email_recipients,
    meeting_export_labels,
    meeting_export_language,
)


def meeting_detail() -> dict:
    return {
        "id": "meeting-export",
        "title": "Roadmap review\r\nBcc: ignored@example.com",
        "startedAt": "2026-07-11T09:00:00Z",
        "createdAt": "2026-07-11T08:59:00Z",
        "captureMetadata": {
            "calendarEvent": {
                "organizer": {
                    "name": "Olivia Organizer",
                    "address": "OWNER@example.com",
                },
                "currentUser": {
                    "name": "Current User",
                    "address": "current.user@primary.example",
                    "aliases": [
                        "current.user@primary.example",
                        "current.user@example.com",
                    ],
                },
                "participants": [
                    {"name": "Márta Example", "address": "MARTA@example.com"},
                    {"name": "Duplicate", "address": "marta@example.com"},
                    {"name": "Invalid", "address": "not-an-email"},
                    {"name": "Header\r\nInjection", "address": "safe@example.org"},
                    {
                        "name": "Current User",
                        "address": "current.user@example.com",
                        "type": "required",
                        "response": "accepted",
                    },
                    {
                        "name": "Board room",
                        "address": "board-room@example.com",
                        "type": "resource",
                        "response": "accepted",
                    },
                    {
                        "name": "Declined participant",
                        "address": "declined@example.com",
                        "type": "optional",
                        "response": "declined",
                    },
                ]
            }
        },
        # Confirmed mappings improve Meeting-local labels only. They never add
        # an address that was not part of the frozen Outlook event.
        "speakers": [{
            "confirmedAttendee": {
                "name": "Not an event recipient",
                "address": "mapping-only@example.net",
            }
        }],
        "segments": [
            {
                "id": "segment-1", "source": "microphone", "speakerLabel": "Alex",
                "startMs": 1_000, "endMs": 4_200, "durationMs": 3_200,
                "text": "We approved the Friday release.",
            },
            {
                "id": "segment-2", "source": "system", "speakerLabel": "Márta",
                "startMs": 5_000, "endMs": 8_250, "durationMs": 3_250,
                "text": "I will send the customer update.",
            },
        ],
        "notes": [{"id": "note-1", "atMs": 6_000, "body": "Confirm release owner."}],
        "actionItems": [{
            "id": "action-1", "text": "Send the customer update", "owner": "Márta",
            "dueDate": "2026-07-12", "status": "open",
        }],
        "outputs": [{
            "kind": "analysis", "status": "completed", "payload": {
                "executiveSummary": "The team approved a Friday release.",
                "decisions": [{"text": "Release on Friday"}],
                "openQuestions": [{"text": "Who monitors deployment?"}],
                "risks": [{"text": "Customer approval is pending"}],
            },
        }],
    }


def test_meeting_templates_keep_summary_and_timestamped_transcript_distinct():
    detail = meeting_detail()
    summary = build_meeting_summary_markdown(detail)
    transcript = build_meeting_transcript_text(detail)
    markdown = build_meeting_markdown(detail)

    assert summary.startswith("## Executive summary")
    assert "# Roadmap review" not in summary
    assert "## Action items" in summary
    assert "Owner: Márta; Due: 2026-07-12; Status: open" in summary
    assert transcript == (
        "0:01 to 0:04 | Alex\nWe approved the Friday release.\n\n"
        "0:05 to 0:08 | Márta\nI will send the customer update."
    )
    assert "**Duration:** 0:08" in markdown
    assert "### 0:01 → 0:04 · Alex" in markdown
    assert markdown.count("# Roadmap review") == 1


def test_email_template_uses_unique_valid_outlook_participants_without_false_attachment_claim():
    detail = meeting_detail()
    recipients = meeting_email_recipients(detail)
    template = build_meeting_email(detail)

    assert recipients == [
        {"name": "Olivia Organizer", "address": "owner@example.com"},
        {"name": "Márta Example", "address": "marta@example.com"},
        {"name": "Header Injection", "address": "safe@example.org"},
    ]
    assert "\r" not in template["subject"] and "\n" not in template["subject"]
    assert "Bcc: ignored@example.com" in template["subject"]
    assert "Duration: 0:08" in template["body"]
    assert "The full timestamped transcript remains available in Scriber." in template["body"]
    assert "The attached meeting document" not in template["body"]


def test_eml_draft_is_rfc822_parseable_and_carries_selected_attachment():
    payload = build_eml_draft(
        meeting_detail(),
        attachment=b"# Meeting attachment\n",
        attachment_name="Roadmap review.md",
        attachment_type="text/markdown",
    )
    message = BytesParser(policy=policy.default).parsebytes(payload)

    assert message["X-Unsent"] == "1"
    assert b"\r\n" in payload
    assert b"\n" not in payload.replace(b"\r\n", b"")
    assert message["Bcc"] is None
    assert "owner@example.com" in str(message["To"])
    assert "marta@example.com" in str(message["To"])
    assert "safe@example.org" in str(message["To"])
    assert "current.user@example.com" not in str(message["To"])
    assert "mapping-only@example.net" not in str(message["To"])
    assert "Attached: Roadmap review.md" in message.get_body(preferencelist=("plain",)).get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Roadmap review.md"
    assert attachments[0].get_content_type() == "text/markdown"
    assert attachments[0].get_payload(decode=True) == b"# Meeting attachment\n"


def test_eml_body_only_has_no_attachment_or_false_attachment_claim():
    payload = build_eml_draft(meeting_detail())
    message = BytesParser(policy=policy.default).parsebytes(payload)

    assert message["X-Unsent"] == "1"
    assert list(message.iter_attachments()) == []
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "The full timestamped transcript remains available in Scriber." in body
    assert "Attached:" not in body


def test_meeting_exports_follow_analysis_output_language_including_email_labels():
    detail = copy.deepcopy(meeting_detail())
    analysis = next(item for item in detail["outputs"] if item["kind"] == "analysis")
    analysis["payload"]["outputLanguage"] = "de"
    analysis["payload"]["executiveSummary"] = "Die Veröffentlichung wurde beschlossen."

    assert meeting_export_language(detail, fallback_language="en") == "de"
    assert meeting_export_labels(detail)["transcript"] == "Transkript"
    markdown = build_meeting_markdown(detail, fallback_language="en")
    assert "## Kurzfassung" in markdown
    assert "## Entscheidungen" in markdown
    assert "## Transkript mit Zeitstempeln" in markdown
    assert "**Datum:**" in markdown
    assert "**Dauer:**" in markdown

    email = build_meeting_email(detail, attachment_name="Besprechung.pdf")
    assert email["subject"].startswith("Besprechungsnachbereitung:")
    assert "Zusammenfassung\nDie Veröffentlichung wurde beschlossen." in email["body"]
    assert "Entscheidungen" in email["body"]
    assert "Im Anhang: Besprechung.pdf" in email["body"]
    assert "Best regards" not in email["body"]

    eml = build_eml_draft(
        detail,
        attachment=b"%PDF-localized",
        attachment_name="Besprechung.pdf",
        attachment_type="application/pdf",
    )
    # Headers, quoted-printable body, and base64 attachment remain ASCII-safe
    # even though the decoded draft contains umlauts and localized text.
    eml.decode("ascii")
    parsed = BytesParser(policy=policy.default).parsebytes(eml)
    assert "Die Veröffentlichung wurde beschlossen." in parsed.get_body(
        preferencelist=("plain",)
    ).get_content()
    assert list(parsed.iter_attachments())[0].get_payload(decode=True) == b"%PDF-localized"


def test_meeting_export_uses_concrete_settings_language_only_when_language_is_unknown():
    detail = copy.deepcopy(meeting_detail())
    detail["language"] = "auto"
    analysis = next(item for item in detail["outputs"] if item["kind"] == "analysis")
    analysis["payload"].pop("outputLanguage", None)
    # This short fixture is intentionally below the conservative heuristic's
    # evidence threshold, so the configured language becomes the fallback.
    assert meeting_export_language(detail, fallback_language="de") == "de"
    assert build_meeting_email(detail, fallback_language="de")["subject"].startswith(
        "Besprechungsnachbereitung:"
    )


def test_transcript_language_overrides_stale_analysis_and_settings_language():
    detail = copy.deepcopy(meeting_detail())
    detail["language"] = "en"
    analysis = next(item for item in detail["outputs"] if item["kind"] == "analysis")
    analysis["payload"]["outputLanguage"] = "en"
    detail["segments"] = [{
        "id": "segment-de",
        "source": "microphone",
        "speakerLabel": "Alex",
        "startMs": 0,
        "endMs": 12_000,
        "durationMs": 12_000,
        "text": (
            "Wir haben heute die Planung besprochen und die wichtigsten Aufgaben "
            "für das Team festgelegt. Die Veröffentlichung ist am Freitag, aber "
            "wir prüfen auch noch, dass der Kunde mit dem Ergebnis zufrieden ist."
        ),
    }]

    assert meeting_export_language(detail, fallback_language="en") == "de"
    assert build_meeting_email(detail)["subject"].startswith(
        "Besprechungsnachbereitung:"
    )

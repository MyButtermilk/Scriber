from __future__ import annotations

from email import policy
from email.parser import BytesParser

from src.meeting_export import (
    build_eml_draft,
    build_meeting_email,
    build_meeting_markdown,
    build_meeting_summary_markdown,
    build_meeting_transcript_text,
    meeting_email_recipients,
)


def meeting_detail() -> dict:
    return {
        "id": "meeting-export",
        "title": "Roadmap review\r\nBcc: ignored@example.com",
        "startedAt": "2026-07-11T09:00:00Z",
        "createdAt": "2026-07-11T08:59:00Z",
        "captureMetadata": {
            "calendarEvent": {
                "participants": [
                    {"name": "Márta Example", "address": "MARTA@example.com"},
                    {"name": "Duplicate", "address": "marta@example.com"},
                    {"name": "Invalid", "address": "not-an-email"},
                    {"name": "Header\r\nInjection", "address": "safe@example.org"},
                ]
            }
        },
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

    assert message["Bcc"] is None
    assert "marta@example.com" in str(message["To"])
    assert "safe@example.org" in str(message["To"])
    assert "Attached: Roadmap review.md" in message.get_body(preferencelist=("plain",)).get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Roadmap review.md"
    assert attachments[0].get_content_type() == "text/markdown"
    assert attachments[0].get_payload(decode=True) == b"# Meeting attachment\n"

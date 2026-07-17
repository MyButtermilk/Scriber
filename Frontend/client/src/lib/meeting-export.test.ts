import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_NATIVE_MEETING_EXPORT_BYTES,
  meetingAudioExportMeetingId,
  meetingEmailDraftPath,
  meetingExportApiPath,
  meetingExportDownloadErrorMessage,
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
  meetingExportFolderName,
  meetingExportNativeCommandError,
  meetingExportNativeLimitErrorMessage,
} from "./meeting-export-utils";

test("meeting export reads encoded and plain response filenames", () => {
  assert.equal(
    meetingExportFilename(
      "attachment; filename*=UTF-8''Weekly%20planning.pdf",
      "Meeting.pdf",
    ),
    "Weekly planning.pdf",
  );
  assert.equal(
    meetingExportFilename('attachment; filename="Fallback.docx"', "Meeting.docx"),
    "Fallback.docx",
  );
  assert.equal(meetingExportFilename(null, "Meeting.md"), "Meeting.md");
});

test("meeting export only accepts the supported save formats", () => {
  assert.equal(meetingExportExtension("Meeting.PDF"), "pdf");
  assert.equal(meetingExportExtension("Meeting.eml"), "eml");
  assert.equal(meetingExportExtension("Meeting.opus"), "opus");
  assert.equal(meetingExportExtension("Meeting.exe"), "pdf");
});

test("meeting export presents a human folder name", () => {
  assert.equal(meetingExportFolderName("C:\\Users\\Alex\\Documents"), "Documents");
  assert.equal(meetingExportFolderName("/home/alex/Downloads/"), "Downloads");
  assert.equal(meetingExportFolderName(""), null);
});

test("meeting export only fetches scoped meeting export endpoints", () => {
  assert.equal(
    meetingExportApiPath("/api/meetings/meeting_123/export/docx"),
    "/api/meetings/meeting_123/export/docx",
  );
  assert.equal(
    meetingExportApiPath("/api/meetings/abc123/export-email?attachment=pdf"),
    "/api/meetings/abc123/export-email?attachment=pdf",
  );
  assert.equal(
    meetingExportApiPath("/api/meetings/abc123/export/audio"),
    "/api/meetings/abc123/export/audio",
  );
  assert.equal(meetingAudioExportMeetingId("/api/meetings/abc123/export/audio"), "abc123");
  assert.equal(meetingAudioExportMeetingId("/api/meetings/abc123/export/pdf"), null);
  const localizedAddressError = {
    message: "Diese Meeting-Exportadresse ist nicht zulässig.",
  };
  assert.throws(
    () => meetingExportApiPath("https://example.com/report.pdf"),
    localizedAddressError,
  );
  assert.throws(() => meetingExportApiPath("/api/runtime/support-bundle"), localizedAddressError);
  assert.throws(
    () => meetingExportApiPath("/api/meetings/../secrets/export/pdf"),
    localizedAddressError,
  );
});

test("meeting email draft path preserves body-only and every selected attachment", () => {
  assert.equal(meetingEmailDraftPath("meeting_123", ""), "/api/meetings/meeting_123/export-email");
  assert.equal(
    meetingEmailDraftPath("meeting_123", "md"),
    "/api/meetings/meeting_123/export-email?attachment=md",
  );
  assert.equal(
    meetingEmailDraftPath("meeting_123", "pdf"),
    "/api/meetings/meeting_123/export-email?attachment=pdf",
  );
  assert.equal(
    meetingEmailDraftPath("meeting_123", "docx"),
    "/api/meetings/meeting_123/export-email?attachment=docx",
  );
  assert.throws(() => meetingEmailDraftPath("../secrets", "pdf"));
});

test("native meeting export size limit matches the Rust command", () => {
  assert.equal(meetingExportFitsNativeLimit(0), true);
  assert.equal(meetingExportFitsNativeLimit(MAX_NATIVE_MEETING_EXPORT_BYTES), true);
  assert.equal(meetingExportFitsNativeLimit(MAX_NATIVE_MEETING_EXPORT_BYTES + 1), false);
  assert.equal(meetingExportFitsNativeLimit(Number.NaN), false);
});

test("meeting export browser failures are localized and expose only safe HTTP status codes", () => {
  assert.equal(
    meetingExportDownloadErrorMessage(),
    "Der Meeting-Export konnte nicht heruntergeladen werden. Versuche es erneut.",
  );
  assert.equal(
    meetingExportDownloadErrorMessage(503),
    "Der Meeting-Export konnte nicht heruntergeladen werden (HTTP 503). Versuche es erneut.",
  );
  assert.equal(
    meetingExportDownloadErrorMessage(999),
    "Der Meeting-Export konnte nicht heruntergeladen werden. Versuche es erneut.",
  );
  assert.equal(
    meetingExportNativeLimitErrorMessage(),
    "Der Meeting-Export überschreitet die Desktop-Speichergrenze von 64 MiB.",
  );
});

test("native meeting export errors require a scoped code and a path-free message", () => {
  assert.equal(
    meetingExportNativeCommandError(
      {
        code: "meeting_export_open_failed",
        message: "Scriber konnte die gespeicherte Datei nicht öffnen.",
      },
      "Scriber could not open the saved file.",
    ).message,
    "Scriber konnte die gespeicherte Datei nicht öffnen.",
  );
  assert.equal(
    meetingExportNativeCommandError(
      {
        code: "meeting_export_open_failed",
        message: "Could not open C:\\Users\\Alex\\secret.pdf",
      },
      "Scriber could not open the saved file.",
    ).message,
    "Scriber konnte die gespeicherte Datei nicht öffnen.",
  );
  assert.equal(
    meetingExportNativeCommandError(
      { code: "unscoped_error", message: "internal failure" },
      "Scriber could not open the folder.",
    ).message,
    "Scriber konnte den Ordner nicht öffnen.",
  );
});

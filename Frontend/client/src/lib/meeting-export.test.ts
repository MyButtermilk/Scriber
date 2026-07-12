import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_NATIVE_MEETING_EXPORT_BYTES,
  meetingExportApiPath,
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
  meetingExportFolderName,
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
  assert.equal(meetingExportExtension("Meeting.exe"), "pdf");
});

test("meeting export presents a human folder name", () => {
  assert.equal(meetingExportFolderName("C:\\Users\\Alex\\Documents"), "Documents");
  assert.equal(meetingExportFolderName("/home/alex/Downloads/"), "Downloads");
  assert.equal(meetingExportFolderName(""), "the folder you chose");
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
  assert.throws(() => meetingExportApiPath("https://example.com/report.pdf"));
  assert.throws(() => meetingExportApiPath("/api/runtime/support-bundle"));
  assert.throws(() => meetingExportApiPath("/api/meetings/../secrets/export/pdf"));
});

test("native meeting export size limit matches the Rust command", () => {
  assert.equal(meetingExportFitsNativeLimit(0), true);
  assert.equal(meetingExportFitsNativeLimit(MAX_NATIVE_MEETING_EXPORT_BYTES), true);
  assert.equal(meetingExportFitsNativeLimit(MAX_NATIVE_MEETING_EXPORT_BYTES + 1), false);
  assert.equal(meetingExportFitsNativeLimit(Number.NaN), false);
});

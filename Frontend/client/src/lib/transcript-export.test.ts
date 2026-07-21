import assert from "node:assert/strict";
import test from "node:test";

import {
  transcriptExportApiPath,
  transcriptExportDownloadErrorMessage,
} from "./transcript-export-utils";

test("transcript export paths are restricted to one transcript and supported formats", () => {
  assert.equal(
    transcriptExportApiPath("/api/transcripts/youtube_123/export/pdf"),
    "/api/transcripts/youtube_123/export/pdf",
  );
  assert.equal(
    transcriptExportApiPath("/api/transcripts/123e4567-e89b-12d3-a456-426614174000/export/docx"),
    "/api/transcripts/123e4567-e89b-12d3-a456-426614174000/export/docx",
  );
  assert.throws(() => transcriptExportApiPath("https://example.com/report.pdf"));
  assert.throws(() => transcriptExportApiPath("/api/transcripts/../settings/export/pdf"));
  assert.throws(() => transcriptExportApiPath("/api/transcripts/abc/export/exe"));
});

test("transcript download errors expose only a bounded HTTP status", () => {
  assert.ok(transcriptExportDownloadErrorMessage().length > 0);
  assert.match(transcriptExportDownloadErrorMessage(503), /503/);
  assert.doesNotMatch(transcriptExportDownloadErrorMessage(999), /999/);
});

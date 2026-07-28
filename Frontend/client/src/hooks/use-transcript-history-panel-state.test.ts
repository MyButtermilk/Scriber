import assert from "node:assert/strict";
import test from "node:test";

import { parseTranscriptHistoryViewMode, serializeTranscriptHistorySearch } from "./use-transcript-history-panel-state";

test("history view mode accepts only supported layouts", () => {
  assert.equal(parseTranscriptHistoryViewMode("list", "grid"), "list");
  assert.equal(parseTranscriptHistoryViewMode("grid", "list"), "grid");
  assert.equal(parseTranscriptHistoryViewMode("tiles", "grid"), "grid");
  assert.equal(parseTranscriptHistoryViewMode(null, "list"), "list");
});

test("history search keeps meaningful text out of empty URLs", () => {
  assert.equal(serializeTranscriptHistorySearch("  meeting notes  "), "meeting notes");
  assert.equal(serializeTranscriptHistorySearch("   "), null);
});

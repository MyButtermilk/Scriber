import assert from "node:assert/strict";
import test from "node:test";

import { formatOutlookSyncMoment } from "./outlook-calendar-display";

test("Outlook sync time distinguishes today, yesterday, and older calendar data", () => {
  const now = new Date(2026, 6, 15, 12, 0);

  assert.match(
    formatOutlookSyncMoment(new Date(2026, 6, 15, 9, 5).toISOString(), now, "en-US") ?? "",
    /^today at /,
  );
  assert.match(
    formatOutlookSyncMoment(new Date(2026, 6, 14, 18, 30).toISOString(), now, "en-US") ?? "",
    /^yesterday at /,
  );
  assert.match(
    formatOutlookSyncMoment(new Date(2026, 6, 10, 8, 0).toISOString(), now, "en-US") ?? "",
    /^Jul 10 at /,
  );
});

test("Outlook sync time hides missing or invalid timestamps", () => {
  assert.equal(formatOutlookSyncMoment(""), null);
  assert.equal(formatOutlookSyncMoment("not-a-date"), null);
});

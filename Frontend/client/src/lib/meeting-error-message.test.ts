import assert from "node:assert/strict";
import test from "node:test";

import { localizeMeetingErrorMessage } from "./meeting-error-message";

const translate = (source: string, values?: Record<string, unknown>) => (
  source.replace("{{seconds}}", String(values?.seconds ?? ""))
);

test("localizes runtime meeting analysis timeout values through stable catalog keys", () => {
  assert.equal(
    localizeMeetingErrorMessage(
      "Text generation timed out after 240s. Please try again.",
      translate,
    ),
    "Text generation timed out after 240s. Please try again.",
  );
  assert.equal(
    localizeMeetingErrorMessage(
      "Summarization timed out after 90.5s. Please try again.",
      translate,
    ),
    "Summarization timed out after 90.5s. Please try again.",
  );
});

test("preserves normal meeting errors for the regular translation lookup", () => {
  assert.equal(
    localizeMeetingErrorMessage("The meeting failed.", translate),
    "The meeting failed.",
  );
});

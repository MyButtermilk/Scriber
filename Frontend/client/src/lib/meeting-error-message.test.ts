import assert from "node:assert/strict";
import test from "node:test";

import { localizeMeetingErrorMessage, meetingAnalysisFailurePresentation } from "./meeting-error-message";

const translate = (source: string, values?: Record<string, unknown>) =>
  source.replace("{{seconds}}", String(values?.seconds ?? ""));

test("localizes runtime meeting analysis timeout values through stable catalog keys", () => {
  assert.equal(
    localizeMeetingErrorMessage("Text generation timed out after 240s. Please try again.", translate),
    "Text generation timed out after 240s. Please try again.",
  );
  assert.equal(
    localizeMeetingErrorMessage("Summarization timed out after 90.5s. Please try again.", translate),
    "Summarization timed out after 90.5s. Please try again.",
  );
});

test("preserves normal meeting errors for the regular translation lookup", () => {
  assert.equal(localizeMeetingErrorMessage("The meeting failed.", translate), "The meeting failed.");
});

test("turns an incomplete meeting analysis into safe, actionable guidance", () => {
  const presentation = meetingAnalysisFailurePresentation(
    "meeting_analysis_incomplete_response",
    "minimax hit max_tokens with private provider diagnostics",
    translate,
  );

  assert.equal(presentation.title, "The meeting brief could not be completed.");
  assert.equal(
    presentation.reason,
    "The AI service did not return a complete response for every part of this meeting.",
  );
  assert.match(presentation.safety, /transcript, recording, speaker names, and notes are saved/i);
  assert.match(presentation.retryGuidance, /Try meeting brief again/);
  assert.match(presentation.settingsGuidance, /another summary model/);
  assert.equal(JSON.stringify(presentation).includes("minimax"), false);
  assert.equal(JSON.stringify(presentation).includes("max_tokens"), false);
});

test("hides legacy provider errors behind generic meeting-analysis guidance", () => {
  const presentation = meetingAnalysisFailurePresentation(
    "RuntimeError",
    "gemini-3.1-pro-preview summarization failed and the OpenRouter fallback also failed.",
    translate,
  );

  assert.equal(presentation.reason, "The AI service could not finish the meeting brief.");
  assert.equal(JSON.stringify(presentation).includes("gemini-3.1-pro-preview"), false);
});

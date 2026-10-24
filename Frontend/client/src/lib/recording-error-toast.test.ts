import assert from "node:assert/strict";
import test from "node:test";

import { failedSessionToastMessage, rememberReportedErrorSession } from "@/lib/recording-error-toast";

const fallback = { title: "Recording Error", message: "Live mic transcription failed." };

function finished(session: Record<string, unknown>, sessionId = "session-1") {
  return { apiVersion: "1", type: "session_finished" as const, sessionId, session: { id: sessionId, ...session } };
}

test("a failed session already reported by its error event does not replace that toast", () => {
  const reported = new Set<string>();
  rememberReportedErrorSession(reported, "session-1");

  assert.equal(
    failedSessionToastMessage(finished({ status: "failed", step: "API key invalid." }), reported, fallback),
    null,
  );
  assert.equal(reported.has("session-1"), false);
});

test("an unreported failed session shows its retained failure step instead of the generic fallback", () => {
  const toast = failedSessionToastMessage(
    finished({ status: "failed", step: "API key invalid.", content: "partial speech" }),
    new Set(),
    fallback,
  );

  assert.equal(toast?.message, "API key invalid.");
  assert.equal(toast?.title, "Recording Error");
  assert.equal(toast?.sessionId, "session-1");
});

test("legacy error content and the generic fallback remain available", () => {
  assert.equal(
    failedSessionToastMessage(finished({ status: "failed", content: "[Error] Connection failed" }), new Set(), fallback)
      ?.message,
    "Connection failed",
  );
  assert.equal(
    failedSessionToastMessage(finished({ status: "failed", content: "" }), new Set(), fallback)?.message,
    fallback.message,
  );
  assert.equal(failedSessionToastMessage(finished({ status: "completed" }), new Set(), fallback), null);
});

test("reported error sessions stay bounded", () => {
  const reported = new Set<string>();
  for (let index = 0; index < 40; index += 1) {
    rememberReportedErrorSession(reported, `session-${index}`);
  }
  rememberReportedErrorSession(reported, "");

  assert.equal(reported.size, 32);
  assert.equal(reported.has("session-0"), false);
  assert.equal(reported.has("session-39"), true);
});

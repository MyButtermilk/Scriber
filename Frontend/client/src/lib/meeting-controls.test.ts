import assert from "node:assert/strict";
import test from "node:test";

import type { MeetingState } from "./api-types";
import { meetingControlVisibility, meetingTimerNowMs } from "./meeting-controls";

test("Meeting controls are exposed only for recording and paused source states", () => {
  const expectations: Record<MeetingState, [boolean, boolean, boolean]> = {
    starting: [false, false, false],
    recording: [true, false, true],
    paused: [false, true, true],
    stopping: [false, false, false],
    finalizing: [false, false, false],
    analyzing: [false, false, false],
    ready: [false, false, false],
    capture_failed: [false, false, false],
    finalization_failed: [false, false, false],
    analysis_failed: [false, false, false],
    interrupted: [false, false, false],
    discarded: [false, false, false],
  };

  for (const [state, [pause, resume, stop]] of Object.entries(expectations)) {
    assert.deepEqual(meetingControlVisibility(state as MeetingState), { pause, resume, stop });
  }
});

test("stopped clocks retain the capture end even when processing resumes days later", () => {
  const now = Date.parse("2026-07-19T22:00:00.000Z");
  const endedAt = "2026-07-17T10:04:00.000Z";

  for (const state of [
    "stopping",
    "finalizing",
    "analyzing",
    "ready",
    "capture_failed",
    "finalization_failed",
    "analysis_failed",
    "interrupted",
    "discarded",
  ] as const) {
    assert.equal(meetingTimerNowMs(state, endedAt, now), Date.parse(endedAt));
  }
  assert.equal(meetingTimerNowMs("recording", endedAt, now), now);
  assert.equal(meetingTimerNowMs("paused", endedAt, now), now);
  assert.equal(meetingTimerNowMs("finalizing", "not-a-date", now), now);
});

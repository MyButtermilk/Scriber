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

test("processing clocks freeze at endedAt while active states use the live clock", () => {
  const now = Date.parse("2026-07-17T10:05:00.000Z");
  const endedAt = "2026-07-17T10:04:00.000Z";

  for (const state of ["stopping", "finalizing", "analyzing"] as const) {
    assert.equal(meetingTimerNowMs(state, endedAt, now), Date.parse(endedAt));
  }
  assert.equal(meetingTimerNowMs("recording", endedAt, now), now);
  assert.equal(meetingTimerNowMs("paused", endedAt, now), now);
  assert.equal(meetingTimerNowMs("finalizing", "not-a-date", now), now);
});

import assert from "node:assert/strict";
import test from "node:test";

import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { isBusyForUpdatePrompt, trayRecordingStateFromMessage } from "./runtime-message-state";

function message(value: object): ScriberWebSocketMessage {
  return value as ScriberWebSocketMessage;
}

test("update prompts stay hidden for every active recording phase", () => {
  for (const state of ["starting", "recording", "paused", "stopping", "finalizing", "analyzing"]) {
    assert.equal(
      isBusyForUpdatePrompt(
        message({
          type: "meeting_state",
          meeting: { state },
        }),
      ),
      true,
    );
  }

  assert.equal(isBusyForUpdatePrompt(message({ type: "session_started" })), true);
  assert.equal(isBusyForUpdatePrompt(message({ type: "transcribing" })), true);
  assert.equal(isBusyForUpdatePrompt(message({ type: "session_finished" })), false);
  assert.equal(isBusyForUpdatePrompt(message({ type: "history_updated" })), null);
});

test("tray state follows live mic and meeting lifecycle messages", () => {
  assert.deepEqual(
    trayRecordingStateFromMessage(
      message({
        type: "meeting_state",
        meeting: { state: "recording" },
      }),
    ),
    { active: true, mode: "meeting-recording" },
  );
  assert.deepEqual(
    trayRecordingStateFromMessage(
      message({
        type: "meeting_state",
        meeting: { state: "finalizing" },
      }),
    ),
    { active: false, mode: "meeting-finalizing" },
  );
  assert.deepEqual(
    trayRecordingStateFromMessage(
      message({
        type: "state",
        listening: true,
        recordingState: "recording",
      }),
    ),
    { active: true, mode: "recording" },
  );
  assert.deepEqual(
    trayRecordingStateFromMessage(
      message({
        type: "status",
        transcribing: true,
        recordingState: "finalizing",
      }),
    ),
    { active: false, mode: "transcribing" },
  );
  assert.deepEqual(trayRecordingStateFromMessage(message({ type: "session_finished" })), {
    active: false,
    mode: "idle",
  });
  assert.equal(trayRecordingStateFromMessage(message({ type: "history_updated" })), null);
});

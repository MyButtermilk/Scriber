import assert from "node:assert/strict";
import test from "node:test";

import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import {
  createLiveMicSessionMessageGate,
  isBusyForUpdatePrompt,
  trayRecordingStateFromMessage,
} from "./runtime-message-state";

function message(value: object): ScriberWebSocketMessage {
  return value as ScriberWebSocketMessage;
}

test("new captures supersede the previous session before its finalizer completes", () => {
  const accept = createLiveMicSessionMessageGate();
  assert.equal(accept(message({ type: "state", sessionId: "old" })), true);
  assert.equal(accept(message({ type: "session_started", sessionId: "new" })), true);
  for (const type of [
    "transcript",
    "status",
    "transcribing",
    "audio_level",
    "input_warning",
    "session_finished",
    "error",
  ]) {
    assert.equal(accept(message({ type, sessionId: "old" })), false, type);
  }
  assert.equal(accept(message({ type: "status", sessionId: "new", recordingState: "recording" })), true);
  assert.equal(accept(message({ type: "history_updated", sessionId: "old" })), true);
  assert.equal(accept(message({ type: "post_processing_fallback_used", sessionId: "old" })), true);
});

test("an authoritative reconnect snapshot adopts the current capture", () => {
  const accept = createLiveMicSessionMessageGate();
  accept(message({ type: "session_started", sessionId: "old" }));
  assert.equal(accept(message({ type: "state", sessionId: "new" })), true);
  assert.equal(accept(message({ type: "session_finished", sessionId: "old" })), false);
  assert.equal(accept(message({ type: "session_finished", sessionId: "new" })), true);
});

test("pending starts remain busy and expose a stoppable initializing state to the tray", () => {
  const pending = message({ type: "status", micStartPending: true, listening: false, recordingState: "finalizing" });
  assert.deepEqual(trayRecordingStateFromMessage(pending), { active: true, mode: "initializing" });
  assert.equal(isBusyForUpdatePrompt(pending), true);
  assert.equal(
    isBusyForUpdatePrompt(message({ type: "state", recordingState: "idle", backgroundProcessing: true })),
    true,
  );
});

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

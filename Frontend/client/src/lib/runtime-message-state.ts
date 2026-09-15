import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

/** Follow the current capture while older recordings finish independently. */
export function createLiveMicSessionMessageGate(): (message: ScriberWebSocketMessage) => boolean {
  let activeSessionId: string | null = null;
  return (message) => {
    const sessionId = typeof message.sessionId === "string" && message.sessionId ? message.sessionId : null;
    if (message.type === "state" || message.type === "session_started") {
      activeSessionId = sessionId;
      return true;
    }
    if (
      !["status", "audio_level", "input_warning", "transcript", "transcribing", "session_finished", "error"].includes(
        message.type,
      )
    ) {
      return true;
    }
    if (sessionId && activeSessionId && sessionId !== activeSessionId) {
      return false;
    }
    if (message.type === "session_finished" || message.type === "error") {
      activeSessionId = null;
    } else if (sessionId && !activeSessionId) {
      activeSessionId = sessionId;
    }
    return true;
  };
}

export function isBusyForUpdatePrompt(message: ScriberWebSocketMessage): boolean | null {
  if (message.type === "meeting_state") {
    return ["starting", "recording", "paused", "stopping", "finalizing", "analyzing"].includes(
      String(message.meeting.state || "").toLowerCase(),
    );
  }
  if (message.type === "transcribing" || message.type === "session_started") {
    return true;
  }
  if (message.type === "session_finished") {
    return false;
  }
  if (message.type === "state" || message.type === "status") {
    const recordingState = String(message.recordingState || "").toLowerCase();
    return Boolean(
      message.listening ||
      message.micStartPending ||
      (message.type === "state" && message.backgroundProcessing) ||
      (message.type === "state" && message.voiceEnrollmentActive) ||
      message.transcribing ||
      (recordingState && !["idle", "completed", "failed", "stopped"].includes(recordingState)),
    );
  }
  return null;
}

export function trayRecordingStateFromMessage(
  message: ScriberWebSocketMessage,
): { active: boolean; mode: string } | null {
  if (message.type === "meeting_state") {
    const meetingState = String(message.meeting.state || "").toLowerCase();
    if (["starting", "recording", "paused"].includes(meetingState)) {
      return { active: true, mode: `meeting-${meetingState}` };
    }
    if (["stopping", "finalizing", "analyzing"].includes(meetingState)) {
      return { active: false, mode: `meeting-${meetingState}` };
    }
    return { active: false, mode: "idle" };
  }
  if (message.type === "session_started") {
    return { active: true, mode: "initializing" };
  }
  if (message.type === "transcribing") {
    return { active: false, mode: "transcribing" };
  }
  if (message.type === "session_finished" || message.type === "error") {
    return { active: false, mode: "idle" };
  }
  if (message.type !== "state" && message.type !== "status") {
    return null;
  }

  const recordingState = String(message.recordingState || "").toLowerCase();
  if (message.micStartPending) {
    return { active: true, mode: "initializing" };
  }
  if (recordingState === "initializing") {
    return { active: true, mode: "initializing" };
  }
  if (recordingState === "recording" || message.listening) {
    return { active: true, mode: "recording" };
  }
  if (recordingState === "finalizing" || message.transcribing) {
    return { active: false, mode: "transcribing" };
  }
  if (["idle", "completed", "failed", "stopped"].includes(recordingState)) {
    return { active: false, mode: "idle" };
  }
  return null;
}

import type { MeetingState } from "./api-types";

export interface MeetingControlVisibility {
  pause: boolean;
  resume: boolean;
  stop: boolean;
}

/** Keep destructive Meeting controls tied to the exact backend source states. */
export function meetingControlVisibility(state: MeetingState): MeetingControlVisibility {
  return {
    pause: state === "recording",
    resume: state === "paused",
    stop: state === "recording" || state === "paused",
  };
}

/** Freeze processing-state clocks at the durable capture end boundary. */
export function meetingTimerNowMs(
  state: MeetingState,
  endedAt: string | null,
  nowMs: number,
): number {
  if (!["stopping", "finalizing", "analyzing"].includes(state) || !endedAt) {
    return nowMs;
  }
  const endedAtMs = new Date(endedAt).getTime();
  return Number.isFinite(endedAtMs) ? endedAtMs : nowMs;
}

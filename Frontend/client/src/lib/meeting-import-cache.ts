import type { QueryClient } from "@tanstack/react-query";

import type {
  MeetingImportJob,
  MeetingImportState,
  MeetingImportsResponse,
} from "@/lib/api-types";

export const MEETING_IMPORTS_QUERY_KEY = ["/api/meeting-imports"] as const;

const IMPORT_PHASE_RANK: Record<MeetingImportState, number> = {
  created: 0,
  receiving: 1,
  received: 2,
  probing: 3,
  preparing: 4,
  waiting_for_workspace: 5,
  committing: 6,
  finalizing: 7,
  cancel_requested: 8,
  completed: 9,
  canceled: 9,
  failed: 9,
};

const TERMINAL_IMPORT_STATES = new Set<MeetingImportState>([
  "completed",
  "canceled",
  "failed",
]);

const CANCELABLE_IMPORT_STATES = new Set<MeetingImportState>([
  "created",
  "receiving",
  "received",
  "probing",
  "preparing",
  "waiting_for_workspace",
]);

export interface MeetingImportProgressEventData {
  importId: string;
  phase: string;
  progress: number;
  status: string;
  receivedBytes: number;
  expectedBytes?: number;
  meetingId?: string;
}

export interface MeetingImportProgressView {
  importId: string;
  phase: string;
  stage: string;
  percentage: number;
}

function importState(value: string): MeetingImportState | null {
  return Object.prototype.hasOwnProperty.call(IMPORT_PHASE_RANK, value)
    ? value as MeetingImportState
    : null;
}

function boundedProgress(value: number): number {
  return Math.round(Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0)));
}

function isStaleImportState(
  current: MeetingImportState,
  incoming: MeetingImportState | null,
): boolean {
  // Once the backend has published a terminal result, a delayed cancellation
  // response or another terminal event must not rewrite that outcome.
  if (TERMINAL_IMPORT_STATES.has(current)) return incoming !== current;
  return incoming != null && IMPORT_PHASE_RANK[incoming] < IMPORT_PHASE_RANK[current];
}

function mergeMeetingImportJob(
  current: MeetingImportJob,
  incoming: MeetingImportJob,
): MeetingImportJob {
  if (isStaleImportState(current.state, incoming.state)) return current;
  if (current.state !== incoming.state) return incoming;
  return {
    ...incoming,
    progress: Math.max(current.progress, incoming.progress),
    receivedBytes: Math.max(current.receivedBytes, incoming.receivedBytes),
    meetingId: incoming.meetingId ?? current.meetingId,
  };
}

/**
 * Combine XHR, websocket, and fallback-poll progress without allowing a late
 * response from an older phase to move the UI backwards.
 */
export function mergeMeetingImportProgress(
  current: MeetingImportProgressView,
  incoming: MeetingImportProgressView,
): MeetingImportProgressView {
  const next = { ...incoming, percentage: boundedProgress(incoming.percentage) };
  if (!current.importId || current.importId !== next.importId) return next;

  const currentState = importState(current.phase);
  const nextState = importState(next.phase);
  if (currentState && isStaleImportState(currentState, nextState)) return current;
  if (currentState && nextState) {
    const currentRank = IMPORT_PHASE_RANK[currentState];
    const nextRank = IMPORT_PHASE_RANK[nextState];
    if (nextRank < currentRank) return current;
    if (nextRank > currentRank) return next;
  } else if (current.phase !== next.phase) {
    return next;
  }

  return {
    ...next,
    percentage: Math.max(current.percentage, next.percentage),
  };
}

export function upsertMeetingImportJob(
  queryClient: QueryClient,
  job: MeetingImportJob,
): void {
  queryClient.setQueryData<MeetingImportsResponse>(MEETING_IMPORTS_QUERY_KEY, (current) => {
    if (!current) {
      return {
        apiVersion: job.apiVersion,
        items: [job],
        total: 1,
        limit: 24,
      };
    }
    const index = current.items.findIndex((item) => item.id === job.id);
    const items = index >= 0
      ? current.items.map((item, itemIndex) => (
        itemIndex === index ? mergeMeetingImportJob(item, job) : item
      ))
      : [job, ...current.items].slice(0, current.limit);
    return {
      ...current,
      items,
      total: Math.max(current.total, current.items.length) + (index >= 0 ? 0 : 1),
    };
  });
}

/** Patch inbox data in-place; progress events must never trigger an HTTP fetch. */
export function applyMeetingImportProgressEvent(
  queryClient: QueryClient,
  event: MeetingImportProgressEventData,
): void {
  queryClient.setQueryData<MeetingImportsResponse>(MEETING_IMPORTS_QUERY_KEY, (current) => {
    if (!current) return current;
    const index = current.items.findIndex((item) => item.id === event.importId);
    if (index < 0) return current;

    const item = current.items[index];
    const eventState = importState(event.phase);
    const state = eventState ?? item.state;
    const eventRank = eventState ? IMPORT_PHASE_RANK[eventState] : null;
    const currentRank = IMPORT_PHASE_RANK[item.state];
    const stalePhase = isStaleImportState(item.state, eventState)
      || (eventRank != null && eventRank < currentRank);
    const samePhase = state === item.state;
    const progress = stalePhase
      ? item.progress
      : samePhase
        ? Math.max(item.progress, Math.max(0, Math.min(1, event.progress)))
        : Math.max(0, Math.min(1, event.progress));
    const nextState = stalePhase ? item.state : state;
    const next: MeetingImportJob = {
      ...item,
      state: nextState,
      progress,
      status: stalePhase ? item.status : event.status,
      receivedBytes: Math.max(item.receivedBytes, Math.max(0, event.receivedBytes)),
      expectedBytes: typeof event.expectedBytes === "number"
        ? Math.max(0, event.expectedBytes)
        : item.expectedBytes,
      meetingId: event.meetingId ?? item.meetingId,
      cancelRequested: nextState === "cancel_requested" || item.cancelRequested,
      canCancel: CANCELABLE_IMPORT_STATES.has(nextState),
      canRetry: nextState === "failed" && Boolean(event.meetingId ?? item.meetingId),
    };
    return {
      ...current,
      items: current.items.map((value, itemIndex) => itemIndex === index ? next : value),
    };
  });
}

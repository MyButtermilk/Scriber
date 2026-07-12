import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import type {
  MeetingActionItem,
  MeetingCapabilities,
  MeetingDetail,
  MeetingSegment,
  MeetingState,
  MeetingSummary,
  MeetingsResponse,
  MeetingTranscriptCheckpoint,
} from "@/lib/api-types";

const ACTIVE_MEETING_STATES = new Set<MeetingState>([
  "starting",
  "recording",
  "paused",
  "stopping",
  "finalizing",
  "analyzing",
]);

/**
 * The compact active-meeting query and paginated library intentionally use
 * different cache shapes. Sharing one key lets a preload or the global pill
 * seed a flat response that crashes `useInfiniteQuery` before Meetings renders.
 */
export const MEETING_LIST_QUERY_KEY = ["/api/meetings"] as const;
export const MEETING_HISTORY_QUERY_KEY = ["/api/meetings", "history"] as const;

export function isActiveMeetingState(state: MeetingState): boolean {
  return ACTIVE_MEETING_STATES.has(state);
}

export function mergeMeetingSegment(
  current: MeetingSegment[],
  incoming: MeetingSegment,
): MeetingSegment[] {
  if (incoming.revision === "live" && current.some((item) => item.revision === "canonical")) {
    return current;
  }
  const index = current.findIndex((item) => item.id === incoming.id);
  const next = index >= 0
    ? current.map((item, itemIndex) => itemIndex === index ? incoming : item)
    : [...current, incoming];
  next.sort((left, right) => left.startMs - right.startMs || left.sequence - right.sequence || left.id.localeCompare(right.id));
  return next;
}

export function applyMeetingSummaryEvent(
  queryClient: QueryClient,
  meeting: MeetingSummary,
): void {
  const activeMeeting = isActiveMeetingState(meeting.state) ? meeting : null;
  queryClient.setQueryData<MeetingsResponse>(MEETING_LIST_QUERY_KEY, (current) => {
    if (!current) return current;
    const index = current.items.findIndex((item) => item.id === meeting.id);
    const items = index >= 0
      ? current.items.map((item, itemIndex) => itemIndex === index ? meeting : item)
      : [meeting, ...current.items];
    return {
      ...current,
      items,
      total: Math.max(current.total, current.items.length) + (index >= 0 ? 0 : 1),
      activeMeeting: activeMeeting ?? (
        current.activeMeeting?.id === meeting.id ? null : current.activeMeeting
      ),
    };
  });
  queryClient.setQueryData<InfiniteData<MeetingsResponse, number>>(MEETING_HISTORY_QUERY_KEY, (current) => {
    if (!current) return current;
    const loaded = current.pages.flatMap((page) => page.items);
    const index = loaded.findIndex((item) => item.id === meeting.id);
    const items = index >= 0
      ? loaded.map((item, itemIndex) => itemIndex === index ? meeting : item)
      : [meeting, ...loaded];
    const total = Math.max(
      current.pages[0]?.total ?? 0,
      loaded.length,
    ) + (index >= 0 ? 0 : 1);
    let cursor = 0;
    const pages = current.pages.map((page) => {
      const limit = Math.max(1, page.limit || 100);
      const pageItems = items.slice(cursor, cursor + limit);
      const offset = cursor;
      cursor += pageItems.length;
      return {
        ...page,
        items: pageItems,
        total,
        offset,
        activeMeeting: activeMeeting ?? (
          page.activeMeeting?.id === meeting.id ? null : page.activeMeeting
        ),
      };
    });
    return {
      ...current,
      pages,
    };
  });
  queryClient.setQueryData<MeetingCapabilities>(["/api/meetings/capabilities"], (current) => current ? {
    ...current,
    activeMeeting: activeMeeting ?? (
      current.activeMeeting?.id === meeting.id ? null : current.activeMeeting
    ),
  } : current);
  queryClient.setQueryData<MeetingDetail>(["/api/meetings", meeting.id], (current) => current ? {
    ...current,
    ...meeting,
  } : current);
}

export function applyMeetingSegmentEvent(
  queryClient: QueryClient,
  meetingId: string,
  segment: MeetingSegment,
): void {
  queryClient.setQueryData<MeetingDetail>(["/api/meetings", meetingId], (current) => current ? {
    ...current,
    segments: mergeMeetingSegment(current.segments, segment),
  } : current);
}

export function applyMeetingCheckpointEvent(
  queryClient: QueryClient,
  meetingId: string,
  checkpoint: MeetingTranscriptCheckpoint,
): void {
  queryClient.setQueryData<MeetingDetail>(["/api/meetings", meetingId], (current) => {
    if (!current) return current;
    const index = current.transcriptCheckpoints.findIndex((item) => item.id === checkpoint.id);
    const transcriptCheckpoints = index >= 0
      ? current.transcriptCheckpoints.map((item, itemIndex) => itemIndex === index ? checkpoint : item)
      : [...current.transcriptCheckpoints, checkpoint];
    transcriptCheckpoints.sort((left, right) => left.commitOrdinal - right.commitOrdinal || left.sequence - right.sequence);
    return { ...current, transcriptCheckpoints };
  });
}

/** Apply a returned action-item update only to the Meeting that was mutated. */
export function applyMeetingActionItem(
  queryClient: QueryClient,
  meetingId: string,
  item: MeetingActionItem,
): void {
  if (item.meetingId !== meetingId) return;
  queryClient.setQueryData<MeetingDetail>(["/api/meetings", meetingId], (current) => {
    if (!current) return current;
    const index = current.actionItems.findIndex((value) => value.id === item.id);
    const actionItems = index >= 0
      ? current.actionItems.map((value, itemIndex) => itemIndex === index ? item : value)
      : [...current.actionItems, item];
    return { ...current, actionItems };
  });
}

export function applyMeetingTranscriptEditedEvent(
  queryClient: QueryClient,
  meetingId: string,
  segment: MeetingSegment,
  transcriptEditVersion: number,
): void {
  queryClient.setQueryData<MeetingDetail>(["/api/meetings", meetingId], (current) => current ? {
    ...current,
    transcriptEditVersion,
    segments: mergeMeetingSegment(current.segments, segment),
  } : current);
}

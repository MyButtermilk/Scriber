import type {
  MeetingSpeakerAssignmentConfirmationResponse,
  MeetingSpeakerAssignmentsResponse,
} from "@/lib/api-types";

/**
 * Apply one durable confirmation without discarding the other ephemeral LLM
 * candidates. Those candidates are intentionally not persisted by the backend
 * for privacy, so a full query refetch would make users request/pay for them a
 * second time after confirming only the first speaker.
 */
export function applySpeakerAssignmentConfirmation(
  current: MeetingSpeakerAssignmentsResponse | undefined,
  response: MeetingSpeakerAssignmentConfirmationResponse,
): MeetingSpeakerAssignmentsResponse | undefined {
  if (!current) return current;
  const speakerId = response.assignment.speakerId;
  if (!current.items.some((item) => item.speakerId === speakerId)) return current;
  return {
    ...current,
    items: current.items.map((item) => item.speakerId === speakerId
      ? {
          ...item,
          currentDisplayName: response.assignment.displayName,
          confirmedAttendee: response.assignment.confirmedAttendee,
          participantLinkSource: response.assignment.source || undefined,
        }
      : item),
  };
}

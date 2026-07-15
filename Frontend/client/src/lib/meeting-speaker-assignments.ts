import type {
  MeetingSpeakerAssignment,
  MeetingSpeakerAssignmentConfirmationResponse,
  MeetingSpeakerAssignmentsResponse,
} from "@/lib/api-types";

export interface MeetingSpeakerMergeOption {
  profileId: string;
  speakerId: string;
  displayName: string;
  speakerLabel: string;
  isNamed: boolean;
}

/** One selectable row per durable profile represented in this meeting. */
export function meetingSpeakerMergeOptions(
  items: readonly MeetingSpeakerAssignment[],
): MeetingSpeakerMergeOption[] {
  const seen = new Set<string>();
  const options: MeetingSpeakerMergeOption[] = [];
  for (const item of items) {
    const profileId = item.profileId?.trim() ?? "";
    if (!profileId || seen.has(profileId)) continue;
    seen.add(profileId);
    const canonicalProfileName = item.profileDisplayName?.trim()
      || item.profileMatch?.displayName.trim()
      || "Unnamed voice profile";
    options.push({
      profileId,
      speakerId: item.speakerId,
      displayName: canonicalProfileName,
      speakerLabel: item.speakerLabel || "",
      isNamed: Boolean(item.profileIsNamed ?? item.profileMatch),
    });
  }
  return options;
}

/** Mirror the backend rule that preserves the only named durable profile. */
export function canonicalMeetingSpeakerMergeSelection(
  requestedTarget: MeetingSpeakerMergeOption | null,
  requestedSource: MeetingSpeakerMergeOption | null,
): { target: MeetingSpeakerMergeOption | null; source: MeetingSpeakerMergeOption | null; directionChanged: boolean } {
  if (!requestedTarget || !requestedSource) {
    return { target: requestedTarget, source: requestedSource, directionChanged: false };
  }
  if (!requestedTarget.isNamed && requestedSource.isNamed) {
    return { target: requestedSource, source: requestedTarget, directionChanged: true };
  }
  return { target: requestedTarget, source: requestedSource, directionChanged: false };
}

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
          confirmedCustomName: response.assignment.customDisplayName ?? null,
          participantLinkSource: response.assignment.source || undefined,
        }
      : item),
  };
}

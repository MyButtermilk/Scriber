import assert from "node:assert/strict";
import test from "node:test";

import { REST_API_VERSION, type MeetingSpeakerAssignmentsResponse } from "./api-types";
import { applySpeakerAssignmentConfirmation } from "./meeting-speaker-assignments";

test("confirming one speaker preserves the remaining paid AI suggestions", () => {
  const current: MeetingSpeakerAssignmentsResponse = {
    apiVersion: REST_API_VERSION,
    calendarEvent: null,
    requiresConfirmation: true,
    llmSuggestionAvailable: false,
    llmRequested: true,
    items: ["speaker-1", "speaker-2"].map((speakerId, index) => ({
      speakerId,
      speakerLabel: `Speaker ${index + 1}`,
      currentDisplayName: `Speaker ${index + 1}`,
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [{
        attendee: {
          participantId: `participant-${index + 1}`,
          name: `Participant ${index + 1}`,
          address: `participant-${index + 1}@example.com`,
        },
        source: "llm" as const,
        confidence: 0.74,
        reason: "Transcript context",
      }],
    })),
  };

  const updated = applySpeakerAssignmentConfirmation(current, {
    apiVersion: REST_API_VERSION,
    requiresConfirmation: false,
    assignment: {
      speakerId: "speaker-1",
      displayName: "Participant 1",
      confirmedAttendee: current.items[0].suggestions[0].attendee,
      source: "llm",
      confirmedAt: "2026-07-14T20:00:00Z",
    },
  });

  assert.equal(updated?.items[0].confirmedAttendee?.participantId, "participant-1");
  assert.equal(updated?.items[0].currentDisplayName, "Participant 1");
  assert.deepEqual(updated?.items[1].suggestions, current.items[1].suggestions);
  assert.equal(updated?.items[1].suggestions[0].source, "llm");
  assert.equal(current.items[0].confirmedAttendee, null);
});

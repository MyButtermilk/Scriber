import assert from "node:assert/strict";
import test from "node:test";

import { REST_API_VERSION, type MeetingSpeakerAssignmentsResponse } from "./api-types";
import {
  applySpeakerAssignmentConfirmation,
  canonicalMeetingSpeakerMergeSelection,
  meetingSpeakerMergeOptions,
} from "./meeting-speaker-assignments";

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

test("a meeting-only name is resolved without creating an Outlook attendee", () => {
  const current: MeetingSpeakerAssignmentsResponse = {
    apiVersion: REST_API_VERSION,
    calendarEvent: null,
    requiresConfirmation: true,
    llmSuggestionAvailable: false,
    items: [{
      speakerId: "speaker-room",
      speakerLabel: "Speaker 1",
      currentDisplayName: "Speaker 1",
      profileMatch: null,
      confirmedAttendee: null,
      confirmedCustomName: null,
      suggestions: [],
    }],
  };

  const updated = applySpeakerAssignmentConfirmation(current, {
    apiVersion: REST_API_VERSION,
    requiresConfirmation: false,
    assignment: {
      speakerId: "speaker-room",
      displayName: "Berlin project room",
      confirmedAttendee: null,
      customDisplayName: "Berlin project room",
      source: "custom_name",
      confirmedAt: "2026-07-15T12:00:00Z",
    },
  });

  assert.equal(updated?.items[0].currentDisplayName, "Berlin project room");
  assert.equal(updated?.items[0].confirmedCustomName, "Berlin project room");
  assert.equal(updated?.items[0].confirmedAttendee, null);
  assert.equal(updated?.items[0].participantLinkSource, "custom_name");
});

test("merge options contain each durable profile represented in the meeting once", () => {
  const options = meetingSpeakerMergeOptions([
    {
      speakerId: "speaker-1",
      speakerLabel: "Speaker 1",
      currentDisplayName: "Ada",
      profileId: "profile-ada",
      profileDisplayName: "Ada Voice Library",
      profileIsNamed: true,
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [],
    },
    {
      speakerId: "speaker-2",
      speakerLabel: "Speaker 2",
      currentDisplayName: "Ada again",
      profileId: "profile-ada",
      profileDisplayName: "Ada Voice Library",
      profileIsNamed: true,
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [],
    },
    {
      speakerId: "speaker-3",
      speakerLabel: "Speaker 3",
      currentDisplayName: "Project room",
      profileId: null,
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [],
    },
    {
      speakerId: "speaker-4",
      speakerLabel: "Speaker 4",
      currentDisplayName: "Grace",
      profileId: "profile-grace",
      profileDisplayName: "Grace Voice Library",
      profileIsNamed: false,
      profileMatch: null,
      confirmedAttendee: null,
      suggestions: [],
    },
  ]);

  assert.deepEqual(options, [
    { profileId: "profile-ada", speakerId: "speaker-1", displayName: "Ada Voice Library", speakerLabel: "Speaker 1", isNamed: true },
    { profileId: "profile-grace", speakerId: "speaker-4", displayName: "Grace Voice Library", speakerLabel: "Speaker 4", isNamed: false },
  ]);
});

test("the only named Voice Library profile is canonical in either merge direction", () => {
  const named = {
    profileId: "profile-named",
    speakerId: "speaker-named",
    displayName: "Alice",
    speakerLabel: "Speaker 1",
    isNamed: true,
  };
  const unnamed = {
    profileId: "profile-unnamed",
    speakerId: "speaker-unnamed",
    displayName: "Speaker 9f01ab",
    speakerLabel: "Speaker 2",
    isNamed: false,
  };

  const forward = canonicalMeetingSpeakerMergeSelection(named, unnamed);
  const reverse = canonicalMeetingSpeakerMergeSelection(unnamed, named);

  assert.equal(forward.target?.profileId, named.profileId);
  assert.equal(forward.source?.profileId, unnamed.profileId);
  assert.equal(forward.directionChanged, false);
  assert.equal(reverse.target?.profileId, named.profileId);
  assert.equal(reverse.source?.profileId, unnamed.profileId);
  assert.equal(reverse.directionChanged, true);
});

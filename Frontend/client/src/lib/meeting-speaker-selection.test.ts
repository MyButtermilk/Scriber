import assert from "node:assert/strict";
import test from "node:test";

import type { OutlookCalendarContact } from "./api-types";
import { initialMeetingParticipantId, meetingContactsMatch } from "./meeting-speaker-selection";

const outlookAttendee: OutlookCalendarContact = {
  participantId: "outlook-participant-7",
  name: "Ada Lovelace",
  address: "ada@example.com",
  aliases: ["ada.lovelace@example.com"],
};

test("voice suggestions map to the exact Outlook Select id through aliases", () => {
  const independentlySerializedVoiceSuggestion: OutlookCalendarContact = {
    participantId: "voice-match-opaque-id",
    name: "Ada",
    address: "ada.lovelace@example.com",
  };

  assert.equal(
    initialMeetingParticipantId(
      [outlookAttendee],
      "",
      null,
      independentlySerializedVoiceSuggestion,
    ),
    "outlook-participant-7",
  );
  assert.equal(meetingContactsMatch(outlookAttendee, independentlySerializedVoiceSuggestion), true);
});

test("an explicit unconfirmed user choice survives a query refresh", () => {
  assert.equal(
    initialMeetingParticipantId([outlookAttendee], "outlook-participant-7", null, null),
    "outlook-participant-7",
  );
});

test("unknown suggestions never create an invalid Select value", () => {
  assert.equal(
    initialMeetingParticipantId(
      [outlookAttendee],
      "",
      null,
      { name: "Grace", address: "grace@example.com", participantId: "other" },
    ),
    "",
  );
});

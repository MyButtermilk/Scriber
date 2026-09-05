import assert from "node:assert/strict";
import test from "node:test";
import { genericMeetingSpeakerLabel, genericMeetingSpeakerLabels } from "./meeting-display";

test("technical anonymous speaker labels become stable letters", () => {
  assert.equal(genericMeetingSpeakerLabel("SPEAKER_00", 0), "Speaker A");
  assert.equal(genericMeetingSpeakerLabel("Speaker a1b2c3", 1), "Speaker B");
  assert.equal(genericMeetingSpeakerLabel("Remote 2", 2), "Speaker C");
  assert.equal(genericMeetingSpeakerLabel("Remote", 3), "Speaker D");
  assert.equal(genericMeetingSpeakerLabel("Speaker A", 4), "Speaker E");
  assert.equal(genericMeetingSpeakerLabel("Alex Morgan", 2), null);
  assert.equal(genericMeetingSpeakerLabel("Remote named", 2), null);
});

test("named speakers keep their ordinal reserved for remaining anonymous speakers", () => {
  const labels = genericMeetingSpeakerLabels([
    { id: "local", label: "You", displayName: "You", displayNameSource: "anonymous" },
    {
      id: "meeting-audio",
      label: "Meeting audio",
      displayName: "Meeting audio",
      displayNameSource: "anonymous",
    },
    { id: "speaker-1", label: "SPEAKER_00", displayName: "Alex", displayNameSource: "manual" },
    { id: "speaker-2", label: "SPEAKER_01", displayName: "SPEAKER_01", displayNameSource: "anonymous" },
    { id: "speaker-3", label: "Remote 2", displayName: "Remote 2", displayNameSource: "anonymous" },
  ]);

  assert.equal(labels.has("local"), false);
  assert.equal(labels.has("meeting-audio"), false);
  assert.equal(labels.has("speaker-1"), false);
  assert.equal(labels.get("speaker-2"), "Speaker B");
  assert.equal(labels.get("speaker-3"), "Speaker C");
});

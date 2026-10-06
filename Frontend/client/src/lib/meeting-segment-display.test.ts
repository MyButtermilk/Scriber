import assert from "node:assert/strict";
import test from "node:test";
import type { MeetingSegment } from "./api-types";
import { createMeetingSegmentProjector } from "./meeting-segment-display";

function segment(id: string): MeetingSegment {
  return {
    id,
    meetingId: "meeting",
    revision: "live",
    source: "system",
    speakerId: "speaker",
    speakerLabel: "Speaker A",
    startMs: 0,
    endMs: 1000,
    durationMs: 1000,
    text: "Synthetic passage",
    confidence: null,
    alignmentQuality: "provider_segment",
    isFinal: true,
    sequence: 0,
    createdAt: "2026-10-06",
    editVersion: 0,
    editedAt: null,
  };
}

const labelFor = (segment: MeetingSegment) => segment.speakerLabel;

test("projection reuses unchanged segments across append, reorder and removal", () => {
  const project = createMeetingSegmentProjector();
  const a = segment("a"),
    b = segment("b"),
    c = segment("c");
  const first = project([a, b], labelFor);
  const next = project([b, c, a], labelFor);
  assert.equal(next[0], first[1]);
  assert.equal(next[2], first[0]);
  assert.equal(project([a], labelFor)[0], first[0]);
  assert.deepEqual(
    next,
    [b, c, a].map((value) => ({ ...value, label: labelFor(value) })),
  );
});

test("updated text, timing, revision and reused IDs always receive the new snapshot", () => {
  const project = createMeetingSegmentProjector();
  const original = segment("a");
  const first = project([original], labelFor)[0];
  for (const update of [
    { text: "Correction", editVersion: 1 },
    { startMs: 99, endMs: 555 },
    { revision: "canonical" as const },
    { meetingId: "other-meeting", text: "Another recording" },
  ]) {
    const incoming = { ...original, ...update };
    const result = project([incoming], labelFor)[0];
    assert.notEqual(result, first);
    assert.deepEqual(result, { ...incoming, label: incoming.speakerLabel });
  }
  assert.deepEqual(original, segment("a"));
});

test("speaker naming and locale changes invalidate only changed labels", () => {
  const project = createMeetingSegmentProjector();
  const segments = [segment("a"), segment("b")];
  const first = project(segments, labelFor);
  const named = project(segments, (value) => (value.id === "a" ? "Ada" : value.speakerLabel));
  assert.notEqual(named[0], first[0]);
  assert.equal(named[0].label, "Ada");
  assert.equal(named[1], first[1]);
  const translated = project(segments, () => "Sprecher A");
  assert.ok(translated.every((value) => value.label === "Sprecher A"));
  assert.deepEqual(project(segments, labelFor), first);
});

test("a 12000-segment snapshot copies only the one changed segment after warmup", () => {
  let textReads = 0;
  let labelReads = 0;
  const countedLabel = (value: MeetingSegment) => {
    labelReads++;
    return value.speakerLabel;
  };
  const segments = Array.from({ length: 12_000 }, (_, i) => {
    const value = segment(String(i));
    Object.defineProperty(value, "text", {
      enumerable: true,
      get: () => {
        textReads++;
        return "Synthetic passage";
      },
    });
    return value;
  });
  const project = createMeetingSegmentProjector();
  const first = project(segments, countedLabel);
  const updated = { ...segments[6000], text: "Changed" };
  textReads = 0;
  labelReads = 0;
  const next = project(
    segments.map((value, index) => (index === 6000 ? updated : value)),
    countedLabel,
  );
  assert.equal(textReads, 0, "unchanged transcript bodies must not be recopied");
  assert.equal(labelReads, 1, "only the changed segment needs label resolution");
  assert.equal(next.filter((value, index) => value !== first[index]).length, 1);
  assert.equal(next[6000].text, "Changed");
});

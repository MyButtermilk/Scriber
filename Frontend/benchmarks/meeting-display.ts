import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";
import type { MeetingSegment } from "../client/src/lib/api-types";
import { createMeetingSegmentProjector } from "../client/src/lib/meeting-segment-display";

const initial: MeetingSegment[] = Array.from({ length: 12_000 }, (_, i) => ({
  id: `s${i}`,
  meetingId: "synthetic",
  revision: "live",
  source: "system",
  speakerId: `speaker-${i % 6}`,
  speakerLabel: `Speaker ${i % 6}`,
  startMs: i * 600,
  endMs: i * 600 + 900,
  durationMs: 900,
  text: "Synthetic benchmark passage",
  confidence: null,
  alignmentQuality: "provider_segment",
  isFinal: true,
  sequence: i,
  createdAt: "2026-10-06",
  editVersion: 0,
  editedAt: null,
}));
const snapshots: MeetingSegment[][] = [];
let current = initial;
for (let i = 0; i < 100; i++) {
  const index = (i * 7919) % current.length;
  current = current.map((segment, position) =>
    position === index ? { ...segment, text: `Synthetic update ${i}` } : segment,
  );
  snapshots.push(current);
}
const labelFor = (segment: MeetingSegment) => segment.speakerLabel;
const reference = (segments: MeetingSegment[]) => segments.map((segment) => ({ ...segment, label: labelFor(segment) }));
const check = createMeetingSegmentProjector();
let prior = check(initial, labelFor);
let newDisplays = 0;
for (const snapshot of snapshots) {
  const result = check(snapshot, labelFor);
  assert.deepEqual(result, reference(snapshot));
  newDisplays += result.filter((value, index) => value !== prior[index]).length;
  prior = result;
}
assert.equal(newDisplays, snapshots.length);
let checksum = 0;
function measure(setup: () => () => void) {
  const values: number[] = [];
  for (let i = 0; i < 12; i++) {
    const run = setup();
    const start = performance.now();
    run();
    if (i >= 3) values.push(performance.now() - start);
  }
  return Number(values.sort((a, b) => a - b)[4].toFixed(3));
}
const referenceMs = measure(() => () => {
  for (const snapshot of snapshots) checksum += reference(snapshot).length;
});
const reusedMs = measure(() => {
  const project = createMeetingSegmentProjector();
  project(initial, labelFor);
  return () => {
    for (const snapshot of snapshots) checksum += project(snapshot, labelFor).length;
  };
});
const firstReferenceMs = measure(() => () => {
  checksum += reference(initial).length;
});
const firstProjectionMs = measure(() => {
  const project = createMeetingSegmentProjector();
  return () => {
    checksum += project(initial, labelFor).length;
  };
});
console.log(
  JSON.stringify(
    {
      node: process.version,
      segments: initial.length,
      updates: snapshots.length,
      referenceNewDisplays: initial.length * snapshots.length,
      reusedNewDisplays: newDisplays,
      referenceMs,
      reusedMs,
      firstProjectionMs,
      firstReferenceMs,
      medianOf: 9,
      checksum,
    },
    null,
    2,
  ),
);

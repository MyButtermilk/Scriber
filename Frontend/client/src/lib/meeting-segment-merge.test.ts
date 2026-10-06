import assert from "node:assert/strict";
import test from "node:test";

import type { MeetingSegment } from "./api-types";
import { mergeMeetingSegment } from "./meeting-cache";

function referenceMerge(current: MeetingSegment[], incoming: MeetingSegment): MeetingSegment[] {
  if (incoming.revision === "live" && current.some((item) => item.revision === "canonical")) return current;
  const index = current.findIndex((item) => item.id === incoming.id);
  const next =
    index >= 0 ? current.map((item, itemIndex) => (itemIndex === index ? incoming : item)) : [...current, incoming];
  next.sort(
    (left, right) => left.startMs - right.startMs || left.sequence - right.sequence || left.id.localeCompare(right.id),
  );
  return next;
}

function segment(
  id: string,
  startMs: number,
  sequence: number,
  revision: MeetingSegment["revision"] = "live",
): MeetingSegment {
  return {
    id,
    meetingId: "m1",
    revision,
    source: sequence % 2 === 0 ? "microphone" : "system",
    speakerId: null,
    speakerLabel: "",
    startMs,
    endMs: startMs + 1_000,
    durationMs: 1_000,
    text: `text ${id}`,
    confidence: null,
    alignmentQuality: "provider_segment",
    isFinal: true,
    sequence,
    createdAt: "2026-01-01T00:00:00Z",
    editVersion: 0,
    editedAt: null,
  };
}

function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return state / 0x1_0000_0000;
  };
}

test("segment merge matches replace/append-then-stable-sort for randomized event streams", () => {
  for (let seed = 1; seed <= 40; seed += 1) {
    const random = seededRandom(seed);
    let fast: MeetingSegment[] = [];
    let expected: MeetingSegment[] = [];
    if (seed % 5 === 0) {
      // Unsorted server payloads must still converge on the reference order.
      fast = [segment("z", 9_000, 3), segment("a", 1_000, 1), segment("m", 1_000, 1)];
      expected = fast.slice();
    }
    for (let step = 0; step < 120; step += 1) {
      const roll = random();
      const id = roll < 0.3 && expected.length > 0 ? expected[Math.floor(random() * expected.length)].id : `s${step}`;
      // A tiny time domain forces ties on startMs and sequence, exercising the id tiebreak.
      const startMs = Math.floor(random() * 12) * 500;
      const sequence = Math.floor(random() * 3);
      const revision = random() < 0.04 ? "canonical" : "live";
      const incoming = segment(id, startMs, sequence, revision);
      const fastResult = mergeMeetingSegment(fast, incoming);
      const expectedResult = referenceMerge(expected, incoming);
      assert.deepEqual(fastResult, expectedResult, `seed ${seed} step ${step}`);
      assert.ok(
        fastResult.every((item, position) => item === expectedResult[position]),
        `element identity seed ${seed} step ${step}`,
      );
      assert.equal(fastResult === fast, expectedResult === expected, `identity seed ${seed} step ${step}`);
      fast = fastResult;
      expected = expectedResult;
    }
  }
});

test("segment merge never mutates the cached array", () => {
  const current = [segment("a", 0, 0), segment("b", 1_000, 1)];
  const snapshot = current.slice();
  const next = mergeMeetingSegment(current, segment("c", 500, 2));
  assert.deepEqual(current, snapshot);
  assert.deepEqual(
    next.map((item) => item.id),
    ["a", "c", "b"],
  );
  assert.equal(next[0], current[0]);
});

/**
 * Deterministic work budget (ratchet) for a long live Meeting.
 *
 * Counting comparator field reads is reproducible across machines, unlike
 * wall-clock timing. The former copy-then-sort merge needed 9,055,114 reads
 * for this stream; the linear merge needs 4,563,352. Lower the ceiling whenever
 * the implementation gets cheaper; never raise it to admit a regression.
 */
const LIVE_MEETING_SEGMENT_EVENTS = 3_000;
const LIVE_MEETING_START_MS_READ_CEILING = 4_600_000;

test("live Meeting segment stream stays within its deterministic work budget", () => {
  let reads = 0;
  const counted = (id: string, startMs: number, sequence: number): MeetingSegment => {
    const value = segment(id, startMs, sequence);
    Object.defineProperty(value, "startMs", {
      enumerable: true,
      get() {
        reads += 1;
        return startMs;
      },
    });
    return value;
  };
  let segments: MeetingSegment[] = [];
  for (let step = 0; step < LIVE_MEETING_SEGMENT_EVENTS; step += 1) {
    // Two sources interleave slightly out of order, as microphone and system
    // providers finalize independently.
    const startMs = step * 3_000 + (step % 2 === 0 ? 3_400 : 0);
    segments = mergeMeetingSegment(segments, counted(`seg-${step}`, startMs, step));
  }
  assert.equal(segments.length, LIVE_MEETING_SEGMENT_EVENTS);
  assert.ok(
    reads <= LIVE_MEETING_START_MS_READ_CEILING,
    `segment merge performed ${reads} startMs reads; ceiling is ${LIVE_MEETING_START_MS_READ_CEILING}`,
  );
});

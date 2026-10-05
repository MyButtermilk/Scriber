import assert from "node:assert/strict";
import test from "node:test";

import {
  activeReviewSegmentId,
  createReviewPlaybackLookup,
  matchingReviewSegmentIds,
  nextReviewMatchId,
  reviewTimeRangeBounds,
  reviewTimelinePositionPercent,
  type ReviewTimelineSegment,
} from "./meeting-review-timeline";

const segments: ReviewTimelineSegment[] = [
  {
    id: "live-preview",
    revision: "live",
    speakerId: "speaker-b",
    label: "Speaker B",
    startMs: 900,
    endMs: 1_600,
    alignmentQuality: "exact_word",
    text: "live preview",
  },
  {
    id: "canonical-estimated",
    revision: "canonical",
    speakerId: "speaker-a",
    label: "Speaker A",
    startMs: 0,
    endMs: 1_200,
    alignmentQuality: "estimated",
    text: "estimated canonical text",
  },
  {
    id: "canonical-exact",
    revision: "canonical",
    speakerId: "speaker-b",
    label: "Speaker B",
    startMs: 900,
    endMs: 1_500,
    alignmentQuality: "exact_word",
    text: "exact canonical text",
  },
];

test("playback chooses the best canonical segment and leaves real gaps inactive", () => {
  assert.equal(activeReviewSegmentId(segments, 950), "canonical-exact");
  assert.equal(activeReviewSegmentId(segments, 1_499), "canonical-exact");
  assert.equal(activeReviewSegmentId(segments, 1_600), null);
  assert.equal(activeReviewSegmentId(segments, -1), null);
});

test("review search applies speaker and time filters before wrapping through matches", () => {
  const reviewSegments: ReviewTimelineSegment[] = [
    { ...segments[1], id: "opening", speakerId: "speaker-a", startMs: 0, endMs: 800, text: "Launch plan" },
    { ...segments[2], id: "decision", speakerId: "speaker-b", startMs: 900, endMs: 1_500, text: "Launch decision" },
    { ...segments[2], id: "follow-up", speakerId: "speaker-b", startMs: 2_000, endMs: 2_600, text: "Budget follow-up" },
  ];

  const matches = matchingReviewSegmentIds(reviewSegments, {
    query: "launch",
    speakerId: "speaker-b",
    fromMs: 500,
    toMs: 1_800,
  });
  assert.deepEqual(matches, ["decision"]);
  assert.equal(nextReviewMatchId(["opening", "decision"], "decision", 1), "opening");
  assert.equal(nextReviewMatchId(["opening", "decision"], "opening", -1), "decision");
  assert.equal(nextReviewMatchId([], null, 1), null);
  assert.deepEqual(reviewTimeRangeBounds("15-30"), { fromMs: 900_000, toMs: 1_800_000 });
  assert.deepEqual(reviewTimeRangeBounds("all"), { fromMs: null, toMs: null });
  assert.equal(reviewTimelinePositionPercent(30_000, 120_000), 25);
  assert.equal(reviewTimelinePositionPercent(200_000, 120_000), 100);
  assert.equal(reviewTimelinePositionPercent(1, 0), 0);
});

test("review search keeps live-only meeting text searchable before a canonical transcript exists", () => {
  assert.deepEqual(matchingReviewSegmentIds([segments[0]], { query: "preview" }), ["live-preview"]);
});

test("indexed playback preserves priorities, stable ties, end boundaries and backward seeks", () => {
  const input = [
    ...segments,
    { ...segments[2], id: "same-priority-later-in-input" },
    { ...segments[2], id: "later-start", startMs: 1_100, endMs: 1_300 },
    { ...segments[2], id: "zero-length", startMs: 1_200, endMs: 1_200 },
  ];
  const lookup = createReviewPlaybackLookup(input);
  for (const atMs of [950, 1_200, 1_300, 1_499, 1_500, 1_600, 0, 900, -1, NaN, Infinity, 950]) {
    assert.equal(lookup(atMs), activeReviewSegmentId(input, atMs), `at ${atMs}`);
  }
  assert.equal(lookup(950), "canonical-exact");
  assert.equal(createReviewPlaybackLookup([])(0), null);
});

test("indexed playback matches the linear oracle for unsorted overlapping timelines", () => {
  let seed = 83;
  const random = () => (seed = (Math.imul(seed, 1_664_525) + 1_013_904_223) >>> 0) / 2 ** 32;
  for (let trial = 0; trial < 60; trial++) {
    const input = Array.from({ length: 200 }, (_, i): ReviewTimelineSegment => {
      const startMs = Math.floor(random() * 30) * 100;
      return {
        ...segments[i % segments.length],
        id: `segment-${i}`,
        startMs,
        endMs: startMs + Math.floor(random() * 20) * 100,
      };
    });
    const originalOrder = input.slice();
    const lookup = createReviewPlaybackLookup(input);
    for (let atMs = 0; atMs <= 5_000; atMs += 50) {
      assert.equal(lookup(atMs), activeReviewSegmentId(input, atMs), `trial ${trial}, time ${atMs}`);
    }
    assert.deepEqual(input, originalOrder);
  }
});

test("playback lookup stays below 64 timestamp reads per seek on a long transcript", () => {
  let reads = 0;
  const input = Array.from({ length: 12_000 }, (_, i) => ({
    ...segments[2],
    id: `segment-${i}`,
    get startMs() {
      reads++;
      return i * 600;
    },
    get endMs() {
      reads++;
      return i * 600 + 900;
    },
  }));
  const lookup = createReviewPlaybackLookup(input);
  // Construction is intentionally outside the repeated-playback budget.
  // Linear lookup needs at least 12,000 reads per seek. Do not raise this
  // deterministic ceiling to admit a regression; timings are informational.
  for (let i = 0; i < 2_000; i++) {
    const atMs = (i * 7919) % 7_200_000;
    const expected = activeReviewSegmentId(input, atMs);
    reads = 0;
    assert.equal(lookup(atMs), expected);
    assert.ok(reads <= 64, `${reads} timestamp reads at ${atMs}; ceiling is 64`);
  }
});

test("new transcript snapshots rebuild playback without retaining the previous winner", () => {
  const previous = createReviewPlaybackLookup(segments);
  const updated = createReviewPlaybackLookup(segments.filter((segment) => segment.id !== "canonical-exact"));
  assert.equal(previous(950), "canonical-exact");
  assert.equal(updated(950), "canonical-estimated");
});

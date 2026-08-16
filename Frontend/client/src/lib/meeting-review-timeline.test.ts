import assert from "node:assert/strict";
import test from "node:test";

import {
  activeReviewSegmentId,
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

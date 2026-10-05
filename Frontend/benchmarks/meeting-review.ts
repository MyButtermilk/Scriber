import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";
import {
  activeReviewSegmentId,
  createReviewPlaybackLookup,
  type ReviewTimelineSegment,
} from "../client/src/lib/meeting-review-timeline";

// Synthetic two-hour timeline, with overlapping microphone/system turns.
// No transcript content, network, credentials, or installed app required.
const segments: ReviewTimelineSegment[] = Array.from({ length: 12_000 }, (_, i) => ({
  id: `segment-${i}`,
  revision: "canonical",
  speakerId: null,
  label: "Speaker",
  startMs: i * 600,
  endMs: i * 600 + 900,
  alignmentQuality: i % 3 ? "provider_segment" : "exact_word",
  text: "Synthetic benchmark text",
}));
const samples = Array.from({ length: 2_000 }, (_, i) => (i * 7919) % 7_200_000);
const lookup = createReviewPlaybackLookup(segments);
for (const atMs of samples) assert.equal(lookup(atMs), activeReviewSegmentId(segments, atMs));

function measure(run: () => void) {
  for (let i = 0; i < 3; i++) run();
  const timings = Array.from({ length: 9 }, () => {
    const started = performance.now();
    run();
    return performance.now() - started;
  }).sort((a, b) => a - b);
  return Number(timings[4].toFixed(3));
}

let checksum = 0;
const linearMs = measure(() => {
  for (const atMs of samples) checksum += activeReviewSegmentId(segments, atMs)?.length ?? 0;
});
const indexedMs = measure(() => {
  for (const atMs of samples) checksum += lookup(atMs)?.length ?? 0;
});
const buildMs = measure(() => {
  checksum += createReviewPlaybackLookup(segments)(600)?.length ?? 0;
});
console.log(
  JSON.stringify(
    {
      node: process.version,
      segments: segments.length,
      lookups: samples.length,
      medianOf: 9,
      linearMs,
      indexedMs,
      buildMs,
      speedup: Number((linearMs / indexedMs).toFixed(2)),
      checksum,
    },
    null,
    2,
  ),
);

import assert from "node:assert/strict";
import test from "node:test";

import type { MeetingAudioAsset, MeetingAudioTrackSource } from "./api-types";
import {
  calculateMeetingElapsedMs,
  captureMeetingPlaybackRequest,
  formatMeetingOffset,
  meetingCheckpointFreshness,
  meetingPlaybackOriginMs,
  meetingTimeToAssetTimeSeconds,
  playbackSourceForSegment,
  playbackSourceForMuteState,
} from "./meeting-playback";

test("checkpoint freshness does not report paused capture as stale", () => {
  const updatedAt = "2026-07-12T10:00:00.000Z";
  const now = new Date("2026-07-12T10:01:30.000Z").getTime();

  assert.deepEqual(meetingCheckpointFreshness(updatedAt, now, false), {
    ageSeconds: 90,
    ageLabel: "90 s ago",
    stale: true,
  });
  assert.deepEqual(meetingCheckpointFreshness(updatedAt, now, true), {
    ageSeconds: 90,
    ageLabel: "capture paused",
    stale: false,
  });
  assert.deepEqual(meetingCheckpointFreshness("invalid", now, false), {
    ageSeconds: null,
    ageLabel: "save time unavailable",
    stale: true,
  });
});

test("long Meeting timestamps use an explicit hour field", () => {
  assert.equal(formatMeetingOffset(3_599_000), "59:59");
  assert.equal(formatMeetingOffset(3_600_000), "1:00:00");
  assert.equal(formatMeetingOffset(3_909_000), "1:05:09");
  assert.equal(formatMeetingOffset(18_000_000), "5:00:00");
});

test("elapsed Meeting time freezes at the durable audio frontier while paused", () => {
  const startedAt = "2026-07-12T10:00:00.000Z";
  const now = new Date("2026-07-12T10:30:00.000Z").getTime();
  const gaps = [{ startedAtMs: 60_000, endedAtMs: 90_000 }];

  assert.equal(calculateMeetingElapsedMs(startedAt, now, gaps), 1_770_000);
  assert.equal(
    calculateMeetingElapsedMs(startedAt, now, gaps, 420_000, "2026-07-12T10:07:30.000Z"),
    420_000,
  );
  assert.equal(
    calculateMeetingElapsedMs(startedAt, now, gaps, undefined, "2026-07-12T10:07:30.000Z"),
    420_000,
  );
  assert.equal(
    calculateMeetingElapsedMs(
      startedAt,
      now,
      gaps,
      undefined,
      undefined,
      1_800_000,
      "2026-07-12T10:29:00.000Z",
    ),
    1_860_000,
  );
});

function track(source: MeetingAudioTrackSource, timelineOriginMs: number) {
  return {
    source,
    streamIndex: 0,
    codec: source === "mixed" ? "opus" : "flac",
    sampleRate: 16_000,
    channels: 1,
    timelineOriginMs,
    durationMs: 60_000,
    sampleCount: 960_000,
    pcmSha256: "a".repeat(64),
    equalityVerified: source !== "mixed",
  };
}

function asset(
  kind: "multitrack_flac" | "playback_mix" | "playback_microphone" | "playback_system",
  tracks: ReturnType<typeof track>[],
): MeetingAudioAsset {
  return {
    id: kind,
    meetingId: "meeting",
    kind,
    relativePath: `${kind}.audio`,
    codec: kind.startsWith("playback_") ? "opus" : "flac",
    sampleRate: 16_000,
    channels: 1,
    durationMs: 60_000,
    byteSize: 1,
    sha256: "b".repeat(64),
    trackManifestVersion: 2,
    trackManifest: tracks.map((item, streamIndex) => ({ ...item, streamIndex })),
    equalityVerified: kind === "multitrack_flac",
    createdAt: "2026-07-12T00:00:00Z",
  };
}

const assets = [
  asset("multitrack_flac", [
    track("microphone", 1_000),
    track("mic_clean", 1_200),
    track("system", 200),
  ]),
  asset("playback_mix", [track("mixed", 500)]),
  asset("playback_microphone", [track("mic_clean", 0)]),
  asset("playback_system", [track("system", 0)]),
];

test("nonzero manifests resolve the exact playback track and legacy assets fall back to zero", () => {
  assert.equal(meetingPlaybackOriginMs(assets, "mix"), 500);
  assert.equal(meetingPlaybackOriginMs(assets, "microphone"), 0);
  assert.equal(meetingPlaybackOriginMs(assets, "system"), 0);
  assert.equal(meetingPlaybackOriginMs([{ ...assets[0], trackManifest: undefined }], "microphone"), 0);
});

test("source toggle preserves Meeting time and play/pause intent across different origins", () => {
  const playing = captureMeetingPlaybackRequest(2.3, false, false, assets, "system");
  assert.deepEqual(playing, { meetingTimeMs: 2_300, shouldPlay: true });
  assert.equal(meetingTimeToAssetTimeSeconds(playing.meetingTimeMs, assets, "microphone"), 2.3);
  assert.equal(meetingTimeToAssetTimeSeconds(playing.meetingTimeMs, assets, "mix"), 1.8);

  const paused = captureMeetingPlaybackRequest(1.3, true, false, assets, "microphone");
  assert.deepEqual(paused, { meetingTimeMs: 1_300, shouldPlay: false });
});

test("segment click selects its route and seeks in asset-local time with a negative clamp", () => {
  assert.equal(playbackSourceForSegment("mixed"), "mix");
  assert.equal(playbackSourceForSegment("microphone"), "microphone");
  assert.equal(playbackSourceForSegment("system"), "system");
  assert.equal(meetingTimeToAssetTimeSeconds(2_200, assets, "microphone"), 2.2);
  assert.equal(meetingTimeToAssetTimeSeconds(100, assets, "microphone"), 0.1);
});

test("mute routing never selects an unavailable isolated track", () => {
  const mixOnly = new Set(["mix"] as const);
  assert.equal(
    playbackSourceForMuteState(mixOnly, { microphone: false, system: false }),
    "mix",
  );
  assert.equal(
    playbackSourceForMuteState(mixOnly, { microphone: false, system: true }),
    "mix",
  );
  assert.equal(
    playbackSourceForMuteState(
      new Set(["mix", "microphone", "system"] as const),
      { microphone: false, system: true },
    ),
    "microphone",
  );
});

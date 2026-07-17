import type { MeetingAudioAsset, MeetingAudioTrackManifestEntry } from "./api-types";

export type MeetingPlaybackSource = "mix" | "microphone" | "system";

export interface MeetingPlaybackRequest {
  meetingTimeMs: number;
  shouldPlay: boolean;
}

export const MEETING_SPEAKER_SAMPLE_MIN_MS = 5_000;
export const MEETING_SPEAKER_SAMPLE_MAX_MS = 8_000;

export interface MeetingSpeakerSampleWindow {
  startMs: number;
  endMs: number;
}

export interface MeetingAudioGap {
  startedAtMs: number;
  endedAtMs: number;
}

export interface MeetingCheckpointFreshness {
  ageSeconds: number | null;
  ageLabel: string;
  stale: boolean;
}

function safeNonNegative(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

/**
 * Build a useful speaker-identification preview around a transcript segment.
 * Short utterances gain surrounding full-mix context, while the window stays
 * inside the retained audio. Meetings shorter than five seconds cannot offer a
 * sample that satisfies the identification contract.
 */
export function meetingSpeakerSampleWindow(
  segmentStartMs: number,
  segmentEndMs: number,
  audioOriginMs: number,
  audioDurationMs: number,
): MeetingSpeakerSampleWindow | null {
  const audioStartMs = safeNonNegative(audioOriginMs);
  const availableDurationMs = safeNonNegative(audioDurationMs);
  if (availableDurationMs < MEETING_SPEAKER_SAMPLE_MIN_MS) return null;

  const audioEndMs = audioStartMs + availableDurationMs;
  const boundedSegmentStartMs = Math.min(
    audioEndMs,
    Math.max(audioStartMs, safeNonNegative(segmentStartMs)),
  );
  const boundedSegmentEndMs = Math.min(
    audioEndMs,
    Math.max(boundedSegmentStartMs, safeNonNegative(segmentEndMs)),
  );
  const sampleDurationMs = Math.min(
    MEETING_SPEAKER_SAMPLE_MAX_MS,
    Math.max(
      MEETING_SPEAKER_SAMPLE_MIN_MS,
      boundedSegmentEndMs - boundedSegmentStartMs,
    ),
    availableDurationMs,
  );
  const centeredStartMs = (
    (boundedSegmentStartMs + boundedSegmentEndMs) / 2
  ) - (sampleDurationMs / 2);
  const startMs = Math.min(
    Math.max(audioStartMs, centeredStartMs),
    audioEndMs - sampleDurationMs,
  );
  return { startMs, endMs: startMs + sampleDurationMs };
}

/**
 * Resolve elapsed Meeting-clock media time. The backend records both the exact
 * pause frontier and the current capture session's timeline origin, so provider
 * limit warnings include silence inserted for completed gaps without advancing
 * while capture is paused. Older records fall back to active-capture time.
 */
export function calculateMeetingElapsedMs(
  startedAt: string | null,
  nowMs: number,
  audioGaps: readonly MeetingAudioGap[],
  pausedAtTimelineMs?: unknown,
  pausedAtUtc?: unknown,
  recordingTimelineOffsetMs?: unknown,
  recordingTimelineStartedAtUtc?: unknown,
): number {
  if (typeof pausedAtTimelineMs === "number" && Number.isFinite(pausedAtTimelineMs)) {
    return safeNonNegative(pausedAtTimelineMs);
  }
  const recordingStartedAtMs = typeof recordingTimelineStartedAtUtc === "string"
    ? new Date(recordingTimelineStartedAtUtc).getTime()
    : Number.NaN;
  if (
    typeof recordingTimelineOffsetMs === "number"
    && Number.isFinite(recordingTimelineOffsetMs)
    && Number.isFinite(recordingStartedAtMs)
  ) {
    return safeNonNegative(recordingTimelineOffsetMs)
      + safeNonNegative(nowMs - recordingStartedAtMs);
  }
  if (!startedAt) return 0;
  const startedAtMs = new Date(startedAt).getTime();
  if (!Number.isFinite(startedAtMs)) return 0;
  const pausedAtUtcMs = typeof pausedAtUtc === "string"
    ? new Date(pausedAtUtc).getTime()
    : Number.NaN;
  const effectiveNowMs = Number.isFinite(pausedAtUtcMs)
    ? Math.min(nowMs, pausedAtUtcMs)
    : nowMs;
  const gapDurationMs = audioGaps.reduce(
    (sum, gap) => sum + Math.max(0, gap.endedAtMs - gap.startedAtMs),
    0,
  );
  return safeNonNegative(effectiveNowMs - startedAtMs - gapDurationMs);
}

/** Paused capture cannot produce checkpoints, so its last durable save never becomes stale. */
export function meetingCheckpointFreshness(
  updatedAt: string,
  nowMs: number,
  paused: boolean,
): MeetingCheckpointFreshness {
  const updatedAtMs = new Date(updatedAt).getTime();
  if (!Number.isFinite(updatedAtMs)) {
    return {
      ageSeconds: null,
      ageLabel: paused ? "capture paused" : "save time unavailable",
      stale: !paused,
    };
  }
  const ageSeconds = Math.max(0, Math.floor((nowMs - updatedAtMs) / 1_000));
  return {
    ageSeconds,
    ageLabel: paused ? "capture paused" : ageSeconds < 5 ? "just now" : `${ageSeconds} s ago`,
    stale: !paused && ageSeconds > 75,
  };
}

/** Human-readable Meeting-clock timestamp; hours become explicit for long sessions. */
export function formatMeetingOffset(milliseconds: number | null): string {
  if (milliseconds == null) return "Now";
  const seconds = Math.floor(safeNonNegative(milliseconds) / 1_000);
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function timelineOrigin(track: MeetingAudioTrackManifestEntry | undefined): number {
  return safeNonNegative(track?.timelineOriginMs ?? 0);
}

/** Resolve the timeline origin of the exact file exposed by each playback route. */
export function meetingPlaybackOriginMs(
  audioAssets: readonly MeetingAudioAsset[] | null | undefined,
  source: MeetingPlaybackSource,
): number {
  const assets = audioAssets ?? [];
  if (source === "mix") {
    const manifest = assets.find((asset) => asset.kind === "playback_mix")?.trackManifest;
    return timelineOrigin(
      manifest?.find((track) => track.source === "mixed") ?? manifest?.[0],
    );
  }

  const manifest = assets.find((asset) => asset.kind === (
    source === "microphone" ? "playback_microphone" : "playback_system"
  ))?.trackManifest;
  if (source === "microphone") {
    // Per-track Opus derivatives are padded onto Meeting clock zero, just like
    // the mix. The source name still records whether AEC-clean or raw mic won.
    return timelineOrigin(
      manifest?.find((track) => track.source === "mic_clean")
      ?? manifest?.find((track) => track.source === "microphone"),
    );
  }
  return timelineOrigin(manifest?.find((track) => track.source === "system"));
}

/** Convert a canonical Meeting-clock timestamp to HTMLAudioElement.currentTime. */
export function meetingTimeToAssetTimeSeconds(
  meetingTimeMs: number,
  audioAssets: readonly MeetingAudioAsset[] | null | undefined,
  source: MeetingPlaybackSource,
): number {
  const meetingClockMs = safeNonNegative(meetingTimeMs);
  return Math.max(0, meetingClockMs - meetingPlaybackOriginMs(audioAssets, source)) / 1_000;
}

/** Capture one player's position on the canonical Meeting clock before a source swap. */
export function captureMeetingPlaybackRequest(
  assetCurrentTimeSeconds: number,
  paused: boolean,
  ended: boolean,
  audioAssets: readonly MeetingAudioAsset[] | null | undefined,
  source: MeetingPlaybackSource,
): MeetingPlaybackRequest {
  const localMs = safeNonNegative(assetCurrentTimeSeconds) * 1_000;
  return {
    meetingTimeMs: meetingPlaybackOriginMs(audioAssets, source) + localMs,
    shouldPlay: !paused && !ended,
  };
}

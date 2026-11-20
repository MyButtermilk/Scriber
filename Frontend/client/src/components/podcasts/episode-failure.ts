import type { PodcastEpisode, PodcastFailureKind } from "@/lib/podcast-types";

/** Concrete, state-derived labels for a failed episode (i18n keys). */
export const podcastFailureLabels: Record<PodcastFailureKind, string> = {
  too_large: "Too large to process",
  provider_missing: "Transcription provider missing",
  storage_full: "Storage full",
  download: "Download failed",
  transcription: "Transcription failed",
  summary: "Summary failed",
  processing: "Processing failed",
};

/** The failure category the backend derived, or the generic one for older backends. */
export function podcastFailureKind(episode: Pick<PodcastEpisode, "status" | "failureKind">): PodcastFailureKind | null {
  if (episode.status !== "failed") return null;
  return episode.failureKind || "processing";
}

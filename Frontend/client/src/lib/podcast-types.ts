export interface PodcastSearchResult {
  id: string;
  title: string;
  author: string;
  feedUrl: string;
}

export interface PodcastSubscription {
  id: string;
  title: string;
  author: string;
  description: string;
  feedUrl: string;
  autoProcess: boolean;
  lastCheckedAt: string;
  error: string;
  episodeCount: number;
  completedCount: number;
}

export interface PodcastLibrary {
  subscriptions: PodcastSubscription[];
  activeCount: number;
  refreshIntervalMinutes: number;
  cacheLimitBytes: number;
  running: boolean;
  refreshing: boolean;
}

export type PodcastEpisodeStatus =
  "available" | "queued" | "downloading" | "admitting" | "transcribing" | "summarizing" | "completed" | "failed";

export interface PodcastEpisode {
  id: string;
  title: string;
  description: string;
  publishedAt: string;
  durationSeconds: number | null;
  status: PodcastEpisodeStatus;
  transcriptId: string;
  downloadedBytes: number;
  error: string;
}

export interface PodcastEpisodePage {
  items: PodcastEpisode[];
  total: number;
}

export const podcastStatusLabels: Record<PodcastEpisodeStatus, string> = {
  available: "Available",
  queued: "Queued",
  downloading: "Downloading",
  admitting: "Preparing audio",
  transcribing: "Transcribing",
  summarizing: "Summarizing",
  completed: "Ready to read",
  failed: "Needs attention",
};

export function podcastEpisodeIsBusy(status: PodcastEpisodeStatus): boolean {
  return ["queued", "downloading", "admitting", "transcribing", "summarizing"].includes(status);
}

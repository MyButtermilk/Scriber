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
  /** Episodes published since subscribing that the user has not seen in the list yet. */
  newCount: number;
  /** Ready episodes whose transcript was not opened and whose audio was not played. */
  unreadCount: number;
  /** Episodes that are new, unread, or both. */
  attentionCount: number;
}

export interface PodcastLibrary {
  subscriptions: PodcastSubscription[];
  newCount: number;
  unreadCount: number;
  attentionCount: number;
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
  isNew: boolean;
  readAt: string;
  playedAt: string;
  isUnread: boolean;
}

export interface PodcastEpisodePage {
  items: PodcastEpisode[];
  total: number;
}

export type PodcastEpisodeView = "all" | "unread";

export const PODCAST_LIBRARY_KEY = ["/api/podcasts"] as const;
export const PODCAST_EPISODES_KEY = "/api/podcasts/episodes";
const PODCAST_ID = /^[0-9a-f]{32}$/;

export function isPodcastIdentifier(value: string): boolean {
  return PODCAST_ID.test(value);
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

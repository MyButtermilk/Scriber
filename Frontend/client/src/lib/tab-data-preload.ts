import type { QueryClient } from "@tanstack/react-query";
import {
  fetchTranscriptHistoryPage,
  TRANSCRIPT_HISTORY_PAGE_SIZE,
  transcriptHistoryQueryKey,
  type TranscriptHistoryPage,
  type TranscriptHistoryType,
} from "@/hooks/use-transcript-history-query";
import { loadSettingsBootstrap } from "@/lib/settings-bootstrap";
import type { TranscriptHistoryItem } from "@/lib/api-types";
import type { MeetingsResponse } from "@/lib/api-types";
import { ACTIVE_MEETING_QUERY_PATH } from "@/lib/meeting-cache";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

const PRIMARY_HISTORY_TYPES = ["mic", "youtube", "file"] as const satisfies readonly TranscriptHistoryType[];

export function preloadPrimaryTabData(queryClient: QueryClient): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }

  let cancelled = false;
  const cancelIdle = scheduleIdle(() => {
    void warmPrimaryTabData(queryClient, () => cancelled);
  });

  return () => {
    cancelled = true;
    cancelIdle();
  };
}

async function warmPrimaryTabData(queryClient: QueryClient, isCancelled: () => boolean) {
  if (isCancelled()) return;
  // Settings/device discovery, active Meeting state, and history are independent.
  // Keep history requests sequential and yield between them, but do not let a
  // slow device or Meeting response block every primary tab's history cache.
  await Promise.all([
    warmSettings(queryClient, isCancelled),
    warmActiveMeeting(queryClient, isCancelled),
    warmTranscriptHistories(queryClient, isCancelled),
  ]);
}

async function warmSettings(queryClient: QueryClient, isCancelled: () => boolean) {
  const bootstrap = await loadSettingsBootstrap().catch(() => null);
  if (bootstrap && !isCancelled()) {
    queryClient.setQueryData(["/api/settings"], bootstrap.settings);
  }
}

async function warmActiveMeeting(queryClient: QueryClient, isCancelled: () => boolean) {
  if (!isCancelled()) {
    await queryClient
      .prefetchQuery({
        queryKey: ["/api/meetings"],
        queryFn: async ({ signal }) => {
          const response = await fetchWithTimeout(
            apiUrl(ACTIVE_MEETING_QUERY_PATH),
            {
              credentials: "include",
              signal,
            },
            10_000,
          );
          if (!response.ok) throw new Error("Failed to preload meetings");
          return response.json() as Promise<MeetingsResponse>;
        },
        staleTime: 10_000,
      })
      .catch(() => undefined);
  }
}

async function warmTranscriptHistories(queryClient: QueryClient, isCancelled: () => boolean) {
  for (const type of PRIMARY_HISTORY_TYPES) {
    if (isCancelled()) return;
    await queryClient
      .prefetchInfiniteQuery({
        queryKey: transcriptHistoryQueryKey(type, ""),
        queryFn: async ({ pageParam, signal }) =>
          fetchTranscriptHistoryPage<TranscriptHistoryItem>({
            type,
            q: "",
            offset: typeof pageParam === "number" ? pageParam : 0,
            pageSize: TRANSCRIPT_HISTORY_PAGE_SIZE,
            signal,
          }),
        initialPageParam: 0,
        getNextPageParam: (lastPage: TranscriptHistoryPage<TranscriptHistoryItem>) => {
          const nextOffset = lastPage.offset + lastPage.items.length;
          if (!lastPage.hasMore || nextOffset <= lastPage.offset) {
            return undefined;
          }
          return nextOffset;
        },
        staleTime: Infinity,
      })
      .catch(() => undefined);
    await nextFrame();
  }
}

function scheduleIdle(callback: () => void): () => void {
  if ("requestIdleCallback" in window && typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(callback, { timeout: 3000 });
    return () => window.cancelIdleCallback(handle);
  }

  const handle = window.setTimeout(callback, 600);
  return () => window.clearTimeout(handle);
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve());
  });
}

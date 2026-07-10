import { useInfiniteQuery, type InfiniteData } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { responseErrorMessage } from "@/lib/request-errors";

export type TranscriptHistoryType = "mic" | "file" | "youtube";

export interface TranscriptHistoryPage<TItem> {
  items: TItem[];
  total: number;
  offset: number;
  limit: number;
  hasMore: boolean;
}

export const TRANSCRIPT_HISTORY_PAGE_SIZE = 50;

export function transcriptHistoryQueryKey(type: TranscriptHistoryType, q: string) {
  return ["/api/transcripts", { q, type, infinite: true }] as const;
}

export async function fetchTranscriptHistoryPage<TItem>({
  type,
  q,
  offset,
  pageSize = TRANSCRIPT_HISTORY_PAGE_SIZE,
  signal,
}: {
  type: TranscriptHistoryType;
  q: string;
  offset: number;
  pageSize?: number;
  signal?: AbortSignal;
}): Promise<TranscriptHistoryPage<TItem>> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  params.set("type", type);
  params.set("offset", String(offset));
  params.set("limit", String(pageSize));

  const res = await fetchWithTimeout(
    apiUrl(`/api/transcripts?${params}`),
    { credentials: "include", signal },
    10_000,
  );
  if (!res.ok) {
    throw new Error(await responseErrorMessage(res));
  }

  return (await res.json()) as TranscriptHistoryPage<TItem>;
}

interface UseTranscriptHistoryQueryOptions {
  type: TranscriptHistoryType;
  q: string;
  pageSize?: number;
}

export function useTranscriptHistoryQuery<TItem>({
  type,
  q,
  pageSize = TRANSCRIPT_HISTORY_PAGE_SIZE,
}: UseTranscriptHistoryQueryOptions) {
  const query = useInfiniteQuery<TranscriptHistoryPage<TItem>, Error>({
    queryKey: transcriptHistoryQueryKey(type, q),
    queryFn: async ({ pageParam, signal }) => {
      const offset = typeof pageParam === "number" ? pageParam : 0;
      return fetchTranscriptHistoryPage<TItem>({ type, q, offset, pageSize, signal });
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.items.length;
      if (!lastPage.hasMore || nextOffset <= lastPage.offset) {
        return undefined;
      }
      return nextOffset;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: (previous) => previous,
  });

  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const total = query.data?.pages[0]?.total ?? 0;

  return {
    ...query,
    items,
    total,
  };
}

export function prependTranscriptHistoryItem<TItem extends { id?: string }>(
  previous: InfiniteData<TranscriptHistoryPage<TItem>, number> | undefined,
  item: TItem,
): InfiniteData<TranscriptHistoryPage<TItem>, number> {
  if (!previous) {
    return {
      pages: [
        {
          items: [item],
          total: 1,
          offset: 0,
          limit: TRANSCRIPT_HISTORY_PAGE_SIZE,
          hasMore: false,
        },
      ],
      pageParams: [0],
    };
  }

  const alreadyPresent = previous.pages.some((page) =>
    page.items.some((existing) => existing?.id && existing.id === item.id),
  );
  if (alreadyPresent) {
    return previous;
  }

  const nextTotal = Math.max(
    (previous.pages[0]?.total ?? 0) + 1,
    previous.pages.reduce((count, page) => count + page.items.length, 0) + 1,
  );
  const loadedItems = [item, ...previous.pages.flatMap((page) => page.items)];
  let loadedOffset = 0;
  const pages = previous.pages.map((page) => {
    const limit = Math.max(1, page.limit || TRANSCRIPT_HISTORY_PAGE_SIZE);
    const pageItems = loadedItems.slice(loadedOffset, loadedOffset + limit);
    const offset = loadedOffset;
    loadedOffset += pageItems.length;
    return {
      ...page,
      items: pageItems,
      total: nextTotal,
      offset,
      limit,
      hasMore: loadedOffset < nextTotal,
    };
  });

  return {
    ...previous,
    pages,
    pageParams: pages.map((page) => page.offset),
  };
}

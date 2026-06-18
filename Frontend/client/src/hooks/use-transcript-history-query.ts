import { useInfiniteQuery, type InfiniteData } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
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
}: {
  type: TranscriptHistoryType;
  q: string;
  offset: number;
  pageSize?: number;
}): Promise<TranscriptHistoryPage<TItem>> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  params.set("type", type);
  params.set("offset", String(offset));
  params.set("limit", String(pageSize));

  const res = await fetch(apiUrl(`/api/transcripts?${params}`), { credentials: "include" });
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
    queryFn: async ({ pageParam }) => {
      const offset = typeof pageParam === "number" ? pageParam : 0;
      return fetchTranscriptHistoryPage<TItem>({ type, q, offset, pageSize });
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

  return {
    ...previous,
    pages: previous.pages.map((page, index) => ({
      ...page,
      total: page.total + 1,
      items: index === 0 ? [item, ...page.items] : page.items,
      hasMore: index === 0 ? page.hasMore || page.items.length >= page.limit : page.hasMore,
    })),
  };
}

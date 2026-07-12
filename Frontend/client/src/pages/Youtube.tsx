import { AlertCircle, ArrowRight, Clock, PlayCircle, Youtube as YoutubeIcon, Loader2, CheckCircle2, ThumbsUp, Eye, Square, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useLocation } from "wouter";
import { useState, useEffect, useMemo, useCallback, memo, useRef } from "react";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useToast } from "@/hooks/use-toast";
import { useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { PageIntro } from "@/components/page-intro";
import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";
import { VirtualTranscriptHistory } from "@/components/virtual-transcript-history";
import {
  prependTranscriptHistoryItem,
  transcriptHistoryQueryKey,
  type TranscriptHistoryPage,
  useTranscriptHistoryQuery,
} from "@/hooks/use-transcript-history-query";
import type {
  TranscriptDetailResponse,
  TranscriptHistoryItem,
  YouTubeSearchItem,
  YouTubeSearchResponse,
} from "@/lib/api-types";

type SortOption = "date" | "likes" | "views";
const VIEW_MODE_STORAGE_KEY = "scriber:view-mode";

function youtubeThumbnailSrc(thumbnailUrl?: string): string {
  const value = (thumbnailUrl || "").trim();
  return value ? apiUrl(`/api/youtube/thumbnail?url=${encodeURIComponent(value)}`) : "";
}

function isCompletedStep(step?: string): boolean {
  return /^(completed|complete|ready|done)$/i.test((step || "").trim());
}

function isVisiblyProcessing(item: TranscriptHistoryItem): boolean {
  return item.summaryStatus === "pending" || (item.status === "processing" && !isCompletedStep(item.step));
}

type YoutubeHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";

function youtubeHistoryStatus(item: TranscriptHistoryItem): YoutubeHistoryStatus {
  if (isVisiblyProcessing(item)) return "processing";
  if (item.status === "failed") return "failed";
  if (item.summaryStatus === "failed") return "summary_failed";
  if (item.status === "stopped") return "stopped";
  return "ready";
}

interface YoutubeThumbnailProps {
  thumbnailUrl?: string;
  title?: string;
  className?: string;
  iconClassName?: string;
  loading?: "eager" | "lazy";
}

const YoutubeThumbnail = memo(function YoutubeThumbnail({
  thumbnailUrl,
  title,
  className = "w-full h-full object-cover",
  iconClassName = "w-8 h-8",
  loading = "lazy",
}: YoutubeThumbnailProps) {
  const [failed, setFailed] = useState(false);
  const src = failed ? "" : youtubeThumbnailSrc(thumbnailUrl);

  useEffect(() => {
    setFailed(false);
  }, [thumbnailUrl]);

  if (!src) {
    return (
      <div className="w-full h-full bg-secondary flex items-center justify-center">
        <YoutubeIcon className={`${iconClassName} text-muted-foreground/50`} />
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={title || "Video thumbnail"}
      className={`block ${className}`}
      decoding="async"
      loading={loading}
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
});

// Memoized YoutubeVideoCard to prevent unnecessary re-renders
interface YoutubeVideoCardProps {
  item: TranscriptHistoryItem;
  viewMode: "list" | "grid";
  isDeleting: boolean;
  isCopying: boolean;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onCopy: (e: React.MouseEvent, id: string) => void;
  onNavigate: (id: string) => void;
  onHover?: (id: string) => void;
}

const YoutubeVideoCard = memo(function YoutubeVideoCard({
  item,
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onNavigate,
  onHover,
}: YoutubeVideoCardProps) {
  const deletingClasses = isDeleting
    ? "pointer-events-none opacity-[0.55] scale-[0.985]"
    : "opacity-100 scale-100";
  const historyStatus = youtubeHistoryStatus(item);

  return (
    <div className="w-full">
      <Card
        className={`youtube-history-card perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid" : ""} group cursor-pointer overflow-hidden rounded-[20px] transform-gpu ${deletingClasses}`}
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
      >
        {viewMode === "list" ? (
          // List view
          <div className="flex min-w-0 flex-col gap-3 overflow-hidden p-4 sm:flex-row sm:gap-4">
            <div className="relative aspect-video w-full shrink-0 overflow-hidden rounded-[12px] bg-muted sm:h-20 sm:w-32">
              <YoutubeThumbnail
                thumbnailUrl={item.thumbnailUrl}
                title={item.title}
                className="h-full w-full object-cover opacity-90 transition-transform duration-700 ease-[cubic-bezier(0.32,0.72,0,1)] group-hover:scale-[1.04]"
                iconClassName="w-8 h-8"
              />
              <div className="absolute bottom-1 right-1 bg-black/80 text-white text-[10px] px-1 rounded">
                {item.duration}
              </div>
            </div>

            <div className="min-w-0 flex-1 overflow-hidden">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <h3 className="min-w-0 flex-1 basis-[12rem]">
                  <button
                    type="button"
                    className="line-clamp-2 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.4] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                    onClick={(event) => {
                      event.stopPropagation();
                      onNavigate(item.id);
                    }}
                  >
                    {item.title}
                  </button>
                </h3>
                {historyStatus === "processing" ? (
                  <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1 shrink-0">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    {item.step || "Processing"}
                  </Badge>
                ) : historyStatus === "failed" ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px] shrink-0">Failed</Badge>
                ) : historyStatus === "summary_failed" ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px] flex items-center gap-1 shrink-0">
                    <AlertCircle className="w-3 h-3" />
                    Summary failed
                  </Badge>
                ) : historyStatus === "stopped" ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px] shrink-0">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full shrink-0">
                    <CheckCircle2 className="w-3 h-3" />
                    Ready
                  </div>
                )}
              </div>
              <p className="mt-1 truncate text-[12px] text-muted-foreground">{item.channel || item.channelTitle || "Unknown Channel"} • {item.date}</p>
            </div>

            <div className="flex items-center justify-end gap-1 sm:self-center">
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title="Copy transcript"
                ariaLabel={`Copy transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title="Delete transcript"
                ariaLabel={`Delete transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        ) : (
          // Grid view
          <div className="flex flex-col">
            <div className="relative aspect-video w-full overflow-hidden bg-muted">
              <YoutubeThumbnail
                thumbnailUrl={item.thumbnailUrl}
                title={item.title}
                className="h-full w-full object-cover transition-transform duration-700 ease-[cubic-bezier(0.32,0.72,0,1)] group-hover:scale-[1.045]"
                iconClassName="w-12 h-12"
              />
              <div className="absolute bottom-2 right-2 bg-black/80 text-white text-xs px-1.5 py-0.5 rounded">
                {item.duration}
              </div>
              <div className="absolute top-2 right-2">
                {historyStatus === "processing" ? (
                  <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50/90 text-[10px] flex items-center gap-1">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    Processing
                  </Badge>
                ) : historyStatus === "failed" ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50/90 text-[10px]">Failed</Badge>
                ) : historyStatus === "summary_failed" ? (
                  <Badge
                    variant="outline"
                    className="text-red-600 border-red-200 bg-red-50/90 text-[10px] flex items-center gap-1"
                    title="Summary failed"
                    aria-label="Summary failed"
                  >
                    <AlertCircle className="w-3 h-3" />
                    Summary
                  </Badge>
                ) : historyStatus === "stopped" ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50/90 text-[10px]">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 rounded-full bg-green-50/90 px-2 py-1 text-[10px] font-medium text-green-600 dark:bg-green-950/70 dark:text-green-300">
                    <CheckCircle2 className="w-3 h-3" />
                    Ready
                  </div>
                )}
              </div>
            </div>
            <div className="p-4">
              <h3>
                <button
                  type="button"
                  className="line-clamp-2 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.35] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                  onClick={(event) => {
                    event.stopPropagation();
                    onNavigate(item.id);
                  }}
                >
                  {item.title}
                </button>
              </h3>
              <p className="text-xs text-muted-foreground mt-1 truncate">{item.channel || item.channelTitle || "Unknown"}</p>
              <div className="flex items-center justify-between mt-2">
                <span className="text-xs text-muted-foreground">{item.date}</span>
                <div className="flex items-center gap-1">
                  <CopyActionButton
                    onClick={(e) => onCopy(e, item.id)}
                    disabled={isCopying}
                    copied={isCopying}
                    title="Copy transcript"
                    ariaLabel={`Copy transcript ${item.title}`}
                    size="sm"
                    className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
                  />
                  <DeleteActionButton
                    onClick={(e) => onDelete(e, item.id)}
                    disabled={isDeleting}
                    loading={isDeleting}
                    title="Delete transcript"
                    ariaLabel={`Delete transcript ${item.title}`}
                    size="sm"
                    className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
                  />
                </div>
              </div>
            </div>
          </div>
        )
        }
      </Card>
    </div>
  );
});

export default function Youtube() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [query, setQuery] = useUrlQueryState("search", "", {
    parse: (raw) => raw ?? "",
    serialize: (value) => {
      const trimmed = value.trim();
      return trimmed ? trimmed : null;
    },
    syncDelayMs: 250,
  });
  const [searchResults, setSearchResults] = useState<YouTubeSearchItem[]>([]);
  const [searchError, setSearchError] = useState<string>("");
  const [searchEmpty, setSearchEmpty] = useState(false);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [startError, setStartError] = useState<string>("");
  const [lastFailedStartItem, setLastFailedStartItem] = useState<YouTubeSearchItem | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [startingVideoId, setStartingVideoId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const searchRequestInFlightRef = useRef(false);
  const startRequestInFlightRef = useRef<string | null>(null);
  const deletingRef = useRef<string | null>(null);
  const copyingRef = useRef<string | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const [sortBy, setSortBy] = useUrlQueryState<SortOption>("sort", "date", {
    parse: (raw) => (raw === "likes" || raw === "views" ? raw : "date"),
  });
  const queryClient = useQueryClient();
  const getInitialViewMode = () => {
    if (typeof window === "undefined") return "list" as const;
    const stored = window.localStorage.getItem(VIEW_MODE_STORAGE_KEY);
    if (stored === "list" || stored === "grid") return stored;
    return "list" as const;
  };
  const initialViewMode = getInitialViewMode();
  const [viewMode, setViewMode] = useUrlQueryState<"list" | "grid">("view", initialViewMode, {
    parse: (raw) => (raw === "list" || raw === "grid" ? raw : initialViewMode),
  });

  // History search state
  const [historySearch, setHistorySearch] = useUrlQueryState("q", "", {
    parse: (raw) => raw ?? "",
    serialize: (value) => {
      const trimmed = value.trim();
      return trimmed ? trimmed : null;
    },
    syncDelayMs: 250,
  });
  const [debouncedHistorySearch, setDebouncedHistorySearch] = useState("");

  // Debounce history search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedHistorySearch(historySearch), 300);
    return () => clearTimeout(timer);
  }, [historySearch]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, viewMode);
  }, [viewMode]);

  useEffect(() => () => {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
  }, []);

  const transcriptsQueryKey = useMemo(
    () => transcriptHistoryQueryKey("youtube", debouncedHistorySearch),
    [debouncedHistorySearch],
  );

  // Sort search results
  const sortedResults = useMemo(() => {
    if (!searchResults.length) return [];
    return [...searchResults].sort((a, b) => {
      switch (sortBy) {
        case "date":
          return new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime();
        case "likes":
          return (b.likeCount || 0) - (a.likeCount || 0);
        case "views":
          return (b.viewCount || 0) - (a.viewCount || 0);
        default:
          return 0;
      }
    });
  }, [searchResults, sortBy]);

  const transcriptsQuery = useTranscriptHistoryQuery<TranscriptHistoryItem>({ type: "youtube", q: debouncedHistorySearch });
  const recentVideos = transcriptsQuery.items;

  useTranscriptAutoRefresh({
    queryKey: transcriptsQueryKey,
    onError: (message) => {
      toast({
        title: "Transcription Error",
        description: message,
        variant: "destructive",
        duration: 6000,
      });
    },
  });

  // Helper to detect if input is a YouTube URL
  const isYouTubeUrl = (input: string): boolean => {
    return /(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/|youtube\.com\/shorts\/|youtube\.com\/live\/)/i.test(input);
  };

  const runSearch = async () => {
    const q = query.trim();
    if (!q || searchRequestInFlightRef.current) return;

    searchRequestInFlightRef.current = true;
    setIsSearching(true);
    setSubmittedQuery(q);
    setSearchResults([]);
    setSearchError("");
    setSearchEmpty(false);
    setStartError("");
    setLastFailedStartItem(null);

    try {
      // Check if input is a YouTube URL
      if (isYouTubeUrl(q)) {
        // Fetch video directly by URL
        const url = apiUrl(`/api/youtube/video?url=${encodeURIComponent(q)}`);
        const res = await fetchWithTimeout(url, { credentials: "include" }, 30_000);
        if (!res.ok) {
          throw new Error(await responseErrorMessage(res));
        }
        const video = (await res.json()) as YouTubeSearchItem;
        if (video.videoId) {
          setSearchResults([video]);
        } else {
          setSearchEmpty(true);
        }
      } else {
        // Regular search
        const url = apiUrl(`/api/youtube/search?q=${encodeURIComponent(q)}&maxResults=10`);
        const res = await fetchWithTimeout(url, { credentials: "include" }, 30_000);
        if (!res.ok) {
          throw new Error(await responseErrorMessage(res));
        }
        const payload = (await res.json()) as YouTubeSearchResponse;
        const items = payload.items || [];
        setSearchResults(items);
        setSearchEmpty(items.length === 0);
      }
    } catch (e: any) {
      const msg = friendlyError(e, "YouTube lookup failed.");
      setSearchError(msg);
    } finally {
      searchRequestInFlightRef.current = false;
      setIsSearching(false);
    }
  };

  const startTranscription = async (item: YouTubeSearchItem) => {
    if (!item?.url || startRequestInFlightRef.current) return;
    const requestKey = item.videoId || item.url;
    startRequestInFlightRef.current = requestKey;
    setStartError("");
    setLastFailedStartItem(null);
    setStartingVideoId(requestKey);

    try {
      const res = await fetchWithTimeout(apiUrl("/api/youtube/transcribe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          url: item.url,
          title: item.title,
          channelTitle: item.channelTitle,
          thumbnailUrl: item.thumbnailUrl,
          duration: item.duration,
          videoId: item.videoId,
        }),
      }, 15_000);
      if (!res.ok) {
        throw new Error(await responseErrorMessage(res));
      }
      const rec = (await res.json()) as TranscriptHistoryItem;
      if (rec?.id) {
        if (!debouncedHistorySearch) {
          queryClient.setQueryData<InfiniteData<TranscriptHistoryPage<TranscriptHistoryItem>, number>>(
            transcriptsQueryKey,
            (previous) => {
              const optimistic: TranscriptHistoryItem = {
                ...rec,
                type: rec.type || "youtube",
                title: rec.title || item.title,
                channel: rec.channel || item.channelTitle || "",
                thumbnailUrl: rec.thumbnailUrl || item.thumbnailUrl || "",
                duration: rec.duration || item.duration || "",
                status: rec.status || "processing",
                step: rec.step || "Queued",
              };

              return prependTranscriptHistoryItem(previous, optimistic);
            },
          );
        }

        queryClient.invalidateQueries({
          predicate: (query) =>
            query.queryKey[0] === "/api/transcripts" &&
            (query.queryKey[1] as { type?: string })?.type === "youtube",
        });
        setLocation(`/transcript/${rec.id}`);
      }
    } catch (e: any) {
      const msg = friendlyError(e, "Failed to start transcription.");
      setStartError(msg);
      setLastFailedStartItem(item);
      toast({
        title: "Failed to start transcription",
        description: msg,
        variant: "destructive",
        duration: 4000,
      });
    } finally {
      startRequestInFlightRef.current = null;
      setStartingVideoId(null);
    }
  };

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingRef.current) return;

    deletingRef.current = id;
    setDeletingId(id);
    try {
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      }, 15_000);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.message || res.statusText);
      }
      toast({
        title: "Deleted",
        description: "Transcript removed successfully.",
        duration: 2000,
      });
      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" &&
          (query.queryKey[1] as { type?: string })?.type === "youtube",
      });
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      deletingRef.current = null;
      setDeletingId(null);
    }
  }, [queryClient, toast]);

  const copyTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (copyingRef.current) return;

    copyingRef.current = id;
    setCopyingId(id);
    try {
      // Fetch the full transcript content
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${id}`), {
        credentials: "include",
      }, 15_000);
      if (!res.ok) {
        throw new Error(res.statusText);
      }
      const data = (await res.json()) as TranscriptDetailResponse;
      const content = data?.content || "";
      if (!content) {
        throw new Error("No transcript content available");
      }
      await navigator.clipboard.writeText(content);
      toast({
        title: "Copied",
        description: "Transcript copied to clipboard.",
        duration: 2000,
      });
      // Show check mark briefly
      copyResetTimerRef.current = window.setTimeout(() => {
        copyingRef.current = null;
        copyResetTimerRef.current = null;
        setCopyingId(null);
      }, 1500);
    } catch (e: any) {
      toast({
        title: "Copy failed",
        description: String(e?.message || e),
        duration: 4000,
      });
      copyingRef.current = null;
      setCopyingId(null);
    }
  }, [toast]);

  const navigateToTranscript = useCallback((id: string) => {
    setLocation(`/transcript/${id}`);
  }, [setLocation]);

  // Preload TranscriptDetail page and data on hover for instant navigation
  const preloadTranscript = useCallback((id: string) => {
    import("@/pages/TranscriptDetail");
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  return (
    <div className="transcription-page youtube-page mx-auto w-full max-w-[1320px] px-4 py-5 md:px-6 md:py-6">
      <PageIntro
        eyebrow="Media capture · 02"
        title="YouTube transcription"
        description="Paste a link or search YouTube, then turn the video into a searchable transcript."
        accentClassName="bg-red-500/70"
        sticky={false}
      />

      {/* Media discovery command bar */}
      <div className="youtube-search-shell mb-7">
        <div className="youtube-search-core">
          <form
            className="flex items-center gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              void runSearch();
            }}
            aria-label="Find a YouTube video"
          >
            <div className="youtube-search-mark flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] text-red-500">
              <YoutubeIcon className="h-5 w-5 stroke-[1.65px]" aria-hidden="true" />
            </div>
            <div className="relative min-w-0 flex-1">
              <label htmlFor="youtube-source-search" className="sr-only">YouTube URL or search terms</label>
              <Input
                id="youtube-source-search"
                className="h-12 border-0 bg-transparent pr-10 text-[15px] shadow-none focus-visible:ring-0"
                placeholder="Paste a YouTube link or search videos..."
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                autoComplete="off"
              />
              {query && !isSearching ? (
                <button
                  type="button"
                  className="absolute right-0 top-1/2 inline-flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-[10px] text-muted-foreground outline-none transition-colors hover:bg-foreground/[0.06] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/60"
                  onClick={() => setQuery("")}
                  aria-label="Clear YouTube search"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              ) : null}
            </div>
            <Button
              className="group h-11 w-11 shrink-0 rounded-[12px] px-0 shadow-[0_12px_28px_-18px_hsl(var(--primary))] transition-transform duration-200 active:scale-[0.97] sm:w-auto sm:px-4"
              disabled={!query.trim() || isSearching}
              type="submit"
              aria-label={isSearching ? "Searching YouTube" : "Find video"}
              aria-busy={isSearching}
            >
              {isSearching ? (
                <Loader2 className="h-[17px] w-[17px] animate-spin" aria-hidden="true" />
              ) : (
                <ArrowRight className="h-[17px] w-[17px] stroke-[1.7px] transition-transform duration-200 group-hover:translate-x-0.5" aria-hidden="true" />
              )}
              <span className="hidden sm:inline">{isSearching ? "Searching" : "Find video"}</span>
            </Button>
          </form>
          <div className="youtube-search-foot flex items-center justify-between gap-3 px-1 pt-3 text-[10.5px] text-muted-foreground">
            <span>Paste one link, or search by title and channel</span>
            <span className="hidden font-mono tabular-nums sm:inline">Enter ↵</span>
          </div>
        </div>
      </div>

      {/* Search Results */}
      {(isSearching || searchError || searchEmpty || searchResults.length > 0) && (
        <div className="transcription-results mb-8 space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="font-heading text-[20px] font-semibold tracking-[-0.02em]">Search results</h2>
                {!isSearching && searchResults.length > 0 ? (
                  <span className="transcription-history-count inline-flex h-6 min-w-6 items-center justify-center rounded-[8px] px-2 font-mono text-[10.5px] font-semibold tabular-nums text-muted-foreground">
                    {searchResults.length}
                  </span>
                ) : null}
              </div>
              <p className="mt-1 max-w-[60ch] text-pretty text-[12px] text-muted-foreground">
                {isSearching ? `Looking for “${submittedQuery}”` : `Results for “${submittedQuery}”`}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {searchResults.length > 0 && !isSearching && (
                <Select value={sortBy} onValueChange={(v) => setSortBy(v as SortOption)}>
                  <SelectTrigger className="h-9 w-[150px] text-xs" aria-label="Sort search results">
                    <SelectValue placeholder="Sort by" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="date">
                      <span className="flex items-center gap-2">
                        <Clock className="w-3 h-3" /> Most Recent
                      </span>
                    </SelectItem>
                    <SelectItem value="likes">
                      <span className="flex items-center gap-2">
                        <ThumbsUp className="w-3 h-3" /> Most Liked
                      </span>
                    </SelectItem>
                    <SelectItem value="views">
                      <span className="flex items-center gap-2">
                        <Eye className="w-3 h-3" /> Most Viewed
                      </span>
                    </SelectItem>
                  </SelectContent>
                </Select>
              )}
            </div>
          </div>

          {!isSearching && searchError && (
            <QueryErrorState
              title="YouTube search failed"
              description={searchError}
              onRetry={query.trim() ? runSearch : undefined}
              className="mx-2"
            />
          )}

          {!isSearching && searchEmpty && (
            <div className="transcription-neutral-state rounded-[18px] px-5 py-6 text-center" role="status">
              <p className="font-heading text-[15px] font-semibold text-foreground">No videos found</p>
              <p className="mx-auto mt-1 max-w-[52ch] text-[12px] leading-5 text-muted-foreground">
                Try a more specific title, a channel name, or paste the full YouTube URL.
              </p>
            </div>
          )}

          {isSearching && (
            <div className="grid gap-4 xl:grid-cols-2" aria-live="polite" aria-busy="true">
              {[0, 1].map((item) => (
                <div key={item} className="youtube-result-card min-h-[116px] animate-pulse rounded-[18px] p-4">
                  <div className="flex gap-4">
                    <div className="h-20 w-32 shrink-0 rounded-[10px] bg-foreground/[0.07]" />
                    <div className="flex-1 space-y-3 py-1">
                      <div className="h-4 w-3/4 rounded bg-foreground/[0.08]" />
                      <div className="h-3 w-1/2 rounded bg-foreground/[0.06]" />
                      <div className="h-3 w-1/3 rounded bg-foreground/[0.05]" />
                    </div>
                  </div>
                </div>
              ))}
              <span className="sr-only">Searching YouTube</span>
            </div>
          )}

          {!isSearching && startError && (
            <QueryErrorState
              title="Could not start transcription"
              description={startError}
              onRetry={lastFailedStartItem ? () => startTranscription(lastFailedStartItem) : undefined}
              className="mx-2"
            />
          )}

          {sortedResults.length > 0 && (
            <div className="grid gap-4 xl:grid-cols-2">
              {sortedResults.map((item) => {
                const published = item.publishedAt ? new Date(item.publishedAt).toLocaleDateString() : "";
                const isStarting = startingVideoId === (item.videoId || item.url);
                return (
                  <Card
                    key={item.videoId}
                    className={`youtube-result-card perf-scroll-item group overflow-hidden rounded-[20px] ${isStarting ? "cursor-wait opacity-75" : "cursor-pointer"}`}
                    onClick={() => {
                      if (!isStarting) void startTranscription(item);
                    }}
                    role="button"
                    tabIndex={isStarting ? -1 : 0}
                    aria-label={`Start transcription for ${item.title || "video"}`}
                    aria-busy={isStarting}
                    aria-disabled={isStarting}
                    onKeyDown={(e) => {
                      if (!isStarting && (e.key === "Enter" || e.key === " ")) {
                        e.preventDefault();
                        startTranscription(item);
                      }
                    }}
                  >
                    <div className="flex flex-col gap-4 p-4 sm:flex-row">
                      <div className="relative aspect-video w-full shrink-0 overflow-hidden rounded-[12px] bg-muted sm:h-20 sm:w-32">
                        <YoutubeThumbnail
                          thumbnailUrl={item.thumbnailUrl}
                          title={item.title}
                          className="h-full w-full object-cover opacity-90 transition-transform duration-700 ease-[cubic-bezier(0.32,0.72,0,1)] group-hover:scale-[1.045]"
                          iconClassName="w-8 h-8"
                          loading="eager"
                        />
                        <div className="absolute inset-0 flex items-center justify-center bg-black/20 group-hover:bg-black/10 transition-colors">
                          <PlayCircle className="w-8 h-8 text-white opacity-80" />
                        </div>
                        {!!item.duration && (
                          <div className="absolute bottom-1 right-1 bg-black/80 text-white text-[10px] px-1 rounded">
                            {item.duration}
                          </div>
                        )}
                      </div>

                      <div className="flex-1 min-w-0">
                        <div className="flex justify-between items-start">
                          <h3 className="line-clamp-2 pr-2 font-heading text-[15px] font-medium leading-[1.35] text-foreground">
                            {item.title || "Untitled"}
                          </h3>
                          <Badge variant="outline" className="text-[10px]">
                            {isStarting ? "Starting…" : "Transcribe"}
                          </Badge>
                        </div>
                        <p className="text-sm text-muted-foreground mt-1">
                          {item.channelTitle || "Unknown Channel"}
                          {published ? ` · ${published}` : ""}
                        </p>

                        <div className="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
                          <span className="flex items-center gap-1">
                            <Clock className="w-3 h-3" /> {item.duration || "—"}
                          </span>
                          {item.viewCount !== undefined && item.viewCount > 0 && (
                            <span className="flex items-center gap-1">
                              <Eye className="w-3 h-3" /> {item.viewCount.toLocaleString()}
                            </span>
                          )}
                          {item.likeCount !== undefined && item.likeCount > 0 && (
                            <span className="flex items-center gap-1">
                              <ThumbsUp className="w-3 h-3" /> {item.likeCount.toLocaleString()}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </Card>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Recent History */}
      <div className="transcription-history space-y-4">
        <TranscriptionHistoryToolbar
          title="Recent videos"
          description="Search, copy, or reopen your latest video transcripts."
          total={transcriptsQuery.total}
          itemLabel={transcriptsQuery.total === 1 ? "video" : "videos"}
          searchValue={historySearch}
          onSearchChange={setHistorySearch}
          searchPlaceholder="Search videos..."
          searchAriaLabel="Search YouTube transcript history"
          clearSearchLabel="Clear YouTube history search"
          viewMode={viewMode}
          onViewModeChange={setViewMode}
        />

        <div className="w-full py-2">
          {transcriptsQuery.isLoading ? (
            <SkeletonList count={3} variant={viewMode} />
          ) : transcriptsQuery.isError ? (
            <QueryErrorState
              title="Could not load recent videos"
              description="Please retry loading your YouTube transcript history."
              onRetry={() => transcriptsQuery.refetch()}
            />
          ) : recentVideos.length === 0 ? (
            debouncedHistorySearch ? (
              <p className="text-center text-muted-foreground py-8">No videos match "{debouncedHistorySearch}"</p>
            ) : (
              <EmptyState type="youtube" />
            )
          ) : (
            <VirtualTranscriptHistory
              items={recentVideos}
              viewMode={viewMode}
              getItemKey={(item) => item.id}
              hasMore={transcriptsQuery.hasNextPage}
              isLoadingMore={transcriptsQuery.isFetchingNextPage}
              onLoadMore={() => transcriptsQuery.fetchNextPage()}
              renderItem={(item) => (
                <YoutubeVideoCard
                  item={item}
                  viewMode={viewMode}
                  isDeleting={deletingId === item.id}
                  isCopying={copyingId === item.id}
                  onDelete={deleteTranscript}
                  onCopy={copyTranscript}
                  onNavigate={navigateToTranscript}
                  onHover={preloadTranscript}
                />
              )}
            />
          )}
        </div>
      </div>
    </div>
  );
}


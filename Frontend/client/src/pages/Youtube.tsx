import { ArrowRight, Clock, PlayCircle, Youtube as YoutubeIcon, Loader2, CheckCircle2, ThumbsUp, Eye, LayoutGrid, LayoutList, Square, Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useLocation } from "wouter";
import { useState, useEffect, useMemo, useCallback, memo, useRef } from "react";
import { apiUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion, useReducedMotion } from "framer-motion";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";

type YouTubeSearchItem = {
  videoId: string;
  url: string;
  title: string;
  description: string;
  channelTitle: string;
  publishedAt: string;
  thumbnailUrl: string;
  duration: string;
  durationSeconds: number;
  viewCount?: number;
  likeCount?: number;
};

type SortOption = "date" | "likes" | "views";
const DELETE_GLITCH_DURATION_MS = 1200;
const VIEW_MODE_STORAGE_KEY = "scriber:view-mode";

// Memoized YoutubeVideoCard to prevent unnecessary re-renders
interface YoutubeVideoCardProps {
  item: any;
  index: number;
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
  index,
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onNavigate,
  onHover,
}: YoutubeVideoCardProps) {
  const prefersReducedMotion = useReducedMotion();
  const durationClass = "duration-[1200ms]";
  const listLayoutClasses = `grid transition-[grid-template-rows,margin-bottom] ease-in-out ${durationClass} ${isDeleting
    ? "grid-rows-[0fr] mb-0 overflow-hidden"
    : "grid-rows-[1fr] mb-4 last:mb-0 overflow-visible"
    }`;
  const layoutClasses = viewMode === "list" ? listLayoutClasses : "block";
  const visualClasses = `!transition-all !ease-out !duration-[1200ms] w-full origin-top transform-gpu ${isDeleting
    ? "hue-rotate-180 saturate-200 blur-md skew-x-[40deg] scale-y-50 translate-x-12 opacity-0"
    : "hue-rotate-0 saturate-100 blur-0 skew-x-0 scale-y-100 translate-x-0 opacity-100"
    }`;

  return (
    <motion.div
      layout="position"
      initial={prefersReducedMotion ? false : { opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: Math.min(index * 0.02, 0.1),
        duration: prefersReducedMotion ? 0 : 0.2,
        ease: "easeOut",
        layout: { duration: prefersReducedMotion ? 0 : 0.45, ease: "easeInOut" },
      }}
      className={layoutClasses}
    >
      <Card
        className={`neu-recording-row perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid" : ""} overflow-hidden rounded-[20px] group cursor-pointer ${visualClasses}`}
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
        role="button"
        tabIndex={0}
        aria-label={`Open transcript ${item.title}`}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onNavigate(item.id);
          }
        }}
      >
        {viewMode === "list" ? (
          // List view
          <div className="flex gap-4 p-4 min-w-0 overflow-hidden">
            <div className="relative w-32 h-20 bg-muted rounded-md shrink-0 overflow-hidden">
              {item.thumbnailUrl ? (
                <img
                  src={item.thumbnailUrl}
                  alt={item.title || "Thumbnail"}
                  className="w-full h-full object-cover opacity-90"
                  loading="lazy"
                />
              ) : (
                <div className="w-full h-full bg-secondary flex items-center justify-center">
                  <YoutubeIcon className="w-8 h-8 text-muted-foreground/50" />
                </div>
              )}
              <div className="absolute bottom-1 right-1 bg-black/80 text-white text-[10px] px-1 rounded">
                {item.duration}
              </div>
            </div>

            <div className="flex-1 min-w-0 overflow-hidden">
              <div className="flex justify-between items-start gap-2">
                <h3 className="font-medium text-foreground truncate text-base flex-1 min-w-0">{item.title}</h3>
                {item.status === 'processing' ? (
                  <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1 shrink-0">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    {item.step || "Processing"}
                  </Badge>
                ) : item.status === 'failed' ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px] shrink-0">Failed</Badge>
                ) : item.status === 'stopped' ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px] shrink-0">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full shrink-0">
                    <CheckCircle2 className="w-3 h-3" />
                    Ready
                  </div>
                )}
              </div>
              <p className="text-sm text-muted-foreground mt-1 truncate">{item.channel || item.channelTitle || "Unknown Channel"} • {item.date}</p>
            </div>

            <div className="flex items-center gap-1">
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
            <div className="relative w-full aspect-video bg-muted overflow-hidden">
              {item.thumbnailUrl ? (
                <img
                  src={item.thumbnailUrl}
                  alt={item.title || "Thumbnail"}
                  className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                  loading="lazy"
                />
              ) : (
                <div className="w-full h-full bg-secondary flex items-center justify-center">
                  <YoutubeIcon className="w-12 h-12 text-muted-foreground/50" />
                </div>
              )}
              <div className="absolute bottom-2 right-2 bg-black/80 text-white text-xs px-1.5 py-0.5 rounded">
                {item.duration}
              </div>
              <div className="absolute top-2 right-2">
                {item.status === 'processing' ? (
                  <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50/90 text-[10px] flex items-center gap-1">
                    <Loader2 className="w-3 h-3 animate-spin" />
                  </Badge>
                ) : item.status === 'failed' ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50/90 text-[10px]">Failed</Badge>
                ) : item.status === 'stopped' ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50/90 text-[10px]">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50/90 px-2 py-1 rounded-full">
                    <CheckCircle2 className="w-3 h-3" />
                  </div>
                )}
              </div>
            </div>
            <div className="p-3">
              <h3 className="font-medium text-foreground line-clamp-2 text-sm group-hover:text-primary transition-colors">{item.title}</h3>
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
    </motion.div >
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
  const [startError, setStartError] = useState<string>("");
  const [lastFailedStartItem, setLastFailedStartItem] = useState<YouTubeSearchItem | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [startingVideoId, setStartingVideoId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const deletingRef = useRef<string | null>(null);
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

  const transcriptsQueryKey = useMemo(
    () => ["/api/transcripts", { q: debouncedHistorySearch, type: "youtube" }] as const,
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

  const transcriptsQuery = useQuery({
    queryKey: transcriptsQueryKey,
    queryFn: async () => {
      const params = new URLSearchParams();
      if (debouncedHistorySearch) params.set("q", debouncedHistorySearch);
      params.set("type", "youtube");
      const res = await fetch(apiUrl(`/api/transcripts?${params}`), { credentials: "include" });
      return res.json();
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: (previous) => previous,
  });
  const recentVideos: any[] = (transcriptsQuery.data as any)?.items || [];

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
    return /(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/|youtube\.com\/shorts\/)/i.test(input);
  };

  const runSearch = async () => {
    const q = query.trim();
    if (!q || isSearching) return;

    setIsSearching(true);
    setSearchError("");
    setStartError("");
    setLastFailedStartItem(null);

    try {
      // Check if input is a YouTube URL
      if (isYouTubeUrl(q)) {
        // Fetch video directly by URL
        const url = apiUrl(`/api/youtube/video?url=${encodeURIComponent(q)}`);
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) {
          throw new Error(await responseErrorMessage(res));
        }
        const video = await res.json();
        if (video && video.videoId) {
          setSearchResults([video as YouTubeSearchItem]);
        } else {
          setSearchError("Video not found.");
        }
      } else {
        // Regular search
        const url = apiUrl(`/api/youtube/search?q=${encodeURIComponent(q)}&maxResults=10`);
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) {
          throw new Error(await responseErrorMessage(res));
        }
        const payload = await res.json();
        const items = (payload?.items || []) as YouTubeSearchItem[];
        setSearchResults(items);
        if (!items.length) setSearchError("No results found.");
      }
    } catch (e: any) {
      const msg = friendlyError(e, "YouTube lookup failed.");
      setSearchError(msg);
      toast({
        title: "YouTube lookup failed",
        description: msg,
        duration: 4000,
      });
    } finally {
      setIsSearching(false);
    }
  };

  const startTranscription = async (item: YouTubeSearchItem) => {
    if (!item?.url || startingVideoId) return;
    setStartError("");
    setLastFailedStartItem(null);
    setStartingVideoId(item.videoId);

    try {
      const res = await fetch(apiUrl("/api/youtube/transcribe"), {
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
      });
      if (!res.ok) {
        throw new Error(await responseErrorMessage(res));
      }
      const rec = await res.json();
      if (rec?.id) {
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
      setStartingVideoId(null);
    }
  };

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingRef.current) return;

    deletingRef.current = id;
    setDeletingId(id);
    try {
      await new Promise((resolve) => setTimeout(resolve, DELETE_GLITCH_DURATION_MS));

      const res = await fetch(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      });
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
    if (copyingId) return;

    setCopyingId(id);
    try {
      // Fetch the full transcript content
      const res = await fetch(apiUrl(`/api/transcripts/${id}`), {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(res.statusText);
      }
      const data = await res.json();
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
      setTimeout(() => setCopyingId(null), 1500);
    } catch (e: any) {
      toast({
        title: "Copy failed",
        description: String(e?.message || e),
        duration: 4000,
      });
      setCopyingId(null);
    }
  }, [copyingId, toast]);

  const navigateToTranscript = useCallback((id: string) => {
    setLocation(`/transcript/${id}`);
  }, [setLocation]);

  // Preload TranscriptDetail page and data on hover for instant navigation
  const preloadTranscript = useCallback((id: string) => {
    import("@/pages/TranscriptDetail");
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  return (
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-6 space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Youtube Transcription</h1>
        <p className="text-muted-foreground">Paste a URL or search to transcribe video content</p>
      </header>

      {/* Input Section - Debossed neumorphic style */}
      <div className="neu-status-well p-2 mb-6 rounded-xl">
        <div className="flex items-center gap-2">
          <div className="pl-3 text-muted-foreground">
            <YoutubeIcon className="w-5 h-5" />
          </div>
          <Input
            className="border-0 shadow-none focus-visible:ring-0 bg-transparent text-base h-12"
            placeholder="Paste Youtube link or search videos..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                runSearch();
              }
            }}
          />
          <Button
            size="icon"
            className="h-10 w-10 shrink-0 rounded-lg"
            onClick={runSearch}
            disabled={!query.trim() || isSearching}
            type="button"
            aria-label="Search YouTube"
          >
            <ArrowRight className="w-5 h-5" />
          </Button>
        </div>
      </div>

      {/* Search Results */}
      {(isSearching || searchError || searchResults.length > 0) && (
        <div className="space-y-4 mb-8">
          <div className="flex items-center justify-between px-2">
            <h2 className="text-lg font-semibold">Search Results</h2>
            <div className="flex items-center gap-2">
              {isSearching && <span className="text-xs text-muted-foreground">Searching…</span>}
              {searchResults.length > 0 && !isSearching && (
                <Select value={sortBy} onValueChange={(v) => setSortBy(v as SortOption)}>
                  <SelectTrigger className="w-[140px] h-8 text-xs">
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

          {!isSearching && startError && (
            <QueryErrorState
              title="Could not start transcription"
              description={startError}
              onRetry={lastFailedStartItem ? () => startTranscription(lastFailedStartItem) : undefined}
              className="mx-2"
            />
          )}

          {sortedResults.length > 0 && (
            <div className="grid gap-4 max-w-2xl mx-auto">
              {sortedResults.map((item) => {
                const published = item.publishedAt ? new Date(item.publishedAt).toLocaleDateString() : "";
                const isStarting = startingVideoId === item.videoId;
                return (
                  <Card
                    key={item.videoId}
                    className="neu-recording-row perf-scroll-item overflow-hidden rounded-[20px] hover:scale-[1.01] transition-all group cursor-pointer"
                    onClick={() => startTranscription(item)}
                    role="button"
                    tabIndex={0}
                    aria-label={`Start transcription for ${item.title || "video"}`}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        startTranscription(item);
                      }
                    }}
                  >
                    <div className="flex gap-4 p-4">
                      <div className="relative w-32 h-20 bg-muted rounded-md shrink-0 overflow-hidden">
                        {item.thumbnailUrl ? (
                          <img
                            src={item.thumbnailUrl}
                            alt={item.title || "Thumbnail"}
                            className="w-full h-full object-cover opacity-90 group-hover:scale-105 transition-transform duration-500"
                          />
                        ) : (
                          <div className="w-full h-full bg-secondary flex items-center justify-center">
                            <YoutubeIcon className="w-8 h-8 text-muted-foreground/50" />
                          </div>
                        )}
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
                          <h3 className="font-medium text-foreground truncate pr-4 text-base">
                            {item.title || "Untitled"}
                          </h3>
                          <Badge variant="outline" className="text-[10px]">
                            {isStarting ? "Starting..." : "Transcribe"}
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
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Recent Videos</h2>
          <ToggleGroup
            type="single"
            value={viewMode}
            onValueChange={(val) => val && setViewMode(val as "list" | "grid")}
            className="bg-secondary/50 rounded-lg p-1"
          >
            <ToggleGroupItem value="list" aria-label="List view" className="h-8 w-8 p-0">
              <LayoutList className="h-4 w-4" />
            </ToggleGroupItem>
            <ToggleGroupItem value="grid" aria-label="Grid view" className="h-8 w-8 p-0">
              <LayoutGrid className="h-4 w-4" />
            </ToggleGroupItem>
          </ToggleGroup>
        </div>

        {/* History Search Bar */}
        <div className="relative mt-3">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            type="text"
            placeholder="Search history..."
            value={historySearch}
            onChange={(e) => setHistorySearch(e.target.value)}
            className="pl-9 pr-9 h-9 bg-secondary/50"
          />
          {historySearch && (
            <button
              type="button"
              onClick={() => setHistorySearch("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              aria-label="Clear YouTube history search"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

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
            <div className={viewMode === "grid" ? "grid grid-cols-3 gap-4" : "flex flex-col"}>
              {recentVideos.map((item: any, index: number) => (
                <YoutubeVideoCard
                  key={item.id}
                  item={item}
                  index={index}
                  viewMode={viewMode}
                  isDeleting={deletingId === item.id}
                  isCopying={copyingId === item.id}
                  onDelete={deleteTranscript}
                  onCopy={copyTranscript}
                  onNavigate={navigateToTranscript}
                  onHover={preloadTranscript}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


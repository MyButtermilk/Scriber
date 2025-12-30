import { ArrowRight, Clock, MoreHorizontal, PlayCircle, Youtube as YoutubeIcon, Loader2, Trash2, CheckCircle2, ThumbsUp, Eye, LayoutGrid, LayoutList } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useLocation } from "wouter";
import { useState, useEffect, useMemo } from "react";
import { apiUrl, wsUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";

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

export default function Youtube() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<YouTubeSearchItem[]>([]);
  const [searchError, setSearchError] = useState<string>("");
  const [isSearching, setIsSearching] = useState(false);
  const [startingVideoId, setStartingVideoId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<SortOption>("date");
  const queryClient = useQueryClient();
  const [viewMode, setViewMode] = useState<"list" | "grid">(
    () => (localStorage.getItem("scriber-view-mode") as "list" | "grid") || "list"
  );

  // Persist view mode
  useEffect(() => {
    localStorage.setItem("scriber-view-mode", viewMode);
  }, [viewMode]);

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
    queryKey: ["/api/transcripts"],
    staleTime: 0, // Always fetch fresh data on mount
  });
  const recentVideos: any[] = ((transcriptsQuery.data as any)?.items || []).filter(
    (t: any) => t?.type === "youtube",
  );

  // WebSocket connection for real-time updates
  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws"));

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg?.type === "history_updated") {
          queryClient.refetchQueries({ queryKey: ["/api/transcripts"] });
        }
      } catch {
        // ignore parse errors
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [queryClient]);

  // Helper to detect if input is a YouTube URL
  const isYouTubeUrl = (input: string): boolean => {
    return /(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/|youtube\.com\/shorts\/)/i.test(input);
  };

  const runSearch = async () => {
    const q = query.trim();
    if (!q || isSearching) return;

    setIsSearching(true);
    setSearchError("");

    try {
      // Check if input is a YouTube URL
      if (isYouTubeUrl(q)) {
        // Fetch video directly by URL
        const url = apiUrl(`/api/youtube/video?url=${encodeURIComponent(q)}`);
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.message || res.statusText);
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
          const text = await res.text();
          throw new Error(text || res.statusText);
        }
        const payload = await res.json();
        const items = (payload?.items || []) as YouTubeSearchItem[];
        setSearchResults(items);
        if (!items.length) setSearchError("No results found.");
      }
    } catch (e: any) {
      const msg = String(e?.message || e);
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
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
      const rec = await res.json();
      if (rec?.id) {
        setLocation(`/transcript/${rec.id}`);
      }
    } catch (e: any) {
      const msg = String(e?.message || e);
      toast({
        title: "Failed to start transcription",
        description: msg,
        duration: 4000,
      });
    } finally {
      setStartingVideoId(null);
    }
  };

  const deleteTranscript = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingId) return;

    setDeletingId(id);
    try {
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
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-6 space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Youtube Transcription</h1>
        <p className="text-muted-foreground">Paste a URL or search to transcribe video content</p>
      </header>

      {/* Input Section - Debossed neumorphic style */}
      <div className="neu-search-inset p-2 mb-6 rounded-xl">
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
            <p className="text-sm text-muted-foreground px-2">{searchError}</p>
          )}

          {sortedResults.length > 0 && (
            <div className="grid gap-4 max-w-2xl mx-auto">
              {sortedResults.map((item) => {
                const published = item.publishedAt ? new Date(item.publishedAt).toLocaleDateString() : "";
                const isStarting = startingVideoId === item.videoId;
                return (
                  <Card
                    key={item.videoId}
                    className="neu-panel-raised overflow-hidden border-0 hover:scale-[1.01] transition-all group cursor-pointer bg-card"
                    onClick={() => startTranscription(item)}
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

                      <div className="flex flex-col justify-between items-end">
                        <Button variant="ghost" size="icon" className="h-8 w-8 -mr-2">
                          <MoreHorizontal className="w-4 h-4" />
                        </Button>
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

        <div className="w-full py-2">
          {transcriptsQuery.isLoading ? (
            <SkeletonList count={3} variant={viewMode} />
          ) : recentVideos.length === 0 ? (
            <EmptyState type="youtube" />
          ) : (
            <div className={viewMode === "grid" ? "grid grid-cols-3 gap-4" : "grid grid-cols-1 gap-4"}>
              {recentVideos.map((item: any, index: number) => (
                <motion.div
                  key={item.id}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: index * 0.05, duration: 0.3, ease: "easeOut" }}
                >
                  <Card className="neu-panel-raised overflow-hidden bg-card border-0 hover:scale-[1.01] transition-all group cursor-pointer rounded-xl" onClick={() => setLocation(`/transcript/${item.id}`)}>
                    {viewMode === "list" ? (
                      // List view
                      <div className="flex gap-4 p-4 min-w-0 overflow-hidden">
                        <div className="relative w-32 h-20 bg-muted rounded-md shrink-0 overflow-hidden">
                          {item.thumbnailUrl ? (
                            <img
                              src={item.thumbnailUrl}
                              alt={item.title || "Thumbnail"}
                              className="w-full h-full object-cover opacity-90"
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
                            ) : (
                              <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full shrink-0">
                                <CheckCircle2 className="w-3 h-3" />
                                Ready
                              </div>
                            )}
                          </div>
                          <p className="text-sm text-muted-foreground mt-1 truncate">{item.channel || item.channelTitle || "Unknown Channel"} • {item.date}</p>
                        </div>

                        <div className="flex flex-col justify-center">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
                            onClick={(e) => deleteTranscript(e, item.id)}
                            disabled={deletingId === item.id}
                          >
                            {deletingId === item.id ? (
                              <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                              <Trash2 className="w-4 h-4" />
                            )}
                          </Button>
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
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-6 w-6 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
                              onClick={(e) => deleteTranscript(e, item.id)}
                              disabled={deletingId === item.id}
                            >
                              {deletingId === item.id ? (
                                <Loader2 className="w-3 h-3 animate-spin" />
                              ) : (
                                <Trash2 className="w-3 h-3" />
                              )}
                            </Button>
                          </div>
                        </div>
                      </div>
                    )}
                  </Card>
                </motion.div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


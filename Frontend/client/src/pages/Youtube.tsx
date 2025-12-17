import { ArrowRight, Clock, MoreHorizontal, PlayCircle, Youtube as YoutubeIcon, Loader2, Trash2, CheckCircle2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useLocation } from "wouter";
import { useState, useEffect } from "react";
import { apiUrl, wsUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";

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
};

export default function Youtube() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<YouTubeSearchItem[]>([]);
  const [searchError, setSearchError] = useState<string>("");
  const [isSearching, setIsSearching] = useState(false);
  const [startingVideoId, setStartingVideoId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const queryClient = useQueryClient();

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

  const runSearch = async () => {
    const q = query.trim();
    if (!q || isSearching) return;

    setIsSearching(true);
    setSearchError("");

    try {
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
    } catch (e: any) {
      const msg = String(e?.message || e);
      setSearchError(msg);
      toast({
        title: "YouTube search failed",
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

      {/* Input Section */}
      <Card className="p-2 mb-6 shadow-lg border-border/60 bg-card/80 backdrop-blur-sm">
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
      </Card>

      {/* Search Results */}
      {(isSearching || searchError || searchResults.length > 0) && (
        <div className="space-y-4 mb-8">
          <div className="flex items-center justify-between px-2">
            <h2 className="text-lg font-semibold">Search Results</h2>
            {isSearching && <span className="text-xs text-muted-foreground">Searching…</span>}
          </div>

          {!isSearching && searchError && (
            <p className="text-sm text-muted-foreground px-2">{searchError}</p>
          )}

          {searchResults.length > 0 && (
            <div className="grid gap-4">
              {searchResults.map((item) => {
                const published = item.publishedAt ? new Date(item.publishedAt).toLocaleDateString() : "";
                const isStarting = startingVideoId === item.videoId;
                return (
                  <Card
                    key={item.videoId}
                    className="overflow-hidden border-border/60 hover:border-primary/50 transition-colors group cursor-pointer"
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
      <div className="space-y-6">
        <h2 className="text-lg font-semibold px-2">Recent Videos</h2>

        {recentVideos.length === 0 ? (
          <p className="text-muted-foreground text-center py-8">No YouTube videos transcribed yet. Search for a video to get started!</p>
        ) : (
          <div className="grid gap-4">
            {recentVideos.map((item: any) => (
              <Card key={item.id} className="overflow-hidden border-border/60 hover:border-primary/50 transition-colors group cursor-pointer" onClick={() => setLocation(`/transcript/${item.id}`)}>
                <div className="flex gap-4 p-4">
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

                  <div className="flex-1 min-w-0">
                    <div className="flex justify-between items-start">
                      <h3 className="font-medium text-foreground truncate pr-4 text-base">{item.title}</h3>
                      {item.status === 'processing' ? (
                        <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1">
                          <Loader2 className="w-3 h-3 animate-spin" />
                          {item.step || "Processing"}
                        </Badge>
                      ) : item.status === 'failed' ? (
                        <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
                      ) : (
                        <div className="flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
                          <CheckCircle2 className="w-3 h-3" />
                          Ready
                        </div>
                      )}
                    </div>
                    <p className="text-sm text-muted-foreground mt-1">{item.channel || item.channelTitle || "Unknown Channel"} • {item.date}</p>
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
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

import { useEffect, useState, useCallback, memo, useMemo, useRef } from "react";
import { useSharedWebSocket } from "@/contexts/WebSocketContext";
import { Mic, Square, Globe, Trash2, Loader2, LayoutGrid, LayoutList, Search, X, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Input } from "@/components/ui/input";
import type { Transcript } from "@/lib/mockData";
import { useLocation } from "wouter";

// Memoized TranscriptCard to prevent unnecessary re-renders
interface TranscriptCardProps {
  item: Transcript;
  index: number;
  viewMode: "list" | "grid";
  isDeleting: boolean;
  isCopying: boolean;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onCopy: (e: React.MouseEvent, id: string) => void;
  onNavigate: (id: string) => void;
  onHover?: (id: string) => void;
}

const TranscriptCard = memo(function TranscriptCard({
  item,
  index,
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onNavigate,
  onHover,
}: TranscriptCardProps) {
  return (
    <div>
      <Card
        className={`neu-recording-row perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid" : ""} p-4 cursor-pointer bg-transparent hover:scale-[1.01] group`}
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
          // List View
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className="w-10 h-10 rounded-full bg-gradient-to-br from-primary/20 to-primary/5 flex items-center justify-center text-primary">
                <Mic className="w-5 h-5" />
              </div>
              <div>
                <h3 className="font-medium text-foreground group-hover:text-primary transition-colors">{item.title}</h3>
                <p className="text-sm text-muted-foreground mt-1 truncate">
                  {item.date} • {formatDurationLikeYoutube(item.duration)} • {item.language || "—"}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="opacity-100 md:opacity-60 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity text-muted-foreground hover:text-primary"
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                title="Copy transcript"
                aria-label={`Copy transcript ${item.title}`}
                type="button"
              >
                {isCopying ? (
                  <Check className="w-4 h-4 text-green-500" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="opacity-100 md:opacity-60 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                title="Delete transcript"
                aria-label={`Delete transcript ${item.title}`}
                type="button"
              >
                {isDeleting ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Trash2 className="w-4 h-4" />
                )}
              </Button>
            </div>
          </div>
        ) : (
          // Grid View
          <div className="flex flex-col h-full">
            <div className="flex items-start justify-between mb-3">
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-primary/20 to-primary/5 flex items-center justify-center text-primary">
                <Mic className="w-6 h-6" />
              </div>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon"
                  className="opacity-100 md:opacity-60 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity text-muted-foreground hover:text-primary"
                  onClick={(e) => onCopy(e, item.id)}
                  disabled={isCopying}
                  title="Copy transcript"
                  aria-label={`Copy transcript ${item.title}`}
                  type="button"
                >
                  {isCopying ? (
                    <Check className="w-4 h-4 text-green-500" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="opacity-100 md:opacity-60 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                  onClick={(e) => onDelete(e, item.id)}
                  disabled={isDeleting}
                  title="Delete transcript"
                  aria-label={`Delete transcript ${item.title}`}
                  type="button"
                >
                  {isDeleting ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                </Button>
              </div>
            </div>
            <h3 className="font-medium text-foreground group-hover:text-primary transition-colors line-clamp-2 mb-2">{item.title}</h3>
            <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground mt-auto">
              <span>{item.date}</span>
              <span>•</span>
              <span>{formatDurationLikeYoutube(item.duration)}</span>
            </div>
            <div className="mt-2">
              <span className="inline-flex items-center gap-1 bg-secondary/50 px-2 py-1 rounded-md text-xs"><Globe className="w-3.5 h-3.5" /> {item.language || "—"}</span>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
});

interface AudioVisualizerProps {
  isRecording: boolean;
  audioLevelRef: React.MutableRefObject<number>;
}

interface InputWarningAction {
  id: string;
  label: string;
  uri: string;
}

const INPUT_WARNING_ACTIONS_BY_CODE: Record<string, InputWarningAction[]> = {
  mic_level_very_low: [
    {
      id: "open_input_volume",
      label: "Open Input Volume",
      uri: "ms-settings:sound-defaultinputproperties",
    },
    {
      id: "open_microphone_privacy",
      label: "Check Microphone Privacy",
      uri: "ms-settings:privacy-microphone",
    },
    {
      id: "open_sound_settings",
      label: "Open Sound Settings",
      uri: "ms-settings:sound",
    },
  ],
};

function normalizeInputWarningActions(value: unknown): InputWarningAction[] {
  if (!Array.isArray(value)) return [];
  const normalized: InputWarningAction[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== "object") continue;
    const action = raw as Record<string, unknown>;
    const id = typeof action.id === "string" ? action.id.trim() : "";
    const label = typeof action.label === "string" ? action.label.trim() : "";
    const uri = typeof action.uri === "string" ? action.uri.trim() : "";
    if (!id || !label || !uri) continue;
    normalized.push({ id, label, uri });
  }
  return normalized;
}

const AudioVisualizer = memo(function AudioVisualizer({
  isRecording,
  audioLevelRef,
}: AudioVisualizerProps) {
  const [level, setLevel] = useState(0);
  const lastFrameRef = useRef(0);

  useEffect(() => {
    if (!isRecording) {
      setLevel(0);
      return;
    }
    let rafId = 0;
    const tick = (time: number) => {
      if (time - lastFrameRef.current >= 33) {
        setLevel(audioLevelRef.current);
        lastFrameRef.current = time;
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [audioLevelRef, isRecording]);

  const intensity = Math.min(1, Math.max(0, level * 3));

  return (
    <div className="h-16 flex items-center justify-center gap-1 w-full max-w-xs overflow-hidden">
      {isRecording ? (
        Array.from({ length: 20 }).map((_, i) => (
          <div
            key={i}
            className="w-1.5 bg-primary/80 rounded-full transition-[height] duration-150 ease-out"
            style={{
              height: `${10 + intensity * 48 * (0.35 + 0.65 * Math.abs(Math.sin((i + 1) * 0.9))) }px`,
            }}
          />
        ))
      ) : (
        <div className="w-full h-0.5 bg-border rounded-full" />
      )}
    </div>
  );
});
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { formatDurationLikeYoutube } from "@/lib/duration";

export default function LiveMic() {
  const { toast } = useToast();
  const [isRecording, setIsRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [status, setStatus] = useState<string>("Stopped");
  const [inputWarning, setInputWarning] = useState("");
  const [inputWarningActions, setInputWarningActions] = useState<InputWarningAction[]>([]);
  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const [viewMode, setViewMode] = useUrlQueryState<"list" | "grid">("view", "grid", {
    parse: (raw) => (raw === "list" ? "list" : "grid"),
  });
  const [searchQuery, setSearchQuery] = useUrlQueryState("q", "", {
    parse: (raw) => raw ?? "",
    serialize: (value) => {
      const trimmed = value.trim();
      return trimmed ? trimmed : null;
    },
    syncDelayMs: 250,
  });
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const audioLevelRef = useRef(0);
  const transcriptsQueryKey = useMemo(
    () => ["/api/transcripts", { q: debouncedSearch, type: "mic" }] as const,
    [debouncedSearch],
  );
  const { refreshNow: refreshMicHistory } = useTranscriptAutoRefresh({ queryKey: transcriptsQueryKey });

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const transcriptsQuery = useQuery({
    queryKey: transcriptsQueryKey,
    queryFn: async () => {
      const params = new URLSearchParams();
      if (debouncedSearch) params.set("q", debouncedSearch);
      params.set("type", "mic");
      const res = await fetch(apiUrl(`/api/transcripts?${params}`), { credentials: "include" });
      return res.json();
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    placeholderData: (previous) => previous,
  });
  const transcripts: Transcript[] = (transcriptsQuery.data as any)?.items || [];
  const activeSessionIdRef = useRef<string | null>(null);

  // Mock timer
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (isRecording) {
      interval = setInterval(() => {
        setElapsed(e => e + 1);
      }, 1000);
    } else {
      setElapsed(0);
    }
    return () => clearInterval(interval);
  }, [isRecording]);

  const applyInputWarning = useCallback((message: unknown, code: unknown, actions: unknown) => {
    const normalizedMessage = String(message || "").trim();
    setInputWarning(normalizedMessage);
    if (!normalizedMessage) {
      setInputWarningActions([]);
      return;
    }

    const normalizedActions = normalizeInputWarningActions(actions);
    if (normalizedActions.length > 0) {
      setInputWarningActions(normalizedActions);
      return;
    }

    const normalizedCode = typeof code === "string" ? code.trim() : "";
    const fallback = INPUT_WARNING_ACTIONS_BY_CODE[normalizedCode] || [];
    setInputWarningActions(fallback.map((action) => ({ ...action })));
  }, []);

  const handleInputWarningAction = useCallback((action: InputWarningAction) => {
    try {
      const normalizedUri = String(action.uri || "").trim().toLowerCase();
      if (!normalizedUri.startsWith("ms-settings:")) {
        throw new Error("Unsupported settings URI scheme");
      }
      window.location.href = action.uri;
    } catch (e: any) {
      toast({
        title: "Could not open settings",
        description: String(e?.message || e || "Please open Windows Sound settings manually."),
        duration: 4000,
      });
    }
  }, [toast]);

  // WebSocket with auto-reconnection
  const handleWsMessage = useCallback((msg: any) => {
    if (!msg || typeof msg !== "object") return;
    const msgSessionId = typeof msg.sessionId === "string" ? msg.sessionId : null;
    const activeSessionId = activeSessionIdRef.current;

    switch (msg.type) {
      case "state":
        if (msgSessionId) {
          activeSessionIdRef.current = msgSessionId;
        } else if (!msg.listening) {
          activeSessionIdRef.current = null;
        }
        setIsRecording(!!msg.listening);
        setStatus(msg.status || "Stopped");
        applyInputWarning(msg.inputWarning, msg.inputWarningCode, msg.inputWarningActions);
        if (msg.current?.content) {
          setFinalText(String(msg.current.content));
          setInterimText("");
        }
        break;
      case "status":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        setIsRecording(!!msg.listening);
        setStatus(msg.status || "Stopped");
        applyInputWarning(msg.inputWarning, msg.inputWarningCode, msg.inputWarningActions);
        break;
      case "audio_level":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        audioLevelRef.current = Number(msg.rms) || 0;
        break;
      case "input_warning":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        if (msg.active) {
          applyInputWarning(msg.message || "Microphone input level is very low.", msg.code, msg.actions);
        } else {
          applyInputWarning("", "", []);
        }
        break;
      case "transcript":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        if (msg.isFinal) {
          if (msg.content) {
            setFinalText(String(msg.content));
            setInterimText("");
            break;
          }
          const t = String(msg.text || "").trim();
          if (t) setFinalText((prev) => (prev ? `${prev} ${t}` : t));
          setInterimText("");
        } else {
          setInterimText(String(msg.text || ""));
        }
        break;
      case "session_started":
        if (msgSessionId) {
          activeSessionIdRef.current = msgSessionId;
        }
        setIsRecording(true);
        setStatus("Listening");
        applyInputWarning("", "", []);
        setFinalText("");
        setInterimText("");
        break;
      case "session_finished":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        activeSessionIdRef.current = null;
        setIsRecording(false);
        setStatus("Stopped");
        applyInputWarning("", "", []);
        if (msg.session?.content) {
          setFinalText(String(msg.session.content));
          setInterimText("");
        }
        refreshMicHistory();
        break;
      case "error":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        toast({
          title: "Recording Error",
          description: msg.message || "An error occurred during recording.",
          variant: "destructive",
          duration: 6000,
        });
        setIsRecording(false);
        setStatus("Stopped");
        applyInputWarning("", "", []);
        break;
      case "settings_updated":
        break;
      default:
        break;
    }
  }, [applyInputWarning, refreshMicHistory, toast]);

  // PERFORMANCE: Uses singleton WebSocket connection (shared across all pages)
  useSharedWebSocket(handleWsMessage);

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const handleToggle = async () => {
    try {
      const endpoint = isRecording ? "/api/live-mic/stop" : "/api/live-mic/start";
      const res = await fetch(apiUrl(endpoint), { method: "POST", credentials: "include" });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || res.statusText);
      }
    } catch (e: any) {
      toast({
        title: "Action failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    }
  };

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (deletingId) return;

    setDeletingId(id);
    try {
      const res = await fetch(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(res.statusText);
      }
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
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
  }, [deletingId, queryClient, toast]);

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
    // Preload the lazy-loaded TranscriptDetail page chunk
    import("@/pages/TranscriptDetail");
    // Prefetch the transcript data
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  return (
    <div className="max-w-screen-md xl:max-w-6xl mx-auto px-4 py-6 md:py-8">
      <header className="mb-8 text-center space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Live Transcription</h1>
        <p className="text-muted-foreground">Capture high-fidelity voice notes instantly</p>
      </header>

      <div className="space-y-10">
        <section className="flex flex-col items-center justify-center space-y-6">

          {/* Live Text Output - Debossed status well for unified design */}
          <div className="neu-status-well w-full max-w-lg min-h-[120px] text-center flex items-center justify-center p-6">
            <p className="sr-only" aria-live="assertive" aria-atomic="true">
              {isRecording ? `Recording started. Elapsed ${formatTime(elapsed)}.` : "Recording stopped."}
            </p>
            <div aria-live="polite" aria-atomic="true">
              {isRecording ? (
                <p className="text-lg md:text-xl font-medium leading-relaxed relative z-10">
                  {(finalText || interimText) ? (
                    <>
                      "<span className="text-foreground/90">{finalText}</span>
                      {interimText && (
                        <span className="text-muted-foreground italic">{finalText ? ' ' : ''}{interimText}</span>
                      )}"
                    </>
                  ) : (
                    <span className="text-foreground/90">"{status || "Listening"}..."</span>
                  )}
                </p>
              ) : (
                <p className="text-muted-foreground relative z-10">Ready to record. Tap the microphone to start.</p>
              )}
            </div>
          </div>
          {isRecording && inputWarning && (
            <div className="w-full max-w-lg rounded-lg border border-amber-400/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200 space-y-3">
              <p>{inputWarning}</p>
              {inputWarningActions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {inputWarningActions.map((action) => (
                    <Button
                      key={`${action.id}-${action.uri}`}
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="h-8 bg-amber-200/15 text-amber-100 hover:bg-amber-200/25"
                      onClick={() => handleInputWarningAction(action)}
                    >
                      {action.label}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Waveform Visualization (Mock) */}
          <AudioVisualizer isRecording={isRecording} audioLevelRef={audioLevelRef} />

          {/* Controls */}
          <div className="flex items-center gap-6">
            <div className="text-sm font-medium text-muted-foreground w-16 text-right">
              {isRecording && <span className="animate-pulse text-red-500 motion-reduce:animate-none">REC</span>}
            </div>

            <button
              type="button"
              className={`neu-mic-button flex items-center justify-center text-white ${isRecording ? 'recording recording-pulse' : ''}`}
              onClick={handleToggle}
              aria-label={isRecording ? "Stop recording" : "Start recording"}
            >
              {isRecording ? (
                <Square className="w-10 h-10 fill-current" />
              ) : (
                <Mic className="w-12 h-12" />
              )}
            </button>

            <div className="text-sm font-mono font-medium text-muted-foreground w-16">
              {isRecording ? formatTime(elapsed) : "00:00"}
            </div>
          </div>
        </section>

        {/* History Section */}
        <section className="space-y-4">
          <div className="flex items-center justify-between px-2">
            <h2 className="text-lg font-semibold text-foreground">Recent Recordings</h2>
            <div className="flex items-center gap-2">
              <ToggleGroup
                type="single"
                value={viewMode}
                onValueChange={(val) => val && setViewMode(val as "list" | "grid")}
                className="bg-secondary/50 rounded-lg p-1"
              >
                <ToggleGroupItem value="list" aria-label="List view" className="h-11 w-11 p-0">
                  <LayoutList className="h-4 w-4" />
                </ToggleGroupItem>
                <ToggleGroupItem value="grid" aria-label="Grid view" className="h-11 w-11 p-0">
                  <LayoutGrid className="h-4 w-4" />
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
          </div>

          {/* Search Bar */}
          <div className="relative mt-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type="text"
              placeholder="Search recordings..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9 pr-9 h-11 bg-secondary/50"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label="Clear recording search"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>

          {transcriptsQuery.isLoading ? (
            <SkeletonList count={3} variant={viewMode} />
          ) : transcriptsQuery.isError ? (
            <QueryErrorState
              title="Could not load recordings"
              description="Please retry loading your recording history."
              onRetry={() => transcriptsQuery.refetch()}
            />
          ) : (
            transcripts.length === 0 ? (
              debouncedSearch ? (
                <p className="text-center text-muted-foreground py-8">No recordings match "{debouncedSearch}"</p>
              ) : (
                <EmptyState type="mic" />
              )
            ) : (
              <div className={viewMode === "grid" ? "grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" : "flex flex-col gap-4"}>
                {transcripts.map((item, index) => (
                  <TranscriptCard
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
            )
          )}
        </section>
      </div>
    </div>
  );
}


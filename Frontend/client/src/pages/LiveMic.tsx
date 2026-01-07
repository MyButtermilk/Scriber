import { useEffect, useState, useCallback, memo } from "react";
import { useWebSocket } from "@/hooks/use-websocket";
import { Mic, Square, Clock, Globe, Timer, Trash2, Loader2, LayoutGrid, LayoutList } from "lucide-react";
import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { Transcript } from "@/lib/mockData";
import { useLocation } from "wouter";

// Memoized TranscriptCard to prevent unnecessary re-renders
interface TranscriptCardProps {
  item: Transcript;
  index: number;
  viewMode: "list" | "grid";
  deletingId: string | null;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onNavigate: (id: string) => void;
  onHover?: (id: string) => void;
}

const TranscriptCard = memo(function TranscriptCard({
  item,
  index,
  viewMode,
  deletingId,
  onDelete,
  onNavigate,
  onHover,
}: TranscriptCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: Math.min(index * 0.02, 0.1),
        duration: 0.2,
        ease: "easeOut"
      }}
    >
      <Card
        className="neu-recording-row p-4 cursor-pointer bg-transparent hover:scale-[1.01] group"
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
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
                <div className="flex items-center gap-3 text-xs text-muted-foreground mt-1">
                  <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {item.date}</span>
                  <span className="flex items-center gap-1"><Timer className="w-3 h-3" /> {item.duration}</span>
                  <span className="flex items-center gap-1 bg-secondary px-1.5 py-0.5 rounded-md"><Globe className="w-3 h-3" /> {item.language}</span>
                </div>
              </div>
            </div>
            <div className="flex flex-col justify-center">
              <Button
                variant="ghost"
                size="icon"
                className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                onClick={(e) => onDelete(e, item.id)}
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
          // Grid View
          <div className="flex flex-col h-full">
            <div className="flex items-start justify-between mb-3">
              <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-primary/20 to-primary/5 flex items-center justify-center text-primary">
                <Mic className="w-6 h-6" />
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive h-8 w-8"
                onClick={(e) => onDelete(e, item.id)}
                disabled={deletingId === item.id}
              >
                {deletingId === item.id ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Trash2 className="w-4 h-4" />
                )}
              </Button>
            </div>
            <h3 className="font-medium text-foreground group-hover:text-primary transition-colors line-clamp-2 mb-2">{item.title}</h3>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mt-auto">
              <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {item.date}</span>
              <span className="flex items-center gap-1"><Timer className="w-3 h-3" /> {item.duration}</span>
            </div>
            <div className="mt-2">
              <span className="inline-flex items-center gap-1 bg-secondary/50 px-2 py-1 rounded-md text-xs"><Globe className="w-3 h-3" /> {item.language}</span>
            </div>
          </div>
        )}
      </Card>
    </motion.div>
  );
});
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl, wsUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";

export default function LiveMic() {
  const { toast } = useToast();
  const [isRecording, setIsRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [status, setStatus] = useState<string>("Stopped");
  const [audioLevel, setAudioLevel] = useState(0);
  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const [viewMode, setViewMode] = useState<"list" | "grid">(
    () => (localStorage.getItem("scriber-view-mode") as "list" | "grid") || "list"
  );

  // Persist view mode
  useEffect(() => {
    localStorage.setItem("scriber-view-mode", viewMode);
  }, [viewMode]);

  const transcriptsQuery = useQuery({
    queryKey: ["/api/transcripts"],
  });
  const transcripts: Transcript[] = (transcriptsQuery.data as any)?.items || [];

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

  // WebSocket with auto-reconnection
  const handleWsMessage = useCallback((msg: any) => {
    if (!msg || typeof msg !== "object") return;

    switch (msg.type) {
      case "state":
        setIsRecording(!!msg.listening);
        setStatus(msg.status || "Stopped");
        if (msg.current?.content) {
          setFinalText(String(msg.current.content));
          setInterimText("");
        }
        break;
      case "status":
        setIsRecording(!!msg.listening);
        setStatus(msg.status || "Stopped");
        break;
      case "audio_level":
        setAudioLevel(Number(msg.rms) || 0);
        break;
      case "transcript":
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
        setIsRecording(true);
        setStatus("Listening");
        setFinalText("");
        setInterimText("");
        break;
      case "session_finished":
        setIsRecording(false);
        setStatus("Stopped");
        if (msg.session?.content) {
          setFinalText(String(msg.session.content));
          setInterimText("");
        }
        queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
        break;
      case "history_updated":
        queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
        break;
      case "error":
        toast({
          title: "Recording Error",
          description: msg.message || "An error occurred during recording.",
          variant: "destructive",
          duration: 6000,
        });
        setIsRecording(false);
        setStatus("Stopped");
        break;
      case "settings_updated":
        break;
      default:
        break;
    }
  }, [queryClient, toast]);

  const { isConnected } = useWebSocket({
    path: "/ws",
    onMessage: handleWsMessage,
    onError: () => {
      toast({
        title: "Backend disconnected",
        description: "Could not connect to the Scriber backend. Reconnecting...",
        duration: 4000,
      });
    },
    autoReconnect: true,
    reconnectDelay: 1000,
  });


  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const intensity = Math.min(1, Math.max(0, audioLevel * 3));

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
    <div className="max-w-screen-md mx-auto px-4 py-6 md:py-8">
      <header className="mb-8 text-center space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground">Live Transcription</h1>
        <p className="text-muted-foreground">Capture high-fidelity voice notes instantly</p>
      </header>

      {/* Main Recording Area */}
      <div className="flex flex-col items-center justify-center space-y-6 mb-10">

        {/* Live Text Output - Debossed status well for unified design */}
        <div className="neu-status-well w-full max-w-lg min-h-[120px] text-center flex items-center justify-center p-6">
          {isRecording ? (
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-lg md:text-xl font-medium leading-relaxed relative z-10"
            >
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
            </motion.p>
          ) : (
            <p className="text-muted-foreground relative z-10">Ready to record. Tap the microphone to start.</p>
          )}
        </div>

        {/* Waveform Visualization (Mock) */}
        <div className="h-16 flex items-center justify-center gap-1 w-full max-w-xs overflow-hidden">
          {isRecording ? (
            Array.from({ length: 20 }).map((_, i) => (
              <motion.div
                key={i}
                className="w-1.5 bg-primary/80 rounded-full"
                animate={{
                  height: [
                    10,
                    10 + intensity * 48 * (0.35 + 0.65 * Math.abs(Math.sin((i + 1) * 0.9))),
                    10,
                  ],
                }}
                transition={{
                  repeat: Infinity,
                  duration: 0.5,
                  delay: i * 0.05,
                }}
              />
            ))
          ) : (
            <div className="w-full h-0.5 bg-border rounded-full" />
          )}
        </div>

        {/* Controls */}
        <div className="flex items-center gap-6">
          <div className="text-sm font-medium text-muted-foreground w-16 text-right">
            {isRecording && <span className="animate-pulse text-red-500">REC</span>}
          </div>

          <button
            className={`neu-mic-button flex items-center justify-center text-white ${isRecording ? 'recording recording-pulse' : ''}`}
            onClick={handleToggle}
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
      </div>

      {/* History Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between px-2">
          <h2 className="text-lg font-semibold text-foreground">Recent Recordings</h2>
          <div className="flex items-center gap-2">
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
        </div>

        {transcriptsQuery.isLoading ? (
          <SkeletonList count={3} variant={viewMode} />
        ) : transcripts.filter(t => t.type === 'mic').length === 0 ? (
          <EmptyState type="mic" />
        ) : (
          <div className={viewMode === "grid" ? "grid grid-cols-2 gap-4" : "flex flex-col gap-4"}>
            {transcripts.filter(t => t.type === 'mic').map((item, index) => (
              <TranscriptCard
                key={item.id}
                item={item}
                index={index}
                viewMode={viewMode}
                deletingId={deletingId}
                onDelete={deleteTranscript}
                onNavigate={navigateToTranscript}
                onHover={preloadTranscript}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}


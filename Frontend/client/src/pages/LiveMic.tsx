import { useEffect, useState, useCallback, memo, useMemo, useRef, type CSSProperties } from "react";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { Mic, Globe, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { PageIntro } from "@/components/page-intro";
import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import { useLocation } from "wouter";
import type { BackendStateResponse } from "@/lib/api-types";

const VIEW_MODE_STORAGE_KEY = "scriber:view-mode";
const MIC_VISUAL_NOISE_FLOOR = 0.00003;
const MIC_VISUAL_DISPLAY_SCALE = 90;

type Transcript = {
  id: string;
  title: string;
  date: string;
  duration: string;
  status: "completed" | "processing" | "failed" | "recording" | "stopped";
  type: "mic" | "youtube" | "file";
  content?: string;
  language?: string;
  channel?: string;
  fileSize?: string;
  step?: string;
  preview?: string;
  createdAt?: string;
};

type LiveRecordingState = "idle" | "initializing" | "recording" | "finalizing" | "completed" | "failed";
type WebSocketStateMessage = Extract<ScriberWebSocketMessage, { type: "state" }>;
type BackendLiveStateSnapshot = BackendStateResponse | WebSocketStateMessage;

function coerceRecordingState(value: unknown, fallback: LiveRecordingState = "idle"): LiveRecordingState {
  switch (value) {
    case "idle":
    case "initializing":
    case "recording":
    case "finalizing":
    case "completed":
    case "failed":
      return value;
    default:
      return fallback;
  }
}

function micVisualGainFromAudioLevel(rawInput: number): number {
  const rms = Math.min(1, Math.max(0, rawInput > 1 ? rawInput / 100 : rawInput));
  if (rms <= MIC_VISUAL_NOISE_FLOOR) {
    return 0;
  }
  return Math.min(1, Math.pow((rms - MIC_VISUAL_NOISE_FLOOR) * MIC_VISUAL_DISPLAY_SCALE, 0.72));
}

// Memoized TranscriptCard to prevent unnecessary re-renders
interface TranscriptCardProps {
  item: Transcript;
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
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onNavigate,
  onHover,
}: TranscriptCardProps) {
  const deletingClasses = isDeleting
    ? "pointer-events-none opacity-[0.55] scale-[0.985]"
    : "opacity-100 scale-100";
  const timeLabel = recordingTimeLabel(item.createdAt, item.date);
  const snippet = (item.preview || "").trim();
  const visibleSnippet = snippet && snippet !== item.title
    ? snippet
    : item.status === "processing" || item.status === "recording"
      ? item.step || "Transcription in progress"
      : item.title.trim() || "No transcript preview available";

  return (
    <div className="h-full w-full">
      <Card
        className={`live-recording-card perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid" : ""} group h-full cursor-pointer rounded-[20px] p-4 transform-gpu ${deletingClasses}`}
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
      >
          {viewMode === "list" ? (
            // List View
            <div className="flex min-h-[72px] flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div className="flex min-w-0 flex-1 items-center gap-4">
                <div className="live-recording-icon flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] text-primary">
                  <Mic className="h-[18px] w-[18px] stroke-[1.65px]" />
                </div>
                <div className="min-w-0 flex-1">
                  <h3>
                    <button
                      type="button"
                      className="line-clamp-2 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.4] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                      onClick={(event) => {
                        event.stopPropagation();
                        onNavigate(item.id);
                      }}
                    >
                      {visibleSnippet}
                    </button>
                  </h3>
                  <p className="mt-1.5 truncate text-[11.5px] text-muted-foreground">
                    {timeLabel} • {formatDurationLikeYoutube(item.duration)} • {item.language || "Unknown language"}
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-end gap-1">
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
            // Grid View
            <div className="flex h-full flex-col">
              <div className="mb-4 flex items-start justify-between">
                <div className="live-recording-icon flex h-11 w-11 items-center justify-center rounded-[13px] text-primary">
                  <Mic className="h-5 w-5 stroke-[1.65px]" />
                </div>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] font-semibold tabular-nums text-muted-foreground">{timeLabel}</span>
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
              </div>
              <h3 className="mb-5 min-h-[58px]">
                <button
                  type="button"
                  className="line-clamp-3 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.45] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                  onClick={(event) => {
                    event.stopPropagation();
                    onNavigate(item.id);
                  }}
                >
                  {visibleSnippet}
                </button>
              </h3>
              <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-foreground/[0.06] pt-3 text-[11px] text-muted-foreground">
                <span>{formatDurationLikeYoutube(item.duration)}</span>
                <span className="inline-flex items-center gap-1.5 font-medium"><Globe className="h-3 w-3 stroke-[1.65px]" /> {item.language || "Unknown"}</span>
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
  barCount: number;
}

interface GlossyMicButtonProps {
  isActive: boolean;
  disabled?: boolean;
  busy?: boolean;
  label: string;
  audioLevelRef: React.MutableRefObject<number>;
  onToggle: () => void;
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
  barCount,
}: AudioVisualizerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) {
      return;
    }

    let rafId = 0;
    const resolvedBarCount = normalizeVisualizerBarCount(barCount);
    let smoothedLevel = 0;
    let lastWidth = 0;
    let lastHeight = 0;
    let lastDrawAt = 0;

    const style = getComputedStyle(document.documentElement);
    const primary = `hsl(${style.getPropertyValue("--primary").trim() || "220 60% 50%"})`;
    const border = `hsl(${style.getPropertyValue("--border").trim() || "220 15% 85%"})`;

    const resizeCanvas = () => {
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(1, Math.floor(rect.width));
      const height = Math.max(1, Math.floor(rect.height));
      if (width === lastWidth && height === lastHeight) {
        return;
      }
      lastWidth = width;
      lastHeight = height;
      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const drawIdle = () => {
      resizeCanvas();
      ctx.clearRect(0, 0, lastWidth, lastHeight);
      ctx.fillStyle = border;
      const y = Math.round(lastHeight / 2);
      ctx.fillRect(0, y, lastWidth, 2);
    };

    if (!isRecording) {
      drawIdle();
      return;
    }

    const tick = (time: number) => {
      if (time - lastDrawAt < 33) {
        rafId = requestAnimationFrame(tick);
        return;
      }
      lastDrawAt = time;
      resizeCanvas();
      const rawLevel = micVisualGainFromAudioLevel(audioLevelRef.current);
      smoothedLevel = smoothedLevel * 0.72 + rawLevel * 0.28;

      ctx.clearRect(0, 0, lastWidth, lastHeight);
      const gap = Math.max(1, Math.min(4, lastWidth / Math.max(1, resolvedBarCount * 6)));
      const barWidth = Math.max(1, (lastWidth - gap * (resolvedBarCount - 1)) / resolvedBarCount);
      const totalWidth = resolvedBarCount * barWidth + (resolvedBarCount - 1) * gap;
      const startX = Math.max(0, (lastWidth - totalWidth) / 2);
      const maxBarHeight = Math.max(8, lastHeight - 8);
      ctx.fillStyle = primary;

      for (let i = 0; i < resolvedBarCount; i += 1) {
        const centerDistance = Math.abs(i - (resolvedBarCount - 1) / 2) / ((resolvedBarCount - 1) / 2);
        const shape = 1 - centerDistance * centerDistance * 0.65;
        const phase = time * 0.008 + i * 0.7;
        const motion = 0.78 + Math.sin(phase) * 0.22;
        const height = Math.max(4, smoothedLevel * maxBarHeight * shape * motion);
        const x = startX + i * (barWidth + gap);
        const y = (lastHeight - height) / 2;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, height, barWidth / 2);
        ctx.fill();
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [audioLevelRef, barCount, isRecording]);

  return (
    <canvas
      ref={canvasRef}
      className="h-12 w-full md:h-16"
      aria-label={isRecording ? "Recording audio level" : "Recording idle"}
      role="img"
    />
  );
});

const GlossyMicButton = memo(function GlossyMicButton({
  isActive,
  disabled = false,
  busy = false,
  label,
  audioLevelRef,
  onToggle,
}: GlossyMicButtonProps) {
  const [ripples, setRipples] = useState<Array<{ id: number; scale: number; alpha: number }>>([]);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const rippleCounterRef = useRef(0);
  const smoothedGainRef = useRef(0);
  const lastRippleTimeRef = useRef(0);
  const rippleTimeoutsRef = useRef<number[]>([]);

  const clearRippleTimeouts = useCallback(() => {
    for (const id of rippleTimeoutsRef.current) {
      window.clearTimeout(id);
    }
    rippleTimeoutsRef.current = [];
  }, []);

  const spawnRipple = useCallback((intensity: number) => {
    const id = ++rippleCounterRef.current;
    const ripple = {
      id,
      scale: 1.3 + (intensity * 0.8),
      alpha: 0.4 + (intensity * 0.5),
    };
    setRipples((prev) => [...prev.slice(-4), ripple]);
    const timeoutId = window.setTimeout(() => {
      setRipples((prev) => prev.filter((item) => item.id !== id));
    }, 1200);
    rippleTimeoutsRef.current.push(timeoutId);
  }, []);

  useEffect(() => {
    if (!isActive) {
      smoothedGainRef.current = 0;
      wrapperRef.current?.style.setProperty("--audio-gain", "0");
      setRipples([]);
      clearRippleTimeouts();
      return;
    }

    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      smoothedGainRef.current = 0.35;
      wrapperRef.current?.style.setProperty("--audio-gain", "0.350");
      setRipples([]);
      clearRippleTimeouts();
      return;
    }

    let rafId = 0;
    let lastVisualFrame = 0;
    const update = (now: number) => {
      if (now - lastVisualFrame < 33) {
        rafId = requestAnimationFrame(update);
        return;
      }
      lastVisualFrame = now;

      const rawInput = Number.isFinite(audioLevelRef.current) ? audioLevelRef.current : 0;
      const currentGain = micVisualGainFromAudioLevel(rawInput);
      const nextGain = (smoothedGainRef.current * 0.75) + (currentGain * 0.25);
      smoothedGainRef.current = nextGain;
      wrapperRef.current?.style.setProperty("--audio-gain", nextGain.toFixed(3));

      const minRippleDelay = 350 - (nextGain * 200);
      if (nextGain > 0.25 && now - lastRippleTimeRef.current > minRippleDelay) {
        spawnRipple(nextGain);
        lastRippleTimeRef.current = now;
      }

      rafId = requestAnimationFrame(update);
    };

    rafId = requestAnimationFrame(update);
    return () => {
      cancelAnimationFrame(rafId);
      clearRippleTimeouts();
    };
  }, [audioLevelRef, clearRippleTimeouts, isActive, spawnRipple]);

  return (
    <div ref={wrapperRef} className={`glossy-mic-wrapper ${isActive ? "is-recording" : ""}`}>
      <div className="glossy-mic-outer-ring">
        <div className="glossy-mic-trench">
          <div className="glossy-mic-pulse-glow" />
          <div className="glossy-mic-ripple-container" aria-hidden="true">
            {ripples.map((ripple) => (
              <span
                key={ripple.id}
                className="glossy-mic-ripple"
                style={{
                  "--scale-target": ripple.scale.toFixed(3),
                  "--ripple-alpha": ripple.alpha.toFixed(3),
                } as CSSProperties}
              />
            ))}
          </div>
          <button
            type="button"
            className="glossy-mic-central-button"
            onClick={onToggle}
            disabled={disabled}
            aria-busy={busy}
            aria-label={label}
          >
            <span className="glossy-mic-layer glossy-mic-idle-layer" />
            <span className="glossy-mic-layer glossy-mic-recording-layer" />
            <span className="glossy-mic-layer glossy-mic-flare-layer" />
            <span className="glossy-mic-gloss-highlight" />
            <Mic className="glossy-mic-icon" />
          </button>
        </div>
      </div>
    </div>
  );
});
import { useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useToast } from "@/hooks/use-toast";
import { recordingErrorToastMessageFromPayload, showRecordingErrorToast } from "@/lib/recording-error-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { formatDurationLikeYoutube } from "@/lib/duration";
import {
  DEFAULT_VISUALIZER_BAR_COUNT,
  loadVisualizerBarCount,
  normalizeVisualizerBarCount,
} from "@/lib/visualizer-settings";
import { VirtualTranscriptHistory } from "@/components/virtual-transcript-history";
import { transcriptHistoryQueryKey, useTranscriptHistoryQuery } from "@/hooks/use-transcript-history-query";
import { recordingTimeLabel, transcriptHistoryPeriod } from "@/lib/transcript-history-period";

export default function LiveMic() {
  const { toast } = useToast();
  const [isRecording, setIsRecording] = useState(false);
  const [recordingState, setRecordingState] = useState<LiveRecordingState>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [status, setStatus] = useState<string>("Stopped");
  const [inputWarning, setInputWarning] = useState("");
  const [inputWarningActions, setInputWarningActions] = useState<InputWarningAction[]>([]);
  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);
  const [toggleAction, setToggleAction] = useState<"start" | "stop" | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const deletingRef = useRef<string | null>(null);
  const copyingRef = useRef<string | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const toggleRequestInFlightRef = useRef(false);
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const getInitialViewMode = () => {
    if (typeof window === "undefined") return "grid" as const;
    const stored = window.localStorage.getItem(VIEW_MODE_STORAGE_KEY);
    if (stored === "list" || stored === "grid") return stored;
    return "grid" as const;
  };
  const initialViewMode = getInitialViewMode();
  const [viewMode, setViewMode] = useUrlQueryState<"list" | "grid">("view", initialViewMode, {
    parse: (raw) => (raw === "list" || raw === "grid" ? raw : initialViewMode),
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
  const recordingStartedAtMsRef = useRef<number | null>(null);
  const transcriptScrollRef = useRef<HTMLDivElement | null>(null);
  const transcriptsQueryKey = useMemo(
    () => transcriptHistoryQueryKey("mic", debouncedSearch),
    [debouncedSearch],
  );
  const { refreshNow: refreshMicHistory } = useTranscriptAutoRefresh({ queryKey: transcriptsQueryKey });
  const hasActiveSession = recordingState === "initializing" || recordingState === "recording" || recordingState === "finalizing";
  const isMicCaptureActive = recordingState === "initializing" || recordingState === "recording";
  const isPreparing = recordingState === "initializing";
  const isTranscribing = recordingState === "finalizing";

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, viewMode);
  }, [viewMode]);

  useEffect(() => () => {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
  }, []);

  const transcriptsQuery = useTranscriptHistoryQuery<Transcript>({ type: "mic", q: debouncedSearch });
  const transcripts = transcriptsQuery.items;
  const activeSessionIdRef = useRef<string | null>(null);

  const refreshVisualizerBarCount = useCallback(async (signal?: AbortSignal) => {
    try {
      const count = await loadVisualizerBarCount(signal);
      setVisualizerBarCount(count);
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setVisualizerBarCount(DEFAULT_VISUALIZER_BAR_COUNT);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshVisualizerBarCount(controller.signal);
    return () => controller.abort();
  }, [refreshVisualizerBarCount]);

  useEffect(() => {
    if (!isRecording) return;
    if (recordingStartedAtMsRef.current === null) {
      recordingStartedAtMsRef.current = Date.now();
    }
    const syncElapsed = () => {
      const startedAt = recordingStartedAtMsRef.current ?? Date.now();
      setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    };
    syncElapsed();
    const interval = window.setInterval(syncElapsed, 500);
    return () => window.clearInterval(interval);
  }, [isRecording]);

  useEffect(() => {
    if (!isRecording || !transcriptScrollRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const transcript = transcriptScrollRef.current;
      if (transcript) transcript.scrollTop = transcript.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [finalText, interimText, isRecording]);

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

  const applyBackendStateSnapshot = useCallback((state: BackendLiveStateSnapshot) => {
    if (typeof state.sessionId === "string" && state.sessionId) {
      activeSessionIdRef.current = state.sessionId;
    } else if (!state.listening) {
      activeSessionIdRef.current = null;
    }

    const nextState = coerceRecordingState(
      state.recordingState,
      state.listening ? "recording" : "idle",
    );
    setRecordingState(nextState);
    setIsRecording(nextState === "recording");
    if (nextState === "recording") {
      const current = state.current as Record<string, unknown> | null | undefined;
      const rawStartedAt = String(current?.processingStartedAt || current?.createdAt || "");
      const startedAtMs = Date.parse(rawStartedAt);
      if (Number.isFinite(startedAtMs)) {
        recordingStartedAtMsRef.current = startedAtMs;
      } else if (recordingStartedAtMsRef.current === null) {
        recordingStartedAtMsRef.current = Date.now();
      }
    }
    if (nextState !== "recording") {
      audioLevelRef.current = 0;
    }
    setStatus(state.status || "Stopped");
    applyInputWarning(state.inputWarning, state.inputWarningCode, state.inputWarningActions);
    if (state.current?.content) {
      setFinalText(String(state.current.content));
      setInterimText("");
    }
  }, [applyInputWarning]);

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
  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (!msg || typeof msg !== "object") return;
    const msgSessionId = typeof msg.sessionId === "string" ? msg.sessionId : null;
    const activeSessionId = activeSessionIdRef.current;

    switch (msg.type) {
      case "state":
        applyBackendStateSnapshot(msg);
        break;
      case "status":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        {
          const nextState = coerceRecordingState(msg.recordingState, msg.listening ? "recording" : "idle");
          setRecordingState(nextState);
          setIsRecording(nextState === "recording");
          if (nextState !== "recording") {
            audioLevelRef.current = 0;
          }
        }
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
        audioLevelRef.current = 0;
        recordingStartedAtMsRef.current = Date.now();
        setElapsed(0);
        setRecordingState("initializing");
        setIsRecording(false);
        setStatus("Preparing microphone...");
        applyInputWarning("", "", []);
        setFinalText("");
        setInterimText("");
        break;
      case "transcribing":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        audioLevelRef.current = 0;
        setRecordingState("finalizing");
        setIsRecording(false);
        setStatus("Transcribing...");
        break;
      case "session_finished":
        if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
          break;
        }
        activeSessionIdRef.current = null;
        audioLevelRef.current = 0;
        setRecordingState("idle");
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
        showRecordingErrorToast(toast, msg);
        audioLevelRef.current = 0;
        setRecordingState("idle");
        setIsRecording(false);
        setStatus("Stopped");
        applyInputWarning("", "", []);
        break;
      case "settings_updated":
        void refreshVisualizerBarCount();
        break;
      default:
        break;
    }
  }, [applyBackendStateSnapshot, applyInputWarning, refreshMicHistory, refreshVisualizerBarCount, toast]);

  // PERFORMANCE: Uses singleton WebSocket connection (shared across all pages)
  const { isConnected } = useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    if (!hasActiveSession && !isConnected) {
      return;
    }

    let cancelled = false;
    let requestInFlight = false;
    const controller = new AbortController();
    const reconcileBackendState = async () => {
      if (requestInFlight) return;
      requestInFlight = true;
      try {
        const res = await fetchWithTimeout(
          apiUrl("/api/state"),
          { credentials: "include", signal: controller.signal },
          5_000,
        );
        if (!res.ok) return;
        const state = (await res.json()) as BackendStateResponse;
        if (cancelled) return;
        applyBackendStateSnapshot(state);
      } catch {
        // WebSocket remains authoritative; this repairs missed terminal states and reconnect gaps.
      } finally {
        requestInFlight = false;
      }
    };

    const firstCheck = window.setTimeout(reconcileBackendState, hasActiveSession ? 750 : 0);
    const interval = hasActiveSession ? window.setInterval(reconcileBackendState, 2000) : undefined;
    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(firstCheck);
      if (interval !== undefined) {
        window.clearInterval(interval);
      }
    };
  }, [applyBackendStateSnapshot, hasActiveSession, isConnected]);

  const formatTime = (seconds: number) => {
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    const clock = `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
    return hours > 0 ? `${hours.toString().padStart(2, "0")}:${clock}` : clock;
  };

  const handleToggle = async () => {
    if (toggleRequestInFlightRef.current) return;
    toggleRequestInFlightRef.current = true;
    const action = hasActiveSession ? "stop" : "start";
    setToggleAction(action);
    if (action === "start") {
      recordingStartedAtMsRef.current = Date.now();
      setElapsed(0);
    }
    try {
      const endpoint = action === "stop" ? "/api/live-mic/stop" : "/api/live-mic/start";
      const res = await fetchWithTimeout(
        apiUrl(endpoint),
        { method: "POST", credentials: "include" },
        15_000,
      );
      if (!res.ok) {
        const text = await res.text();
        let payload: unknown = null;
        try {
          payload = text ? JSON.parse(text) : null;
        } catch {
          payload = null;
        }
        const recordingError = recordingErrorToastMessageFromPayload(payload, text || res.statusText);
        if (recordingError) {
          showRecordingErrorToast(toast, recordingError);
          return;
        }
        throw new Error(text || res.statusText);
      }
    } catch (e: any) {
      toast({
        title: "Action failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      toggleRequestInFlightRef.current = false;
      setToggleAction(null);
    }
  };

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (deletingRef.current) return;

    deletingRef.current = id;
    setDeletingId(id);
    try {
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      }, 15_000);
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
    // Preload the lazy-loaded TranscriptDetail page chunk
    import("@/pages/TranscriptDetail");
    // Prefetch the transcript data
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  const stageStatusLabel = toggleAction === "start"
    ? "Starting"
    : toggleAction === "stop"
      ? "Stopping"
      : isRecording
    ? "Listening"
    : isPreparing
      ? "Preparing microphone"
      : isTranscribing
        ? "Transcribing"
        : isConnected
          ? "Ready"
          : "Offline";

  const stageStatusHint = toggleAction === "start"
    ? "Connecting to your input device"
    : toggleAction === "stop"
      ? "Saving your recording"
      : isRecording
    ? "Tap the microphone to stop"
    : isPreparing
      ? "Connecting to your input device"
      : isTranscribing
        ? "Finalizing your transcript"
        : isConnected
          ? "Press the microphone to start"
          : "Reconnecting to Scriber";

  return (
    <div className="app-page-shell live-mic-page px-4 py-5 md:px-6 md:py-6" data-page-shell="live-mic">
      <PageIntro
        eyebrow="Voice capture · 01"
        title="Live transcription"
        description="Capture thoughts or longer dictation and watch the transcript appear as you speak."
        sticky={false}
      />

      <div className="space-y-7">
        <section className="live-mic-stage-shell">
          <div className="live-mic-stage-core">
          <div className="live-mic-control-deck relative flex min-h-[270px] flex-col items-center justify-center px-6 py-6 lg:min-h-0 lg:py-7">
            <div className="absolute left-5 top-5 inline-flex items-center gap-2 rounded-full bg-white/75 px-2.5 py-1.5 text-[11px] font-semibold text-slate-600 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.06)] dark:bg-[var(--live-card)] dark:text-slate-300">
              <span
                className={`h-2 w-2 rounded-full ${
                  isRecording
                    ? "bg-red-500 shadow-[0_0_0_4px_rgba(239,68,68,0.12)]"
                    : toggleAction !== null || isPreparing || isTranscribing
                      ? "bg-amber-400"
                      : isConnected
                        ? "bg-emerald-500"
                        : "bg-slate-400"
                }`}
                aria-hidden="true"
              />
              {stageStatusLabel}
            </div>

            <div className="flex flex-col items-center justify-center gap-3 pt-5">
              {/* Controls */}
              <GlossyMicButton
                isActive={isMicCaptureActive}
                disabled={isTranscribing || toggleAction !== null || (!isConnected && !hasActiveSession)}
                busy={toggleAction !== null || isPreparing || isTranscribing}
                label={
                  toggleAction === "start"
                    ? "Starting recording"
                    : toggleAction === "stop"
                      ? "Stopping recording"
                      : isTranscribing
                    ? "Transcribing recording"
                    : hasActiveSession
                      ? "Stop recording"
                      : "Start recording"
                }
                audioLevelRef={audioLevelRef}
                onToggle={handleToggle}
              />

              <div className="flex min-h-11 flex-col items-center justify-center gap-0.5">
                <p className="text-[12px] font-medium text-foreground/85">{stageStatusHint}</p>
                <p className={`font-mono text-[13px] font-semibold tabular-nums transition-colors duration-200 ${isMicCaptureActive ? "text-red-500" : "text-muted-foreground"}`}>
                  {formatTime(elapsed)}
                </p>
              </div>
            </div>
          </div>

          <div className="live-mic-transcript-deck flex min-w-0 flex-col p-5 md:p-6 lg:p-7">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-heading text-[18px] font-semibold tracking-[-0.015em] text-foreground">
                  {!hasActiveSession && finalText ? "Last transcript" : "Live transcript"}
                </h2>
                <p className="mt-1 text-[11.5px] leading-4 text-muted-foreground">
                  {!hasActiveSession && finalText ? "Saved to Recent recordings." : "Speech appears here while you record."}
                </p>
              </div>
              {(isPreparing || isTranscribing) && (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-1 text-[10.5px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                  {stageStatusLabel}
                </span>
              )}
            </div>

            {/* Live Text Output - Debossed status well for unified design */}
            <div className="live-mic-transcript-well flex min-h-[140px] flex-1 items-center p-5 text-left md:min-h-[168px] md:p-6">
              <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
                {isRecording
                  ? "Recording started."
                  : isPreparing
                    ? "Preparing microphone."
                    : isTranscribing
                      ? "Transcribing recording."
                      : "Recording stopped."}
              </p>
              <div
                ref={transcriptScrollRef}
                className="relative z-10 max-h-[228px] w-full overflow-y-auto overscroll-contain pr-1"
                aria-label={!hasActiveSession && finalText ? "Last saved transcript" : "Live transcript"}
              >
                {isRecording || finalText || interimText ? (
                  <p className="max-w-[70ch] text-[17px] font-medium leading-relaxed md:text-[19px]">
                    {(finalText || interimText) ? (
                      <>
                        <span className="text-foreground/90">{finalText}</span>
                        {interimText && (
                          <span className="text-muted-foreground italic">{finalText ? ' ' : ''}{interimText}</span>
                        )}
                      </>
                    ) : isRecording ? (
                      <span className="text-foreground/90">{status || "Listening"}...</span>
                    ) : (
                      <span className="text-muted-foreground">No speech was detected.</span>
                    )}
                  </p>
                ) : isPreparing || isTranscribing ? (
                  <p className="text-[14px] text-muted-foreground">{status}</p>
                ) : (
                  <div>
                    <p className="text-[15px] font-medium text-foreground/80">Your live transcript will appear here.</p>
                    <p className="mt-1.5 text-[12px] leading-5 text-muted-foreground">Start recording with the microphone button or your global hotkey.</p>
                  </div>
                )}
              </div>
            </div>

            {isRecording && inputWarning && (
              <div className="mt-3 space-y-3 rounded-xl border border-amber-400/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-200" role="alert">
                <p>{inputWarning}</p>
                {inputWarningActions.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {inputWarningActions.map((action) => (
                      <Button
                        key={`${action.id}-${action.uri}`}
                        type="button"
                        variant="secondary"
                        size="sm"
                        className="h-8 bg-amber-200/15 text-amber-800 hover:bg-amber-200/25 dark:text-amber-100"
                        onClick={() => handleInputWarningAction(action)}
                      >
                        {action.label}
                      </Button>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="live-mic-signal-bed mt-3 overflow-hidden rounded-lg px-1">
              <AudioVisualizer
                isRecording={isRecording}
                audioLevelRef={audioLevelRef}
                barCount={visualizerBarCount}
              />
            </div>
          </div>
          </div>
        </section>

        {/* History Section */}
        <section className="live-mic-history space-y-4 pb-2">
          <TranscriptionHistoryToolbar
            title="Recent recordings"
            description="Search, copy, or reopen your latest dictations."
            total={transcriptsQuery.total}
            itemLabel={transcriptsQuery.total === 1 ? "recording" : "recordings"}
            searchValue={searchQuery}
            onSearchChange={setSearchQuery}
            searchPlaceholder="Search recordings..."
            searchAriaLabel="Search recording history"
            clearSearchLabel="Clear recording search"
            viewMode={viewMode}
            onViewModeChange={setViewMode}
          />

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
              <VirtualTranscriptHistory
                items={transcripts}
                viewMode={viewMode}
                getItemKey={(item) => item.id}
                getItemGroup={(item) => transcriptHistoryPeriod(item.createdAt)}
                estimateListRowHeight={108}
                estimateGridRowHeight={210}
                hasMore={transcriptsQuery.hasNextPage}
                isLoadingMore={transcriptsQuery.isFetchingNextPage}
                onLoadMore={() => transcriptsQuery.fetchNextPage()}
                renderItem={(item) => (
                  <TranscriptCard
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
            )
          )}
        </section>
      </div>
    </div>
  );
}


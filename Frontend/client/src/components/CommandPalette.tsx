import { useState, useCallback, useRef, useEffect } from "react";
import { useLocation } from "wouter";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandShortcut,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Mic,
  Square,
  Settings,
  Youtube,
  FolderOpen,
  Home,
  Terminal,
  CalendarClock,
  Radio,
  type LucideIcon,
} from "lucide-react";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { useToast } from "@/hooks/use-toast";
import { presentLiveMicControlFailure, requestLiveMicStart, requestLiveMicStop } from "@/lib/live-mic-control";
import { createLiveMicSessionMessageGate, createLiveMicSnapshotFence } from "@/lib/runtime-message-state";
import type { BackendStateResponse, SettingsResponse, TranscriptHistoryItem } from "@/lib/api-types";
import { foldSearchText } from "@/lib/transcript-content";
import { useBackendActions } from "@/hooks/use-backend-status";
import { useI18n } from "@/i18n";

interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface TranscriptPage {
  items?: TranscriptHistoryItem[];
}

const SEARCH_DEBOUNCE_MS = 200;
const RECENT_LIMIT = 8;
const SEARCH_LIMIT = 20;

function previewText(value: string | undefined): string {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, value]);
  return debounced;
}

export function CommandPalette({ open, onOpenChange }: CommandPaletteProps) {
  const [, setLocation] = useLocation();
  const [isRecording, setIsRecording] = useState(false);
  const nativeCleanupPendingRef = useRef(false);
  const pendingSnapshotRef = useRef<ReturnType<typeof createLiveMicSnapshotFence> | null>(null);
  const [acceptSessionMessage] = useState(createLiveMicSessionMessageGate);
  const { toast } = useToast();
  const { checkNow: checkBackendStatus } = useBackendActions();
  const { formatDate: formatLocalizedDate, t } = useI18n();

  // Track recording state via WebSocket
  const handleWsMessage = useCallback(
    (msg: ScriberWebSocketMessage) => {
      if (!msg || typeof msg !== "object") return;
      if (!acceptSessionMessage(msg)) return;
      pendingSnapshotRef.current?.observe(msg);

      switch (msg.type) {
        case "state":
        case "status":
          if (msg.type === "state" || typeof msg.nativeCleanupPending === "boolean") {
            nativeCleanupPendingRef.current = msg.nativeCleanupPending === true;
          }
          setIsRecording(
            !!msg.listening ||
              !!msg.micStartPending ||
              nativeCleanupPendingRef.current ||
              msg.recordingState === "initializing",
          );
          break;
        case "session_started":
          setIsRecording(true);
          break;
        case "session_finished":
        case "transcribing":
        case "error":
          setIsRecording(nativeCleanupPendingRef.current);
          break;
      }
    },
    [acceptSessionMessage],
  );

  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    // The palette mounts lazily while the shared socket may already be open.
    const controller = new AbortController();
    const snapshotFence = createLiveMicSnapshotFence();
    pendingSnapshotRef.current = snapshotFence;
    void (async () => {
      try {
        const response = await fetchWithTimeout(
          apiUrl("/api/state"),
          { credentials: "include", signal: controller.signal },
          5_000,
        );
        if (!response.ok) return;
        const state = (await response.json()) as BackendStateResponse;
        if (controller.signal.aborted || !snapshotFence.acceptsSnapshot(state)) return;
        handleWsMessage({ ...state, type: "state", apiVersion: "1" });
      } catch {
        // Future socket snapshots remain authoritative after a failed read.
      } finally {
        if (pendingSnapshotRef.current === snapshotFence) pendingSnapshotRef.current = null;
      }
    })();
    return () => {
      controller.abort();
      if (pendingSnapshotRef.current === snapshotFence) pendingSnapshotRef.current = null;
    };
  }, [handleWsMessage]);

  // Load settings to get the configured hotkey
  const { data: settings } = useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(
        apiUrl("/api/settings"),
        {
          credentials: "include",
          signal,
        },
        10_000,
      );
      if (!res.ok) throw new Error(t("Failed to load settings"));
      return (await res.json()) as SettingsResponse;
    },
    staleTime: 60000, // Cache for 1 minute
  });

  // Get display hotkey from settings
  const recordingHotkey = settings?.hotkey || settings?.hotkeyRaw || "";

  const [search, setSearch] = useState("");
  useEffect(() => {
    if (!open) setSearch("");
  }, [open]);
  const trimmedSearch = search.trim();
  const debouncedSearch = useDebouncedValue(trimmedSearch, SEARCH_DEBOUNCE_MS);
  // Clearing the input returns to recent transcripts immediately.
  const serverSearch = trimmedSearch ? debouncedSearch : "";

  const fetchTranscriptPage = async (path: string, signal: AbortSignal) => {
    const res = await fetchWithTimeout(apiUrl(path), { credentials: "include", signal }, 10_000);
    if (!res.ok) throw new Error(t("Failed to load transcripts"));
    return (await res.json()) as TranscriptPage;
  };

  // Recent transcripts while the input is empty.
  const recentQuery = useQuery<TranscriptPage>({
    queryKey: ["/api/transcripts", { limit: RECENT_LIMIT }],
    queryFn: ({ signal }) => fetchTranscriptPage(`/api/transcripts?limit=${RECENT_LIMIT}`, signal),
    enabled: open && !serverSearch,
    staleTime: 30_000,
  });

  // Server full-text search (title, content, channel) once typing pauses.
  const searchQuery = useQuery<TranscriptPage>({
    queryKey: ["/api/transcripts", { q: serverSearch, limit: SEARCH_LIMIT }],
    queryFn: ({ signal }) =>
      fetchTranscriptPage(`/api/transcripts?q=${encodeURIComponent(serverSearch)}&limit=${SEARCH_LIMIT}`, signal),
    enabled: open && Boolean(serverSearch),
    placeholderData: keepPreviousData,
    staleTime: 15_000,
  });

  const isSearching = Boolean(trimmedSearch);
  const transcripts = (isSearching ? searchQuery.data?.items : recentQuery.data?.items) ?? [];
  const searchPending =
    isSearching &&
    (trimmedSearch !== debouncedSearch ||
      searchQuery.isPending ||
      (searchQuery.isPlaceholderData && searchQuery.isFetching));

  // Navigation helper
  const navigate = (path: string) => {
    setLocation(path);
    onOpenChange(false);
  };

  // cmdk filtering is disabled so server-ranked transcript matches (often in
  // content only) are never hidden or reordered. The small static command
  // lists are filtered here instead.
  const matchesSearch = (label: string, keywords: readonly string[] = []) => {
    if (!trimmedSearch) return true;
    const haystack = foldSearchText([label, ...keywords].join(" "));
    return foldSearchText(trimmedSearch)
      .split(/\s+/)
      .every((token) => haystack.includes(token));
  };

  const navigationItems: Array<{ path: string; label: string; icon: LucideIcon; keywords: string[] }> = [
    { path: "/", label: t("Live Mic"), icon: Home, keywords: ["live mic", "home", "dictation"] },
    { path: "/youtube", label: "YouTube", icon: Youtube, keywords: ["video"] },
    { path: "/file", label: t("File upload"), icon: FolderOpen, keywords: ["file", "upload"] },
    { path: "/podcasts", label: t("Podcasts"), icon: Radio, keywords: ["podcast", "feed", "rss"] },
    { path: "/meetings", label: t("Meetings"), icon: CalendarClock, keywords: ["meeting"] },
    { path: "/debug", label: t("Debug Console"), icon: Terminal, keywords: ["debug", "logs"] },
    { path: "/settings", label: t("Settings"), icon: Settings, keywords: ["settings", "preferences"] },
  ];
  const visibleNavigation = navigationItems.filter((item) => matchesSearch(item.label, item.keywords));
  const recordingLabel = isRecording ? t("Stop recording") : t("Start recording");
  const showRecordingAction = matchesSearch(recordingLabel, ["record", "recording", "microphone", "dictation"]);

  // Toggle recording
  const handleToggleRecording = async () => {
    try {
      if (isRecording) {
        await requestLiveMicStop();
      } else {
        await requestLiveMicStart();
      }
      onOpenChange(false);
    } catch (error) {
      presentLiveMicControlFailure(error, {
        toast,
        checkBackendStatus,
      });
    }
  };

  // Format date for display
  const formatDate = (dateStr: string) => {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return "";
    return formatLocalizedDate(date, {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  };

  // Get icon for transcript type
  const getTranscriptIcon = (type: string) => {
    switch (type) {
      case "youtube":
        return Youtube;
      case "file":
        return FolderOpen;
      default:
        return Mic;
    }
  };

  // Untitled transcripts show the start of their stored preview text.
  const getDisplayTitle = (transcript: TranscriptHistoryItem) => {
    const title = String(transcript.title || "").trim();
    if (title) return title;
    const preview = previewText(transcript.preview);
    if (preview) return preview.length > 80 ? `${preview.slice(0, 80).trimEnd()}…` : preview;
    return t("Untitled transcript");
  };

  const showTranscriptsGroup = transcripts.length > 0;
  const searchFailed = isSearching && !searchPending && searchQuery.isError;
  const hasStaticItems = showRecordingAction || visibleNavigation.length > 0;

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} commandProps={{ shouldFilter: false }}>
      <CommandInput
        value={search}
        onValueChange={setSearch}
        placeholder={t("Type a command or search transcripts...")}
      />
      <CommandList>
        {!searchPending && !searchFailed && <CommandEmpty>{t("No results found.")}</CommandEmpty>}
        {searchFailed && (
          <div role="alert" className="py-6 text-center text-sm text-destructive">
            {t("Failed to load transcripts")}
          </div>
        )}

        {/* Actions */}
        {showRecordingAction && (
          <CommandGroup heading={t("Actions")}>
            <CommandItem value="action:toggle-recording" onSelect={handleToggleRecording}>
              {isRecording ? <Square className="mr-2 h-4 w-4 text-red-500" /> : <Mic className="mr-2 h-4 w-4" />}
              <span>{recordingLabel}</span>
              {recordingHotkey && <CommandShortcut>{recordingHotkey}</CommandShortcut>}
            </CommandItem>
          </CommandGroup>
        )}

        {showRecordingAction && visibleNavigation.length > 0 && <CommandSeparator />}

        {/* Navigation */}
        {visibleNavigation.length > 0 && (
          <CommandGroup heading={t("Navigation")}>
            {visibleNavigation.map(({ path, label, icon: Icon }) => (
              <CommandItem key={path} value={`nav:${path}`} onSelect={() => navigate(path)}>
                <Icon className="mr-2 h-4 w-4" />
                <span>{label}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {/* Transcripts: recent while empty, server full-text ranking while searching */}
        {showTranscriptsGroup && (
          <>
            {hasStaticItems && <CommandSeparator />}
            <CommandGroup
              heading={isSearching ? t("Transcripts") : t("Recent Transcripts")}
              aria-busy={searchPending || undefined}
              data-command-transcripts={isSearching ? "search" : "recent"}
            >
              {transcripts.map((transcript) => {
                const Icon = getTranscriptIcon(transcript.type);
                const displayTitle = getDisplayTitle(transcript);
                const preview = isSearching && transcript.title ? previewText(transcript.preview) : "";

                return (
                  <CommandItem
                    key={transcript.id}
                    value={`transcript:${transcript.id}`}
                    onSelect={() => navigate(`/transcript/${transcript.id}`)}
                    className={searchPending ? "opacity-70" : undefined}
                  >
                    <Icon className="mr-2 h-4 w-4" />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate">{displayTitle}</span>
                      {preview && <span className="truncate text-xs text-muted-foreground">{preview}</span>}
                    </span>
                    <span className="ml-2 shrink-0 text-xs text-muted-foreground">
                      {formatDate(transcript.createdAt || "")}
                    </span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </>
        )}

        {searchPending && !showTranscriptsGroup && (
          <div className="py-6 text-center text-sm text-muted-foreground" aria-live="polite">
            {t("Searching transcripts…")}
          </div>
        )}
      </CommandList>
    </CommandDialog>
  );
}

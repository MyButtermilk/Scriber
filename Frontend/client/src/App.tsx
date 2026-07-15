import { Switch, Route, useLocation } from "wouter";
import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { AppLayout } from "@/components/layout/AppLayout";
import { ThemeProvider } from "@/components/theme-provider";
import { BackendStatusProvider, useBackendStatus } from "@/hooks/use-backend-status";
import { useDeviceChangeRefresh } from "@/hooks/use-device-change-refresh";
import { BackendOfflineBanner } from "@/components/BackendOfflineBanner";
import { useSharedWebSocket, WebSocketProvider, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { recordingErrorToastMessageFromPayload, showRecordingErrorToast } from "@/lib/recording-error-toast";
import { useToast } from "@/hooks/use-toast";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { isTauriRuntime, loadBackendBaseUrlFromTauri, setTrayRecordingState } from "@/lib/backend";
import { withPromiseTimeout } from "@/lib/fetch-with-timeout";
import { preloadPrimaryTabData } from "@/lib/tab-data-preload";
import {
  flushFrontendPerformanceReport,
  setFrontendPerformanceReportingEnabled,
} from "@/lib/frontend-performance";
import { ToastAction } from "@/components/ui/toast";
import { Download } from "lucide-react";
import {
  checkDesktopUpdateIfDue,
  getCachedDesktopUpdateStatus,
  installDesktopUpdate,
  publishDesktopUpdateStatusToTray,
  shouldNotifyDesktopUpdate,
  subscribeDesktopUpdateStatus,
  type DesktopUpdateStatus,
} from "@/lib/desktop-updates";

// Primary tabs are eager so first navigation never blanks while a local chunk loads.
import LiveMic from "@/pages/LiveMic";
import Youtube from "@/pages/Youtube";
import FileTranscribe from "@/pages/FileTranscribe";
import Meetings from "@/pages/Meetings";
import Settings from "@/pages/Settings";
const DebugConsole = lazy(() => import("@/pages/DebugConsole"));

// Lazy load only rarely accessed pages for slightly smaller initial bundle
const TranscriptDetail = lazy(() => import("@/pages/TranscriptDetail"));
const NotFound = lazy(() => import("@/pages/not-found"));

// Loading fallback component - only needed for lazy-loaded pages
function PageLoader() {
  return (
    <div className="flex min-h-[300px] items-start justify-center px-6 py-8" aria-label="Loading section">
      <div className="w-full max-w-5xl space-y-4">
        <div className="h-8 w-44 animate-pulse rounded-lg bg-slate-200/80 dark:bg-slate-800/80" />
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="h-24 animate-pulse rounded-xl bg-slate-200/70 dark:bg-slate-800/70" />
          <div className="h-24 animate-pulse rounded-xl bg-slate-200/60 dark:bg-slate-800/60" />
          <div className="h-24 animate-pulse rounded-xl bg-slate-200/50 dark:bg-slate-800/50" />
        </div>
        <div className="h-40 animate-pulse rounded-xl bg-slate-200/60 dark:bg-slate-800/60" />
      </div>
    </div>
  );
}

function TabRoutes() {
  const [location] = useLocation();
  return (
    <AppLayout path={location}>
      <Suspense fallback={<PageLoader />}>
        <Switch>
          <Route path="/" component={LiveMic} />
          <Route path="/meetings/:id" component={Meetings} />
          <Route path="/meetings" component={Meetings} />
          <Route path="/youtube" component={Youtube} />
          <Route path="/file" component={FileTranscribe} />
          <Route path="/debug" component={DebugConsole} />
          <Route path="/settings" component={Settings} />
          <Route component={NotFound} />
        </Switch>
      </Suspense>
    </AppLayout>
  );
}

function Router() {
  return (
    <Switch>
      <Route path="/transcript/:id">
        <Suspense fallback={<PageLoader />}>
          <TranscriptDetail />
        </Suspense>
      </Route>
      <Route component={TabRoutes} />
    </Switch>
  );
}

function RecordingErrorToastBridge() {
  const { toast } = useToast();

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (msg.type === "error") {
      showRecordingErrorToast(toast, msg);
      return;
    }
    if (msg.type === "session_finished" && String(msg.session?.status || "").toLowerCase() === "failed") {
      const content = String(msg.session?.content || "");
      const match = content.match(/\[Error\]\s*([^\n]+)/i);
      const message = match?.[1]?.trim() || "Live mic transcription failed. Check the selected provider and try again.";
      const recordingError = recordingErrorToastMessageFromPayload({
        type: "error",
        apiVersion: msg.apiVersion,
        message,
        title: "Recording Error",
        sessionId: msg.sessionId || (msg.session?.id != null ? String(msg.session.id) : undefined),
      });
      if (recordingError) {
        showRecordingErrorToast(toast, recordingError);
      }
    }
  }, [toast]);

  useSharedWebSocket(handleWsMessage);
  return null;
}

function TranscriptHistoryInvalidationBridge() {
  const queryClient = useQueryClient();
  const hasConnectedRef = useRef(false);
  const wasConnectedRef = useRef(false);
  const pendingTranscriptIdsRef = useRef<Set<string>>(new Set());
  const pendingTranscriptTypesRef = useRef<Set<string>>(new Set());
  const invalidateAllDetailsRef = useRef(false);
  const invalidateAllHistoryRef = useRef(false);
  const invalidationTimerRef = useRef<number | null>(null);

  const flushInvalidations = useCallback(() => {
    invalidationTimerRef.current = null;
    const transcriptIds = Array.from(pendingTranscriptIdsRef.current);
    const transcriptTypes = new Set(pendingTranscriptTypesRef.current);
    const invalidateAllDetails = invalidateAllDetailsRef.current;
    const invalidateAllHistory = invalidateAllHistoryRef.current;
    pendingTranscriptIdsRef.current.clear();
    pendingTranscriptTypesRef.current.clear();
    invalidateAllDetailsRef.current = false;
    invalidateAllHistoryRef.current = false;

    for (const transcriptId of transcriptIds) {
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts", transcriptId], exact: true });
    }
    if (invalidateAllDetails) {
      queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] === "/api/transcripts"
          && typeof query.queryKey[1] === "string",
      });
    }

    queryClient.invalidateQueries({
      predicate: (query) => {
        if (query.queryKey[0] !== "/api/transcripts") {
          return false;
        }
        const scope = query.queryKey[1];
        if (typeof scope !== "object" || scope === null) {
          return false;
        }
        return invalidateAllHistory || transcriptTypes.has(String((scope as { type?: string }).type || ""));
      },
    });
  }, [queryClient]);

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (!msg || msg.type !== "history_updated") {
      return;
    }

    if (msg.transcriptId) {
      pendingTranscriptIdsRef.current.add(msg.transcriptId);
    } else {
      invalidateAllDetailsRef.current = true;
    }
    if (msg.transcriptType) {
      pendingTranscriptTypesRef.current.add(msg.transcriptType);
    } else {
      invalidateAllHistoryRef.current = true;
    }
    if (invalidationTimerRef.current === null) {
      invalidationTimerRef.current = window.setTimeout(flushInvalidations, 250);
    }
  }, [flushInvalidations]);

  useEffect(() => () => {
    if (invalidationTimerRef.current !== null) {
      window.clearTimeout(invalidationTimerRef.current);
      invalidationTimerRef.current = null;
    }
  }, []);

  const { isConnected } = useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    if (isConnected && hasConnectedRef.current && !wasConnectedRef.current) {
      invalidateAllDetailsRef.current = true;
      invalidateAllHistoryRef.current = true;
      if (invalidationTimerRef.current !== null) {
        window.clearTimeout(invalidationTimerRef.current);
      }
      flushInvalidations();
    }
    if (isConnected) {
      hasConnectedRef.current = true;
    }
    wasConnectedRef.current = isConnected;
  }, [flushInvalidations, isConnected]);
  return null;
}

function MeetingDetectionBridge() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const seenRef = useRef<Set<string>>(new Set());

  const handleWsMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type !== "meeting_detected" || seenRef.current.has(message.detectionId)) return;
    seenRef.current.add(message.detectionId);
    if (message.source === "hotkey") {
      setLocation(message.meetingId ? `/meetings/${message.meetingId}` : "/meetings");
    }
    toast({
      title: message.meetingId ? "Meeting controls opened" : "Meeting recording requires confirmation",
      description: message.label,
      duration: 5000,
      action: message.source === "hotkey" ? undefined : (
        <ToastAction altText="Review meeting recording" onClick={() => setLocation("/meetings")}>
          Review
        </ToastAction>
      ),
    });
  }, [setLocation, toast]);

  useSharedWebSocket(handleWsMessage);
  return null;
}

const DESKTOP_UPDATE_STARTUP_DELAY_MS = 12_000;
const DESKTOP_UPDATE_BACKGROUND_POLL_MS = 6 * 60 * 60 * 1000;
const SETTINGS_SECTION_REQUEST_KEY = "scriber:open-settings-section";

function isBusyForUpdatePrompt(msg: ScriberWebSocketMessage): boolean | null {
  if (msg.type === "meeting_state") {
    return ["starting", "recording", "paused", "stopping", "finalizing", "analyzing"].includes(
      String(msg.meeting.state || "").toLowerCase(),
    );
  }
  if (msg.type === "transcribing" || msg.type === "session_started") {
    return true;
  }
  if (msg.type === "session_finished") {
    return false;
  }
  if (msg.type === "state" || msg.type === "status") {
    const recordingState = String(msg.recordingState || "").toLowerCase();
    return Boolean(
      msg.listening ||
      (msg.type === "state" && msg.voiceEnrollmentActive) ||
      msg.transcribing ||
      (recordingState && !["idle", "completed", "failed", "stopped"].includes(recordingState)),
    );
  }
  return null;
}

function trayRecordingStateFromMessage(
  msg: ScriberWebSocketMessage,
): { active: boolean; mode: string } | null {
  if (msg.type === "meeting_state") {
    const meetingState = String(msg.meeting.state || "").toLowerCase();
    if (["starting", "recording", "paused"].includes(meetingState)) {
      return { active: true, mode: `meeting-${meetingState}` };
    }
    if (["stopping", "finalizing", "analyzing"].includes(meetingState)) {
      return { active: false, mode: `meeting-${meetingState}` };
    }
    return { active: false, mode: "idle" };
  }
  if (msg.type === "session_started") {
    return { active: true, mode: "initializing" };
  }
  if (msg.type === "transcribing") {
    return { active: false, mode: "transcribing" };
  }
  if (msg.type === "session_finished" || msg.type === "error") {
    return { active: false, mode: "idle" };
  }
  if (msg.type !== "state" && msg.type !== "status") {
    return null;
  }

  const recordingState = String(msg.recordingState || "").toLowerCase();
  if (recordingState === "initializing") {
    return { active: true, mode: "initializing" };
  }
  if (recordingState === "recording" || msg.listening) {
    return { active: true, mode: "recording" };
  }
  if (recordingState === "finalizing" || msg.transcribing) {
    return { active: false, mode: "transcribing" };
  }
  if (["idle", "completed", "failed", "stopped"].includes(recordingState)) {
    return { active: false, mode: "idle" };
  }
  return null;
}

function TrayRecordingStateBridge() {
  const lastStateRef = useRef("");

  const publish = useCallback((active: boolean, mode: string) => {
    if (!isTauriRuntime()) {
      return;
    }
    const key = `${active}:${mode}`;
    if (lastStateRef.current === key) {
      return;
    }
    lastStateRef.current = key;
    void setTrayRecordingState(active, mode).catch((error) => {
      console.debug("Tray recording state sync failed.", error);
    });
  }, []);

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    const next = trayRecordingStateFromMessage(msg);
    if (!next) {
      return;
    }
    publish(next.active, next.mode);
  }, [publish]);

  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    publish(false, "idle");
  }, [publish]);

  return null;
}

type TauriNavigationRequest = {
  navigationId?: number;
  path?: string;
};

function TauriNavigationBridge() {
  const [, setLocation] = useLocation();
  const lastNavigationIdRef = useRef(0);

  const applyNavigation = useCallback((request: TauriNavigationRequest | null | undefined) => {
    const path = String(request?.path || "").trim();
    if (!path.startsWith("/")) {
      return;
    }
    const navigationId = Number(request?.navigationId || 0);
    if (Number.isSafeInteger(navigationId) && navigationId > 0) {
      if (navigationId <= lastNavigationIdRef.current) {
        return;
      }
      lastNavigationIdRef.current = navigationId;
    }
    setLocation(path);
    if (Number.isSafeInteger(navigationId) && navigationId > 0) {
      void import("@tauri-apps/api/core")
        .then(({ invoke }) => invoke<boolean>("acknowledge_navigation", { navigationId }))
        .catch((error) => console.debug("Tauri navigation acknowledgement failed.", error));
    }
  }, [setLocation]);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    let unlisten: (() => void) | undefined;
    let disposed = false;
    void (async () => {
      const [{ listen }, { invoke }] = await Promise.all([
        import("@tauri-apps/api/event"),
        import("@tauri-apps/api/core"),
      ]);
      const cleanup = await listen<TauriNavigationRequest>("scriber-navigate", (event) => {
        applyNavigation(event.payload);
      });
      if (disposed) {
        cleanup();
        return;
      }
      unlisten = cleanup;
      const pending = await invoke<TauriNavigationRequest | null>("navigation_listener_ready");
      if (!disposed) {
        applyNavigation(pending);
      }
    })()
      .catch((error) => console.debug("Tauri navigation listener failed.", error));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [applyNavigation]);

  return null;
}

function DesktopUpdateAutoCheckBridge() {
  const { toast, dismiss } = useToast();
  const [, setLocation] = useLocation();
  const busyRef = useRef(false);
  const installingFromToastRef = useRef(false);
  const notifiedVersionRef = useRef<string | null>(null);

  const openUpdateSettings = useCallback(() => {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(SETTINGS_SECTION_REQUEST_KEY, "updates");
    }
    setLocation("/settings");
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("scriber-open-settings-section", { detail: { section: "updates" } }));
    }, 80);
    dismiss();
  }, [dismiss, setLocation]);

  const handleUpdateToastClick = useCallback((event: ReactMouseEvent) => {
    const target = event.target as HTMLElement | null;
    if (target?.closest("button,a,[role='button']")) {
      return;
    }
    openUpdateSettings();
  }, [openUpdateSettings]);

  const installUpdateFromToast = useCallback(async (event?: ReactMouseEvent) => {
    event?.stopPropagation();
    if (installingFromToastRef.current) {
      return;
    }
    installingFromToastRef.current = true;
    toast({
      variant: "update",
      title: "Installing update",
      description: "Scriber is downloading the update and will restart when it is ready.",
      duration: 30000,
    });
    try {
      await installDesktopUpdate();
    } catch (error) {
      installingFromToastRef.current = false;
      toast({
        variant: "destructive",
        title: "Update failed",
        description: error instanceof Error ? error.message : String(error || "Update installation failed."),
        duration: 7000,
      });
    }
  }, [toast]);

  const maybeNotify = useCallback((status: DesktopUpdateStatus) => {
    if (busyRef.current || !shouldNotifyDesktopUpdate(status) || !status.version) {
      return;
    }
    if (notifiedVersionRef.current === status.version) {
      return;
    }
    notifiedVersionRef.current = status.version;
    toast({
      variant: "update",
      title: (
        <span className="flex items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-600 text-white shadow-[0_10px_22px_rgba(37,99,235,0.22)]">
            <Download className="h-4 w-4" aria-hidden="true" />
          </span>
          <span>Update ready</span>
        </span>
      ),
      description: (
        <span>
          Scriber {status.version} is available. Click this notice to open update settings.
        </span>
      ),
      duration: 12000,
      className: "select-none",
      onClick: handleUpdateToastClick,
      action: (
        <ToastAction altText="Install update now" onClick={(event) => void installUpdateFromToast(event)}>
          Install now
        </ToastAction>
      ),
    });
  }, [handleUpdateToastClick, installUpdateFromToast, toast]);

  const checkIfDue = useCallback(() => {
    if (!isTauriRuntime()) {
      return;
    }
    void checkDesktopUpdateIfDue({ isBusy: busyRef.current })
      .then((result) => maybeNotify(result.status))
      .catch((error) => console.debug("Desktop update background check failed.", error));
  }, [maybeNotify]);

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    const busy = isBusyForUpdatePrompt(msg);
    if (busy === null) {
      return;
    }
    const wasBusy = busyRef.current;
    busyRef.current = busy;
    if (wasBusy && !busy) {
      maybeNotify(getCachedDesktopUpdateStatus());
    }
  }, [maybeNotify]);

  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    if (!isTauriRuntime()) {
      return;
    }
    const cached = getCachedDesktopUpdateStatus();
    publishDesktopUpdateStatusToTray(cached);
    maybeNotify(cached);
    const unsubscribe = subscribeDesktopUpdateStatus(maybeNotify);
    const startupTimer = window.setTimeout(checkIfDue, DESKTOP_UPDATE_STARTUP_DELAY_MS);
    const interval = window.setInterval(checkIfDue, DESKTOP_UPDATE_BACKGROUND_POLL_MS);
    return () => {
      window.clearTimeout(startupTimer);
      window.clearInterval(interval);
      unsubscribe();
    };
  }, [checkIfDue, maybeNotify]);

  return null;
}

function RuntimeShell() {
  const { isOnline } = useBackendStatus();
  const queryClient = useQueryClient();
  const websocketEnabled = isOnline;

  useEffect(() => {
    if (!isOnline) return;
    return preloadPrimaryTabData(queryClient);
  }, [isOnline, queryClient]);

  useEffect(() => {
    setFrontendPerformanceReportingEnabled(isOnline);
    return () => setFrontendPerformanceReportingEnabled(false);
  }, [isOnline]);

  return (
    <WebSocketProvider path="/ws" autoReconnect={true} reconnectDelay={1000} enabled={websocketEnabled}>
      <FrontendPerformanceFlushBridge />
      <RecordingErrorToastBridge />
      <TranscriptHistoryInvalidationBridge />
      <MeetingDetectionBridge />
      <TrayRecordingStateBridge />
      <TauriNavigationBridge />
      <DesktopUpdateAutoCheckBridge />
      <BackendOfflineBanner />
      <Router />
    </WebSocketProvider>
  );
}

function FrontendPerformanceFlushBridge() {
  const handleWsMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type !== "frontend_performance_flush") {
      return;
    }
    void flushFrontendPerformanceReport(
      message.heartbeatSequence,
      message.sourceInstanceId,
    );
  }, []);

  useSharedWebSocket(handleWsMessage);
  return null;
}

function App() {
  const [backendBaseReady, setBackendBaseReady] = useState(!isTauriRuntime());

  useEffect(() => {
    let cancelled = false;
    void withPromiseTimeout(
      loadBackendBaseUrlFromTauri(),
      5_000,
      "Initial Tauri backend lookup",
    )
      .catch((error) => {
        console.debug("Initial Tauri backend lookup failed; continuing with health fallback.", error);
      })
      .finally(() => {
        if (!cancelled) {
          setBackendBaseReady(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useDeviceChangeRefresh(backendBaseReady);

  if (!backendBaseReady) {
    return (
      <ThemeProvider defaultTheme="system" storageKey="scriber-theme">
        <PageLoader />
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider defaultTheme="system" storageKey="scriber-theme">
      <QueryClientProvider client={queryClient}>
        <BackendStatusProvider>
          <Toaster />
          <RuntimeShell />
        </BackendStatusProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}

export default App;


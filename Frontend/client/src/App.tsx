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
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { isTauriRuntime, loadBackendBaseUrlFromTauri } from "@/lib/backend";
import { preloadPrimaryTabChunks } from "@/lib/route-preload";
import { preloadPrimaryTabData } from "@/lib/tab-data-preload";
import { ToastAction } from "@/components/ui/toast";
import {
  checkDesktopUpdateIfDue,
  getCachedDesktopUpdateStatus,
  shouldNotifyDesktopUpdate,
  subscribeDesktopUpdateStatus,
  type DesktopUpdateStatus,
} from "@/lib/desktop-updates";

// Keep default route eager for fastest first paint, lazy-load heavier routes.
import LiveMic from "@/pages/LiveMic";
const Youtube = lazy(() => import("@/pages/Youtube"));
const FileTranscribe = lazy(() => import("@/pages/FileTranscribe"));
const Settings = lazy(() => import("@/pages/Settings"));
const DebugConsole = lazy(() => import("@/pages/DebugConsole"));

// Lazy load only rarely accessed pages for slightly smaller initial bundle
const TranscriptDetail = lazy(() => import("@/pages/TranscriptDetail"));
const NotFound = lazy(() => import("@/pages/not-found"));

// Loading fallback component - only needed for lazy-loaded pages
function PageLoader() {
  return (
    <div className="flex items-center justify-center min-h-[300px]">
      <div className="animate-pulse text-muted-foreground">Loading...</div>
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

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (!msg || msg.type !== "history_updated") {
      return;
    }

    if (msg.transcriptId) {
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts", msg.transcriptId], exact: true });
    }

    queryClient.invalidateQueries({
      predicate: (query) => {
        if (query.queryKey[0] !== "/api/transcripts") {
          return false;
        }

        if (!msg.transcriptType) {
          return true;
        }

        const scope = query.queryKey[1];
        if (typeof scope !== "object" || scope === null) {
          return false;
        }
        return (scope as { type?: string }).type === msg.transcriptType;
      },
    });
  }, [queryClient]);

  useSharedWebSocket(handleWsMessage);
  return null;
}

const DESKTOP_UPDATE_STARTUP_DELAY_MS = 12_000;
const DESKTOP_UPDATE_BACKGROUND_POLL_MS = 6 * 60 * 60 * 1000;

function isBusyForUpdatePrompt(msg: ScriberWebSocketMessage): boolean | null {
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
      msg.transcribing ||
      (recordingState && !["idle", "completed", "failed", "stopped"].includes(recordingState)),
    );
  }
  return null;
}

function DesktopUpdateAutoCheckBridge() {
  const { toast } = useToast();
  const [, setLocation] = useLocation();
  const busyRef = useRef(false);
  const notifiedVersionRef = useRef<string | null>(null);

  const maybeNotify = useCallback((status: DesktopUpdateStatus) => {
    if (busyRef.current || !shouldNotifyDesktopUpdate(status) || !status.version) {
      return;
    }
    if (notifiedVersionRef.current === status.version) {
      return;
    }
    notifiedVersionRef.current = status.version;
    toast({
      title: "Update available",
      description: `Scriber ${status.version} is ready to install.`,
      duration: 9000,
      action: (
        <ToastAction altText="Open update settings" onClick={() => setLocation("/settings")}>
          View
        </ToastAction>
      ),
    });
  }, [setLocation, toast]);

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

  return (
    <WebSocketProvider path="/ws" autoReconnect={true} reconnectDelay={1000} enabled={websocketEnabled}>
      <RecordingErrorToastBridge />
      <TranscriptHistoryInvalidationBridge />
      <DesktopUpdateAutoCheckBridge />
      <BackendOfflineBanner />
      <Router />
    </WebSocketProvider>
  );
}

function App() {
  const [backendBaseReady, setBackendBaseReady] = useState(!isTauriRuntime());

  useEffect(() => {
    let cancelled = false;
    void loadBackendBaseUrlFromTauri().finally(() => {
      if (!cancelled) {
        setBackendBaseReady(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!backendBaseReady) return;
    return preloadPrimaryTabChunks();
  }, [backendBaseReady]);

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


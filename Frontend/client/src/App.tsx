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
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { isTauriRuntime, loadBackendBaseUrlFromTauri } from "@/lib/backend";
import { preloadPrimaryTabChunks } from "@/lib/route-preload";
import { preloadPrimaryTabData } from "@/lib/tab-data-preload";

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


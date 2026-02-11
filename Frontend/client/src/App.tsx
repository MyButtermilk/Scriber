import { Switch, Route, useLocation } from "wouter";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { AppLayout } from "@/components/layout/AppLayout";
import { ThemeProvider } from "@/components/theme-provider";
import { BackendStatusProvider } from "@/hooks/use-backend-status";
import { BackendOfflineBanner } from "@/components/BackendOfflineBanner";
import { WebSocketProvider } from "@/contexts/WebSocketContext";
import { lazy, Suspense } from "react";

// Keep default route eager for fastest first paint, lazy-load heavier routes.
import LiveMic from "@/pages/LiveMic";
const Youtube = lazy(() => import("@/pages/Youtube"));
const FileTranscribe = lazy(() => import("@/pages/FileTranscribe"));
const Settings = lazy(() => import("@/pages/Settings"));

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

function App() {
  return (
    <ThemeProvider defaultTheme="system" storageKey="scriber-theme">
      <QueryClientProvider client={queryClient}>
        <BackendStatusProvider>
          {/* PERFORMANCE: Single WebSocket connection shared across all pages */}
          <WebSocketProvider path="/ws" autoReconnect={true} reconnectDelay={1000}>
            <Toaster />
            <BackendOfflineBanner />
            <Router />
          </WebSocketProvider>
        </BackendStatusProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}

export default App;


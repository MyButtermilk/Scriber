import { Switch, Route, useLocation } from "wouter";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { AppLayout } from "@/components/layout/AppLayout";
import { ThemeProvider } from "@/components/theme-provider";
import { lazy, Suspense } from "react";

// Lazy load pages for code splitting - each page becomes a separate chunk
// This reduces initial bundle size by 30-50%
const LiveMic = lazy(() => import("@/pages/LiveMic"));
const Youtube = lazy(() => import("@/pages/Youtube"));
const FileTranscribe = lazy(() => import("@/pages/FileTranscribe"));
const Settings = lazy(() => import("@/pages/Settings"));
const TranscriptDetail = lazy(() => import("@/pages/TranscriptDetail"));
const NotFound = lazy(() => import("@/pages/not-found"));

// Loading fallback component - minimal to avoid layout shift
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
        {(params) => (
          <Suspense fallback={<PageLoader />}>
            <TranscriptDetail />
          </Suspense>
        )}
      </Route>
      <Route component={TabRoutes} />
    </Switch>
  );
}

function App() {
  return (
    <ThemeProvider defaultTheme="system" storageKey="scriber-theme">
      <QueryClientProvider client={queryClient}>
        <Toaster />
        <Router />
      </QueryClientProvider>
    </ThemeProvider>
  );
}

export default App;

import { Switch, Route, useLocation } from "wouter";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { AppLayout } from "@/components/layout/AppLayout";
import { ThemeProvider } from "@/components/theme-provider";
import { lazy, Suspense } from "react";

// Main navigation pages - eagerly loaded to ensure instant section switching
// These are bundled together since users frequently navigate between them
import LiveMic from "@/pages/LiveMic";
import Youtube from "@/pages/Youtube";
import FileTranscribe from "@/pages/FileTranscribe";
import Settings from "@/pages/Settings";

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
      <Switch>
        <Route path="/" component={LiveMic} />
        <Route path="/youtube" component={Youtube} />
        <Route path="/file" component={FileTranscribe} />
        <Route path="/settings" component={Settings} />
        <Suspense fallback={<PageLoader />}>
          <Route component={NotFound} />
        </Suspense>
      </Switch>
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

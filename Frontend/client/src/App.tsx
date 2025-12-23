import { Switch, Route, useLocation } from "wouter";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { Toaster } from "@/components/ui/toaster";
import { AppLayout } from "@/components/layout/AppLayout";
import { ThemeProvider } from "@/components/theme-provider";

import LiveMic from "@/pages/LiveMic";
import Youtube from "@/pages/Youtube";
import FileTranscribe from "@/pages/FileTranscribe";
import Settings from "@/pages/Settings";
import TranscriptDetail from "@/pages/TranscriptDetail";
import NotFound from "@/pages/not-found";

function TabRoutes() {
  const [location] = useLocation();
  return (
    <AppLayout path={location}>
      <Switch>
        <Route path="/" component={LiveMic} />
        <Route path="/youtube" component={Youtube} />
        <Route path="/file" component={FileTranscribe} />
        <Route path="/settings" component={Settings} />
        <Route component={NotFound} />
      </Switch>
    </AppLayout>
  );
}

function Router() {
  return (
    <Switch>
      <Route path="/transcript/:id" component={TranscriptDetail} />
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


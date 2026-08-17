import { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/inter/400.css";
import "@carrot-kpi/switzer-font/latin-400.css";
import "./index.css";
import "./interface-polish.css";
import { startFrontendLongTaskObserver } from "./lib/frontend-performance";
import { initializeLocaleCatalog, LocaleProvider } from "./i18n";
import { AppErrorBoundary } from "./components/AppErrorBoundary";

// Ignore ResizeObserver loop error - this is a known harmless error that occurs
// with Radix UI components when elements resize during animations
const resizeObserverErr = (e: ErrorEvent) => {
  if (e.message === "ResizeObserver loop completed with undelivered notifications.") {
    e.stopImmediatePropagation();
  }
};
window.addEventListener("error", resizeObserverErr);

// Keep type rendering crisp across the WebView2 shell and browser smoke runs.
document.documentElement.classList.add("antialiased");

const isOverlayWindow = typeof window !== "undefined" && window.location.search.includes("overlay=1");
const isTrayWindow = typeof window !== "undefined" && window.location.search.includes("tray=1");
const MainApp = lazy(() => import("./App"));

if (isOverlayWindow) {
  // Apply the transparent document surface before the lazy overlay bundle
  // mounts. This prevents the normal app background from becoming the first
  // committed WebView2 frame in the pre-created native overlay window.
  document.documentElement.dataset.scriberOverlayWindow = "true";
  document.body.dataset.scriberOverlayWindow = "true";
}

async function renderApplication(): Promise<void> {
  await initializeLocaleCatalog().catch((error) => {
    console.debug("Initial interface translation catalog could not be loaded.", error);
  });

  const root = createRoot(document.getElementById("root")!);
  if (isTrayWindow) {
    const { default: TrayPanel } = await import("./components/TrayPanel");
    root.render(
      <LocaleProvider>
        <TrayPanel />
      </LocaleProvider>,
    );
    return;
  }
  if (isOverlayWindow) {
    const { default: NativeRecordingOverlay } = await import("./components/NativeRecordingOverlay");
    root.render(
      <LocaleProvider>
        <NativeRecordingOverlay />
      </LocaleProvider>,
    );
    return;
  }

  const stopLongTaskObserver = startFrontendLongTaskObserver();
  window.addEventListener("beforeunload", stopLongTaskObserver, { once: true });
  root.render(
    <LocaleProvider>
      <AppErrorBoundary variant="root">
        <Suspense fallback={null}>
          <MainApp />
        </Suspense>
      </AppErrorBoundary>
    </LocaleProvider>,
  );
}

void renderApplication();

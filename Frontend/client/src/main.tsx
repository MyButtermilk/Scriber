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

if (isOverlayWindow) {
  // Apply the transparent document surface before the lazy overlay bundle
  // mounts. This prevents the normal app background from becoming the first
  // committed WebView2 frame in the pre-created native overlay window.
  document.documentElement.dataset.scriberOverlayWindow = "true";
  document.body.dataset.scriberOverlayWindow = "true";
}

async function renderApplication(): Promise<void> {
  const localeReady = initializeLocaleCatalog().catch((error) => {
    console.debug("Initial interface translation catalog could not be loaded.", error);
  });
  if (!isTrayWindow && !isOverlayWindow) {
    // Include module evaluation in startup diagnostics, as the lazy import did.
    const stopLongTaskObserver = startFrontendLongTaskObserver();
    window.addEventListener("beforeunload", stopLongTaskObserver, { once: true });
  }
  // Fetch and evaluate only this window's module while its locale loads. The
  // first React frame still waits for both, preserving localized startup.
  const windowModule = isTrayWindow
    ? import("./components/TrayPanel")
    : isOverlayWindow
      ? import("./components/NativeRecordingOverlay")
      : import("./App");
  // Observe an early module rejection immediately. MainApp's lazy boundary
  // below remains responsible for displaying the existing load-error UI.
  await Promise.all([localeReady, windowModule.catch(() => undefined)]);

  const root = createRoot(document.getElementById("root")!);
  if (isTrayWindow) {
    const { default: TrayPanel } = await windowModule;
    root.render(
      <LocaleProvider>
        <TrayPanel />
      </LocaleProvider>,
    );
    return;
  }
  if (isOverlayWindow) {
    const { default: NativeRecordingOverlay } = await windowModule;
    root.render(
      <LocaleProvider>
        <NativeRecordingOverlay />
      </LocaleProvider>,
    );
    return;
  }

  const MainApp = lazy(() => windowModule);
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

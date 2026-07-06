import { createRoot } from "react-dom/client";
import "./index.css";

// Ignore ResizeObserver loop error - this is a known harmless error that occurs
// with Radix UI components when elements resize during animations
const resizeObserverErr = (e: ErrorEvent) => {
    if (e.message === "ResizeObserver loop completed with undelivered notifications.") {
        e.stopImmediatePropagation();
    }
};
window.addEventListener("error", resizeObserverErr);

const root = createRoot(document.getElementById("root")!);
const isOverlayWindow =
    typeof window !== "undefined" && window.location.search.includes("overlay=1");
const isTrayWindow =
    typeof window !== "undefined" && window.location.search.includes("tray=1");

if (isTrayWindow) {
    void import("./components/TrayPanel").then(({ default: TrayPanel }) => {
        root.render(<TrayPanel />);
    });
} else if (isOverlayWindow) {
    void import("./components/NativeRecordingOverlay").then(({ default: NativeRecordingOverlay }) => {
        root.render(<NativeRecordingOverlay />);
    });
} else {
    void import("./App").then(({ default: App }) => {
        root.render(<App />);
    });
}

import "flag-icons/css/flag-icons.min.css";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// Ignore ResizeObserver loop error - this is a known harmless error that occurs
// with Radix UI components when elements resize during animations
const resizeObserverErr = (e: ErrorEvent) => {
    if (e.message === "ResizeObserver loop completed with undelivered notifications.") {
        e.stopImmediatePropagation();
    }
};
window.addEventListener("error", resizeObserverErr);

createRoot(document.getElementById("root")!).render(<App />);

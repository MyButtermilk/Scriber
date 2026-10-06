import { isTauriRuntime, loadBackendBaseUrlFromTauri } from "@/lib/backend";
import { withPromiseTimeout } from "@/lib/fetch-with-timeout";

let initialAccess: Promise<void> | null = null;
let settled = false;

export function isInitialBackendAccessReady(): boolean {
  return !isTauriRuntime() || settled;
}

// Start alongside the main/tray window module and reuse the same result at mount.
// This is only the initial access gate: health checks still refresh access on
// recovery and backend restarts through loadBackendBaseUrlFromTauri.
export function loadInitialBackendAccess(): Promise<void> {
  if (!isTauriRuntime()) return Promise.resolve();
  if (!initialAccess) {
    initialAccess = withPromiseTimeout(loadBackendBaseUrlFromTauri(), 5_000, "Initial Tauri backend lookup")
      .then(() => undefined)
      .catch((error) => {
        console.debug("Initial Tauri backend lookup failed; continuing with health fallback.", error);
      })
      .finally(() => {
        settled = true;
      });
  }
  return initialAccess;
}

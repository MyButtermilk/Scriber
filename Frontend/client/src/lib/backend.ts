declare global {
  interface Window {
    __SCRIBER_BACKEND_URL__?: string;
    __SCRIBER_SESSION_TOKEN__?: string;
  }
}

const configuredBase = (import.meta.env.VITE_BACKEND_URL as string | undefined)?.trim();
const runtimeBase = typeof window !== "undefined" ? window.__SCRIBER_BACKEND_URL__?.trim() : "";
const defaultBase = "http://127.0.0.1:8765";

export let backendBaseUrl = (runtimeBase || configuredBase || defaultBase).replace(/\/+$/, "");
export let backendSessionToken =
  (typeof window !== "undefined" ? window.__SCRIBER_SESSION_TOKEN__?.trim() : "") || "";

interface BackendAccess {
  baseUrl: string;
  sessionToken: string;
}

export interface AutostartStatus {
  enabled: boolean;
  available: boolean;
  message?: string;
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function setBackendBaseUrl(baseUrl: string): void {
  const normalized = (baseUrl || "").trim().replace(/\/+$/, "");
  if (normalized) {
    backendBaseUrl = normalized;
    if (typeof window !== "undefined") {
      window.__SCRIBER_BACKEND_URL__ = normalized;
    }
  }
}

export function setBackendSessionToken(sessionToken: string): void {
  const normalized = (sessionToken || "").trim();
  backendSessionToken = normalized;
  if (typeof window !== "undefined") {
    window.__SCRIBER_SESSION_TOKEN__ = normalized;
  }
}

function appendSessionToken(url: string): string {
  if (!backendSessionToken || typeof window === "undefined") {
    return url;
  }
  try {
    const parsed = new URL(url, backendBaseUrl || window.location.origin);
    const backend = new URL(backendBaseUrl || window.location.origin);
    const targetsBackend = parsed.hostname === backend.hostname && parsed.port === backend.port;
    if (targetsBackend && (parsed.pathname === "/ws" || parsed.pathname.startsWith("/api/"))) {
      parsed.searchParams.set("scriberToken", backendSessionToken);
      return parsed.toString();
    }
  } catch {
    return url;
  }
  return url;
}

export async function loadBackendBaseUrlFromTauri(): Promise<string> {
  if (!isTauriRuntime()) {
    return backendBaseUrl;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const access = await invoke<BackendAccess>("get_backend_access");
    setBackendBaseUrl(access.baseUrl);
    setBackendSessionToken(access.sessionToken);
  } catch (error) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const baseUrl = await invoke<string>("get_backend_base_url");
      setBackendBaseUrl(baseUrl);
    } catch {
      console.debug("Tauri backend URL lookup failed; using configured backend URL.", error);
    }
  }
  return backendBaseUrl;
}

export function apiUrl(pathOrUrl: string): string {
  if (!pathOrUrl) return pathOrUrl;
  if (/^https?:\/\//i.test(pathOrUrl)) return appendSessionToken(pathOrUrl);
  if (!backendBaseUrl) return pathOrUrl;
  const url = pathOrUrl.startsWith("/")
    ? `${backendBaseUrl}${pathOrUrl}`
    : `${backendBaseUrl}/${pathOrUrl}`;
  return appendSessionToken(url);
}

export async function getAutostartStatus(): Promise<AutostartStatus> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<AutostartStatus>("get_desktop_autostart");
  }

  const res = await fetch(apiUrl("/api/autostart"), { credentials: "include" });
  if (!res.ok) {
    throw new Error(await responseMessage(res, "Failed to load autostart status"));
  }
  return res.json();
}

export async function setAutostartEnabled(enabled: boolean): Promise<AutostartStatus> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<AutostartStatus>("set_desktop_autostart", { enabled });
  }

  const res = await fetch(apiUrl("/api/autostart"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(await responseMessage(res, "Failed to update autostart"));
  }
  return res.json();
}

export async function refreshGlobalHotkey(): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("refresh_global_hotkey");
}

export function wsUrl(path: string): string {
  const base = backendBaseUrl || window.location.origin;
  const wsBase = base.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
  const url = path.startsWith("/") ? `${wsBase}${path}` : `${wsBase}/${path}`;
  return appendSessionToken(url);
}

async function responseMessage(res: Response, fallback: string): Promise<string> {
  const textFallback = res.clone();
  try {
    const payload = await res.json();
    if (payload?.message) {
      return String(payload.message);
    }
  } catch {
    try {
      const text = await textFallback.text();
      if (text.trim()) {
        return text.trim();
      }
    } catch {
      // Ignore and return fallback.
    }
  }
  return fallback;
}

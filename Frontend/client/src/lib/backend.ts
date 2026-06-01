declare global {
  interface Window {
    __SCRIBER_BACKEND_URL__?: string;
  }
}

const configuredBase = (import.meta.env.VITE_BACKEND_URL as string | undefined)?.trim();
const runtimeBase = typeof window !== "undefined" ? window.__SCRIBER_BACKEND_URL__?.trim() : "";
const defaultBase = "http://127.0.0.1:8765";

export let backendBaseUrl = (runtimeBase || configuredBase || defaultBase).replace(/\/+$/, "");

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

export async function loadBackendBaseUrlFromTauri(): Promise<string> {
  if (!isTauriRuntime()) {
    return backendBaseUrl;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const baseUrl = await invoke<string>("get_backend_base_url");
    setBackendBaseUrl(baseUrl);
  } catch (error) {
    console.debug("Tauri backend URL lookup failed; using configured backend URL.", error);
  }
  return backendBaseUrl;
}

export function apiUrl(pathOrUrl: string): string {
  if (!pathOrUrl) return pathOrUrl;
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  if (!backendBaseUrl) return pathOrUrl;
  return pathOrUrl.startsWith("/")
    ? `${backendBaseUrl}${pathOrUrl}`
    : `${backendBaseUrl}/${pathOrUrl}`;
}

export function wsUrl(path: string): string {
  const base = backendBaseUrl || window.location.origin;
  const wsBase = base.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
  return path.startsWith("/") ? `${wsBase}${path}` : `${wsBase}/${path}`;
}

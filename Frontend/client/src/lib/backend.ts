import {
  REST_API_VERSION,
  type AutostartStatus,
  type FrontendReadyRequest,
  type FrontendReadyResponse,
} from "@/lib/api-types";
import { responseDetailMessage } from "@/lib/request-errors";

declare global {
  interface Window {
    __SCRIBER_BACKEND_URL__?: string;
    __SCRIBER_SESSION_TOKEN__?: string;
    __SCRIBER_BACKEND_SESSION_TOKEN_REQUIRED__?: boolean;
  }
}

const configuredBase = (import.meta.env.VITE_BACKEND_URL as string | undefined)?.trim();
const runtimeBase = typeof window !== "undefined" ? window.__SCRIBER_BACKEND_URL__?.trim() : "";
const defaultBase = "http://127.0.0.1:8765";

export let backendBaseUrl = (runtimeBase || configuredBase || defaultBase).replace(/\/+$/, "");
export let backendSessionToken =
  (typeof window !== "undefined" ? window.__SCRIBER_SESSION_TOKEN__?.trim() : "") || "";
let backendSessionTokenRequired =
  (typeof window !== "undefined" ? window.__SCRIBER_BACKEND_SESSION_TOKEN_REQUIRED__ === true : false);
let frontendReadyReportKey = "";
export const BACKEND_SESSION_TOKEN_REQUIRED_EVENT = "scriber-backend-session-token-required-change";

interface BackendAccess {
  baseUrl: string;
  sessionToken: string;
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
    window.dispatchEvent(new CustomEvent(BACKEND_SESSION_TOKEN_REQUIRED_EVENT));
  }
}

export function setBackendSessionTokenRequired(required: boolean): void {
  const changed = backendSessionTokenRequired !== required;
  backendSessionTokenRequired = required;
  if (typeof window !== "undefined") {
    window.__SCRIBER_BACKEND_SESSION_TOKEN_REQUIRED__ = required;
    if (changed) {
      window.dispatchEvent(new CustomEvent(BACKEND_SESSION_TOKEN_REQUIRED_EVENT, { detail: { required } }));
    }
  }
}

export function isBackendSessionTokenRequired(): boolean {
  return backendSessionTokenRequired;
}

function appendSessionToken(url: string): string {
  if (!backendSessionToken || typeof window === "undefined") {
    return url;
  }
  try {
    const parsed = new URL(url, backendBaseUrl || window.location.origin);
    const backend = new URL(backendBaseUrl || window.location.origin);
    // URL.port is "" for default ports, so normalize before comparing
    // (e.g. "http://host" must match "http://host:80").
    const effectivePort = (u: URL) =>
      u.port || (u.protocol === "https:" || u.protocol === "wss:" ? "443" : "80");
    const targetsBackend =
      parsed.hostname === backend.hostname && effectivePort(parsed) === effectivePort(backend);
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

export async function reportFrontendReady(): Promise<void> {
  if (typeof window === "undefined") {
    return;
  }

  const reportKey = `${backendBaseUrl}|${backendSessionToken ? "token" : "no-token"}|${isTauriRuntime() ? "tauri" : "browser"}`;
  if (frontendReadyReportKey === reportKey) {
    return;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);
  try {
    const payload: FrontendReadyRequest = {
      apiVersion: REST_API_VERSION,
      tauriRuntime: isTauriRuntime(),
      backendBaseUrl,
      locationOrigin: window.location.origin,
      path: `${window.location.pathname}${window.location.hash || ""}`,
    };
    const res = await fetch(apiUrl("/api/runtime/frontend-ready"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify(payload),
    });
    const response = res.ok ? ((await res.json()) as FrontendReadyResponse) : null;
    if (response?.apiVersion === REST_API_VERSION && response.ready) {
      frontendReadyReportKey = reportKey;
    }
  } finally {
    clearTimeout(timeoutId);
  }
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
  return (await res.json()) as AutostartStatus;
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
  return (await res.json()) as AutostartStatus;
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
  return (await responseDetailMessage(res)) || fallback;
}

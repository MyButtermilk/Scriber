import {
  REST_API_VERSION,
  type AutostartStatus,
  type FrontendReadyRequest,
  type FrontendReadyResponse,
} from "@/lib/api-types";
import { responseDetailMessage } from "@/lib/request-errors";
import { fetchWithTimeout, withPromiseTimeout } from "@/lib/fetch-with-timeout";

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
let backendAccessLoadInFlight: Promise<string> | null = null;
let backendAccessRetryAfterMs = 0;
let desktopAutostartLoadInFlight: Promise<AutostartStatus> | null = null;
export const BACKEND_SESSION_TOKEN_REQUIRED_EVENT = "scriber-backend-session-token-required-change";

interface BackendAccess {
  baseUrl: string;
  sessionToken: string;
}

export interface TrayStatus {
  recordingActive: boolean;
  recordingMode: string;
  updateAvailable: boolean;
  updateInstalling: boolean;
  updateVersion?: string;
  updateMessage: string;
}

export interface TrayUpdateStatusPayload {
  available: boolean;
  installing: boolean;
  version?: string;
  message?: string;
}

export interface DesktopHotkeyStatus {
  registered: boolean;
  available: boolean;
  hotkey: string;
  postProcessingHotkey?: string;
  mode: string;
  message: string;
  captureSuspended: boolean;
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

export function loadBackendBaseUrlFromTauri(): Promise<string> {
  if (!isTauriRuntime()) {
    return Promise.resolve(backendBaseUrl);
  }
  if (Date.now() < backendAccessRetryAfterMs) {
    return Promise.resolve(backendBaseUrl);
  }
  if (backendAccessLoadInFlight) {
    return backendAccessLoadInFlight;
  }
  const request = loadBackendAccessFromTauri().finally(() => {
    if (backendAccessLoadInFlight === request) {
      backendAccessLoadInFlight = null;
    }
  });
  backendAccessLoadInFlight = request;
  return request;
}

async function loadBackendAccessFromTauri(): Promise<string> {
  let loaded = false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const access = await withPromiseTimeout(
      invoke<BackendAccess>("get_backend_access"),
      2_000,
      "Tauri backend access",
    );
    setBackendBaseUrl(access.baseUrl);
    setBackendSessionToken(access.sessionToken);
    loaded = true;
  } catch (error) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const baseUrl = await withPromiseTimeout(
        invoke<string>("get_backend_base_url"),
        2_000,
        "Tauri backend URL fallback",
      );
      setBackendBaseUrl(baseUrl);
      loaded = true;
    } catch {
      console.debug("Tauri backend URL lookup failed; using configured backend URL.", error);
    }
  }
  backendAccessRetryAfterMs = loaded ? 0 : Date.now() + 60_000;
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

export async function reportFrontendReady(options: { force?: boolean } = {}): Promise<void> {
  if (typeof window === "undefined") {
    return;
  }

  const reportKey = `${backendBaseUrl}|${backendSessionToken ? "token" : "no-token"}|${isTauriRuntime() ? "tauri" : "browser"}`;
  if (!options.force && frontendReadyReportKey === reportKey) {
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
    if (!desktopAutostartLoadInFlight) {
      const { invoke } = await import("@tauri-apps/api/core");
      const request = invoke<AutostartStatus>("get_desktop_autostart");
      desktopAutostartLoadInFlight = request;
      request.then(
        () => {
          if (desktopAutostartLoadInFlight === request) desktopAutostartLoadInFlight = null;
        },
        () => {
          if (desktopAutostartLoadInFlight === request) desktopAutostartLoadInFlight = null;
        },
      );
    }
    return withPromiseTimeout(desktopAutostartLoadInFlight, 5_000, "Autostart status lookup");
  }

  const res = await fetchWithTimeout(
    apiUrl("/api/autostart"),
    { credentials: "include" },
    5_000,
  );
  if (!res.ok) {
    throw new Error(await responseMessage(res, "Failed to load autostart status"));
  }
  return (await res.json()) as AutostartStatus;
}

export async function setAutostartEnabled(enabled: boolean): Promise<AutostartStatus> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    return withPromiseTimeout(
      invoke<AutostartStatus>("set_desktop_autostart", { enabled }),
      5_000,
      "Autostart update",
    );
  }

  const res = await fetchWithTimeout(apiUrl("/api/autostart"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
    credentials: "include",
  }, 5_000);
  if (!res.ok) {
    throw new Error(await responseMessage(res, "Failed to update autostart"));
  }
  return (await res.json()) as AutostartStatus;
}

export async function refreshGlobalHotkey(): Promise<DesktopHotkeyStatus | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return withPromiseTimeout(
    invoke<DesktopHotkeyStatus>("refresh_global_hotkey"),
    10_000,
    "Global hotkey refresh",
  );
}

export async function setGlobalHotkeyCaptureActive(active: boolean): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  await withPromiseTimeout(
    invoke("set_global_hotkey_capture_active", { active }),
    5_000,
    "Global hotkey capture update",
  );
}

export async function getGlobalHotkeyStatus(): Promise<DesktopHotkeyStatus | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return withPromiseTimeout(
    invoke<DesktopHotkeyStatus>("global_hotkey_status"),
    5_000,
    "Global hotkey status",
  );
}

export async function getTrayStatus(): Promise<TrayStatus | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return withPromiseTimeout(invoke<TrayStatus>("tray_status"), 5_000, "Tray status");
}

export async function setTrayUpdateStatus(status: TrayUpdateStatusPayload): Promise<TrayStatus | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return withPromiseTimeout(
    invoke<TrayStatus>("set_tray_update_status", { status }),
    5_000,
    "Tray update status",
  );
}

export async function setTrayRecordingState(active: boolean, mode?: string): Promise<TrayStatus | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return withPromiseTimeout(
    invoke<TrayStatus>("set_tray_recording_state", { active, mode }),
    5_000,
    "Tray recording state",
  );
}

export async function trayAction(action: string): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  await withPromiseTimeout(invoke("tray_action", { action }), 10_000, "Tray action");
}

export async function hideTrayPanel(): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  const { invoke } = await import("@tauri-apps/api/core");
  await withPromiseTimeout(invoke("hide_tray_panel"), 5_000, "Hide tray panel");
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

import { isTauriRuntime, setTrayUpdateStatus } from "@/lib/backend";
import { withPromiseTimeout } from "@/lib/fetch-with-timeout";

declare const __SCRIBER_APP_VERSION__: string | undefined;

export type DesktopUpdatePhase =
  | "idle"
  | "checking"
  | "current"
  | "available"
  | "installing"
  | "unavailable"
  | "error";

export interface DesktopUpdateStatus {
  phase: DesktopUpdatePhase;
  enabled: boolean;
  available: boolean;
  autoCheckEnabled: boolean;
  currentVersion?: string;
  version?: string;
  date?: string;
  notes?: string;
  lastCheckedAt?: string;
  nextCheckAt?: string;
  releaseNotesUrl: string;
  dismissed: boolean;
  deferred: boolean;
  deferredUntil?: string;
  message: string;
}

export interface DesktopUpdateProgress {
  downloadedBytes: number;
  totalBytes?: number;
  percent?: number;
  message: string;
}

export interface DesktopUpdateSettings {
  autoCheckEnabled: boolean;
  intervalHours: number;
}

export interface DesktopUpdateCheckResult {
  checked: boolean;
  reason: "checked" | "not-tauri" | "auto-disabled" | "not-due" | "busy";
  status: DesktopUpdateStatus;
}

type DesktopUpdateCheckOptions = {
  force?: boolean;
  isBusy?: boolean;
};

type DownloadEvent = {
  event?: string;
  data?: {
    contentLength?: number;
    chunkLength?: number;
  };
};

type DesktopUpdateCache = {
  phase?: DesktopUpdatePhase;
  enabled?: boolean;
  currentVersion?: string;
  version?: string;
  date?: string;
  notes?: string;
  lastCheckedAt?: string;
  message?: string;
  dismissedVersion?: string;
  deferredVersion?: string;
  deferredUntil?: string;
};

const CACHE_KEY = "scriber:desktop-update-cache:v1";
const SETTINGS_KEY = "scriber:desktop-update-settings:v1";
const STATUS_EVENT = "scriber:desktop-update-status";
const DEFAULT_CHECK_INTERVAL_HOURS = 24 * 7;
const MIN_CHECK_INTERVAL_HOURS = 24;
const REMIND_LATER_HOURS = 24;
const UPDATE_CHECK_TIMEOUT_MS = 30_000;
const APP_VERSION_TIMEOUT_MS = 5_000;

let updateCheckInFlight: Promise<DesktopUpdateStatus> | null = null;
let updateInstallInFlight: Promise<DesktopUpdateStatus> | null = null;

export const DESKTOP_UPDATE_RELEASE_NOTES_URL = "https://github.com/MyButtermilk/Scriber/releases/latest";

const DEFAULT_SETTINGS: DesktopUpdateSettings = {
  autoCheckEnabled: true,
  intervalHours: DEFAULT_CHECK_INTERVAL_HOURS,
};

const NOT_CHECKED_STATUS: DesktopUpdateStatus = {
  phase: "idle",
  enabled: false,
  available: false,
  autoCheckEnabled: DEFAULT_SETTINGS.autoCheckEnabled,
  releaseNotesUrl: DESKTOP_UPDATE_RELEASE_NOTES_URL,
  dismissed: false,
  deferred: false,
  message: "Updates have not been checked yet.",
};

export function initialDesktopUpdateStatus(): DesktopUpdateStatus {
  return getCachedDesktopUpdateStatus();
}

export function getCachedDesktopUpdateStatus(): DesktopUpdateStatus {
  return statusFromCache(readCache(), readDesktopUpdateSettings());
}

export function readDesktopUpdateSettings(): DesktopUpdateSettings {
  const value = readJson<Partial<DesktopUpdateSettings>>(SETTINGS_KEY);
  return normalizeSettings(value);
}

export function updateDesktopUpdateSettings(update: Partial<DesktopUpdateSettings>): DesktopUpdateStatus {
  const settings = normalizeSettings({ ...readDesktopUpdateSettings(), ...update });
  writeJson(SETTINGS_KEY, settings);
  const status = getCachedDesktopUpdateStatus();
  emitStatus(status);
  return status;
}

export function subscribeDesktopUpdateStatus(listener: (status: DesktopUpdateStatus) => void): () => void {
  if (typeof window === "undefined") {
    return () => undefined;
  }
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<DesktopUpdateStatus>).detail;
    if (detail) {
      listener(detail);
    }
  };
  window.addEventListener(STATUS_EVENT, handler);
  return () => window.removeEventListener(STATUS_EVENT, handler);
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateStatus> {
  const status = await sharedDesktopUpdateCheck();
  emitStatus(status);
  return status;
}

export async function checkDesktopUpdateIfDue(
  options: DesktopUpdateCheckOptions = {},
): Promise<DesktopUpdateCheckResult> {
  const settings = readDesktopUpdateSettings();
  const cached = getCachedDesktopUpdateStatus();

  if (!isTauriRuntime()) {
    return { checked: false, reason: "not-tauri", status: cached };
  }

  if (!settings.autoCheckEnabled && !options.force) {
    return { checked: false, reason: "auto-disabled", status: cached };
  }

  if (options.isBusy && !options.force) {
    return { checked: false, reason: "busy", status: cached };
  }

  if (!options.force && cached.lastCheckedAt && Date.now() < nextCheckTime(cached.lastCheckedAt, settings).getTime()) {
    return { checked: false, reason: "not-due", status: cached };
  }

  const status = await sharedDesktopUpdateCheck();
  emitStatus(status);
  return { checked: true, reason: "checked", status };
}

export function shouldNotifyDesktopUpdate(status: DesktopUpdateStatus): boolean {
  return Boolean(
    status.autoCheckEnabled &&
    status.enabled &&
    status.available &&
    status.version &&
    isVersionNewerThanCurrent(status.version, status.currentVersion) &&
    !status.dismissed &&
    !status.deferred,
  );
}

export function publishDesktopUpdateStatusToTray(status: DesktopUpdateStatus): void {
  if (!isTauriRuntime()) {
    return;
  }
  const actionable = Boolean(
    status.enabled &&
    status.available &&
    status.version &&
    isVersionNewerThanCurrent(status.version, status.currentVersion) &&
    !status.dismissed &&
    !status.deferred,
  );
  void setTrayUpdateStatus({
    available: actionable,
    installing: status.phase === "installing",
    version: actionable ? status.version : undefined,
    message: status.message,
  }).catch((error) => console.debug("Tray update status sync failed.", error));
}

export function skipDesktopUpdateVersion(version?: string): DesktopUpdateStatus {
  const cache = readCache();
  const targetVersion = version || cache?.version;
  if (!targetVersion) {
    return getCachedDesktopUpdateStatus();
  }
  const nextCache: DesktopUpdateCache = {
    ...(cache || {}),
    dismissedVersion: targetVersion,
    deferredVersion: undefined,
    deferredUntil: undefined,
  };
  writeCache(nextCache);
  const status = getCachedDesktopUpdateStatus();
  emitStatus(status);
  return status;
}

export function remindDesktopUpdateLater(version?: string): DesktopUpdateStatus {
  const cache = readCache();
  const targetVersion = version || cache?.version;
  if (!targetVersion) {
    return getCachedDesktopUpdateStatus();
  }
  const deferredUntil = new Date(Date.now() + REMIND_LATER_HOURS * 60 * 60 * 1000).toISOString();
  const nextCache: DesktopUpdateCache = {
    ...(cache || {}),
    deferredVersion: targetVersion,
    deferredUntil,
  };
  writeCache(nextCache);
  const status = getCachedDesktopUpdateStatus();
  emitStatus(status);
  return status;
}

export async function openDesktopUpdateReleaseNotes(): Promise<void> {
  if (isTauriRuntime()) {
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(DESKTOP_UPDATE_RELEASE_NOTES_URL);
      return;
    } catch (error) {
      console.warn("Tauri opener failed; falling back to browser window.open.", error);
    }
  }
  window.open(DESKTOP_UPDATE_RELEASE_NOTES_URL, "_blank", "noopener,noreferrer");
}

export function installDesktopUpdate(
  onProgress?: (progress: DesktopUpdateProgress) => void,
): Promise<DesktopUpdateStatus> {
  if (updateInstallInFlight) {
    return updateInstallInFlight;
  }
  const request = performDesktopUpdateInstall(onProgress);
  updateInstallInFlight = request;
  request.then(
    () => {
      if (updateInstallInFlight === request) updateInstallInFlight = null;
    },
    () => {
      if (updateInstallInFlight === request) updateInstallInFlight = null;
    },
  );
  return request;
}

async function performDesktopUpdateInstall(
  onProgress?: (progress: DesktopUpdateProgress) => void,
): Promise<DesktopUpdateStatus> {
  if (updateCheckInFlight) {
    await updateCheckInFlight;
  }
  const currentVersion = await getCurrentVersion();
  if (!isTauriRuntime()) {
    throw new Error("Desktop updates are available in the installed Windows app.");
  }

  const { check } = await import("@tauri-apps/plugin-updater");
  const { relaunch } = await import("@tauri-apps/plugin-process");
  const update = await withPromiseTimeout(
    check(),
    UPDATE_CHECK_TIMEOUT_MS,
    "Desktop update check",
  );
  if (!update) {
    const status = cacheAndBuildStatus({
      phase: "current",
      enabled: true,
      available: false,
      currentVersion,
      message: "Scriber is up to date.",
    });
    emitStatus(status);
    return status;
  }

  if (!isVersionNewerThanCurrent(update.version, currentVersion)) {
    const status = cacheAndBuildStatus({
      phase: "current",
      enabled: true,
      available: false,
      currentVersion,
      message: "Scriber is up to date.",
    });
    emitStatus(status);
    return status;
  }

  let downloadedBytes = 0;
  let totalBytes: number | undefined;
  await update.downloadAndInstall((event: DownloadEvent) => {
    if (event.event === "Started") {
      downloadedBytes = 0;
      totalBytes = event.data?.contentLength;
      onProgress?.({
        downloadedBytes,
        totalBytes,
        percent: 0,
        message: "Download started.",
      });
      return;
    }

    if (event.event === "Progress") {
      downloadedBytes += event.data?.chunkLength || 0;
      onProgress?.({
        downloadedBytes,
        totalBytes,
        percent: totalBytes ? Math.min(100, Math.round((downloadedBytes / totalBytes) * 100)) : undefined,
        message: "Downloading update.",
      });
      return;
    }

    if (event.event === "Finished") {
      onProgress?.({
        downloadedBytes,
        totalBytes,
        percent: 100,
        message: "Download finished.",
      });
    }
  });

  const status = cacheAndBuildStatus({
    phase: "installing",
    enabled: true,
    available: true,
    currentVersion,
    version: update.version,
    date: update.date,
    notes: update.body,
    message: "Update installed. Scriber is restarting.",
  });
  emitStatus(status);
  await relaunch();
  return status;
}

function sharedDesktopUpdateCheck(): Promise<DesktopUpdateStatus> {
  if (updateInstallInFlight) {
    return updateInstallInFlight;
  }
  if (updateCheckInFlight) {
    return updateCheckInFlight;
  }
  const request = performDesktopUpdateCheck();
  updateCheckInFlight = request;
  request.then(
    () => {
      if (updateCheckInFlight === request) updateCheckInFlight = null;
    },
    () => {
      if (updateCheckInFlight === request) updateCheckInFlight = null;
    },
  );
  return request;
}

async function performDesktopUpdateCheck(): Promise<DesktopUpdateStatus> {
  const currentVersion = await getCurrentVersion();
  if (!isTauriRuntime()) {
    return cacheAndBuildStatus({
      phase: "unavailable",
      enabled: false,
      available: false,
      currentVersion,
      message: "Desktop updates are available in the installed Windows app.",
    });
  }

  try {
    const { check } = await import("@tauri-apps/plugin-updater");
    const update = await withPromiseTimeout(
      check(),
      UPDATE_CHECK_TIMEOUT_MS,
      "Desktop update check",
    );
    if (!update) {
      return cacheAndBuildStatus({
        phase: "current",
        enabled: true,
        available: false,
        currentVersion,
        message: "Scriber is up to date.",
      });
    }

    if (!isVersionNewerThanCurrent(update.version, currentVersion)) {
      return cacheAndBuildStatus({
        phase: "current",
        enabled: true,
        available: false,
        currentVersion,
        message: "Scriber is up to date.",
      });
    }

    return cacheAndBuildStatus({
      phase: "available",
      enabled: true,
      available: true,
      currentVersion,
      version: update.version,
      date: update.date,
      notes: update.body,
      message: `Scriber ${update.version} is available.`,
    });
  } catch (error) {
    return cacheAndBuildStatus({
      phase: "error",
      enabled: !isUpdaterConfigurationError(error),
      available: false,
      currentVersion,
      message: friendlyUpdaterError(error),
    });
  }
}

async function getCurrentVersion(): Promise<string> {
  if (!isTauriRuntime()) {
    return buildVersion();
  }
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    return (
      await withPromiseTimeout(
        getVersion(),
        APP_VERSION_TIMEOUT_MS,
        "App version lookup",
      )
    ) || buildVersion();
  } catch {
    return buildVersion();
  }
}

function cacheAndBuildStatus(input: {
  phase: DesktopUpdatePhase;
  enabled: boolean;
  available: boolean;
  currentVersion?: string;
  version?: string;
  date?: string;
  notes?: string;
  message: string;
}): DesktopUpdateStatus {
  const previous = readCache();
  const inputAvailable = Boolean(input.available && isVersionNewerThanCurrent(input.version, input.currentVersion));
  const sameVersion = Boolean(input.version && input.version === previous?.version);
  const cache: DesktopUpdateCache = {
    phase: inputAvailable ? input.phase : input.phase === "available" ? "current" : input.phase,
    enabled: input.enabled,
    currentVersion: input.currentVersion,
    version: inputAvailable ? input.version : undefined,
    date: inputAvailable ? input.date : undefined,
    notes: inputAvailable ? input.notes : undefined,
    lastCheckedAt: new Date().toISOString(),
    message: inputAvailable || input.phase !== "available" ? input.message : "Scriber is up to date.",
    dismissedVersion: sameVersion ? previous?.dismissedVersion : undefined,
    deferredVersion: sameVersion ? previous?.deferredVersion : undefined,
    deferredUntil: sameVersion ? previous?.deferredUntil : undefined,
  };
  writeCache(cache);
  return statusFromCache(cache, readDesktopUpdateSettings());
}

function statusFromCache(cache: DesktopUpdateCache | null, settings: DesktopUpdateSettings): DesktopUpdateStatus {
  if (!cache) {
    return {
      ...NOT_CHECKED_STATUS,
      autoCheckEnabled: settings.autoCheckEnabled,
      nextCheckAt: nextCheckTime(undefined, settings).toISOString(),
    };
  }

  const currentVersion = latestKnownCurrentVersion(cache.currentVersion);
  const rawAvailable = cache.phase === "available" && Boolean(cache.version);
  const available = Boolean(rawAvailable && isVersionNewerThanCurrent(cache.version, currentVersion));
  const staleAvailable = Boolean(rawAvailable && !available);
  const dismissed = Boolean(available && cache.dismissedVersion === cache.version);
  const deferred = Boolean(
    available &&
    cache.deferredVersion === cache.version &&
    cache.deferredUntil &&
    Date.parse(cache.deferredUntil) > Date.now(),
  );
  return {
    phase: staleAvailable ? "current" : cache.phase || "idle",
    enabled: Boolean(cache.enabled),
    available,
    autoCheckEnabled: settings.autoCheckEnabled,
    currentVersion,
    version: available ? cache.version : undefined,
    date: available ? cache.date : undefined,
    notes: available ? cache.notes : undefined,
    lastCheckedAt: cache.lastCheckedAt,
    nextCheckAt: nextCheckTime(cache.lastCheckedAt, settings).toISOString(),
    releaseNotesUrl: DESKTOP_UPDATE_RELEASE_NOTES_URL,
    dismissed,
    deferred,
    deferredUntil: cache.deferredUntil,
    message: staleAvailable ? "Scriber is up to date." : cache.message || NOT_CHECKED_STATUS.message,
  };
}

function buildVersion(): string {
  return typeof __SCRIBER_APP_VERSION__ === "string" ? __SCRIBER_APP_VERSION__ : "";
}

function latestKnownCurrentVersion(cachedCurrentVersion: string | undefined): string {
  const bundledVersion = buildVersion();
  return bundledVersion || cachedCurrentVersion || "";
}

function isVersionNewerThanCurrent(candidateVersion: string | undefined, currentVersion: string | undefined): boolean {
  const candidate = parseVersion(candidateVersion);
  const current = parseVersion(currentVersion || buildVersion());
  if (!candidate || !current) {
    return Boolean(candidateVersion);
  }
  for (let index = 0; index < Math.max(candidate.length, current.length); index += 1) {
    const left = candidate[index] || 0;
    const right = current[index] || 0;
    if (left > right) {
      return true;
    }
    if (left < right) {
      return false;
    }
  }
  return false;
}

function parseVersion(value: string | undefined): number[] | null {
  const normalized = (value || "")
    .trim()
    .replace(/^v/i, "")
    .split(/[+-]/, 1)[0];
  if (!normalized) {
    return null;
  }
  const parts = normalized.split(".");
  if (!parts.length || parts.some((part) => !/^\d+$/.test(part))) {
    return null;
  }
  return parts.map((part) => Number.parseInt(part, 10));
}

function nextCheckTime(lastCheckedAt: string | undefined, settings: DesktopUpdateSettings): Date {
  if (!lastCheckedAt) {
    return new Date(0);
  }
  const parsed = Date.parse(lastCheckedAt);
  if (!Number.isFinite(parsed)) {
    return new Date(0);
  }
  return new Date(parsed + settings.intervalHours * 60 * 60 * 1000);
}

function normalizeSettings(value: Partial<DesktopUpdateSettings> | null): DesktopUpdateSettings {
  const intervalHours =
    typeof value?.intervalHours === "number" && Number.isFinite(value.intervalHours)
      ? Math.max(MIN_CHECK_INTERVAL_HOURS, Math.round(value.intervalHours))
      : DEFAULT_SETTINGS.intervalHours;
  return {
    autoCheckEnabled: typeof value?.autoCheckEnabled === "boolean" ? value.autoCheckEnabled : true,
    intervalHours,
  };
}

function readCache(): DesktopUpdateCache | null {
  return readJson<DesktopUpdateCache>(CACHE_KEY);
}

function writeCache(cache: DesktopUpdateCache): void {
  writeJson(CACHE_KEY, cache);
}

function readJson<T>(key: string): T | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return null;
    }
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Ignore storage failures. Manual update checks still work without cache.
  }
}

function emitStatus(status: DesktopUpdateStatus): void {
  publishDesktopUpdateStatusToTray(status);
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new CustomEvent<DesktopUpdateStatus>(STATUS_EVENT, { detail: status }));
}

function isUpdaterConfigurationError(error: unknown): boolean {
  const raw = error instanceof Error ? error.message : String(error || "");
  const message = raw.toLowerCase();
  return (
    message.includes("pubkey") ||
    message.includes("public key") ||
    message.includes("signature") ||
    message.includes("endpoint") ||
    message.includes("configuration") ||
    message.includes("configured")
  );
}

function friendlyUpdaterError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (isUpdaterConfigurationError(error)) {
    return "Desktop updater is not configured for this build. Configure the Tauri updater public key, endpoint, and signing key before enabling release updates.";
  }
  if (raw.trim()) {
    return raw;
  }
  return "Update check failed.";
}

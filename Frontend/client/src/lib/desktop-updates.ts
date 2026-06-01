import { isTauriRuntime } from "@/lib/backend";

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
  currentVersion?: string;
  version?: string;
  date?: string;
  notes?: string;
  message: string;
}

export interface DesktopUpdateProgress {
  downloadedBytes: number;
  totalBytes?: number;
  percent?: number;
  message: string;
}

type DownloadEvent = {
  event?: string;
  data?: {
    contentLength?: number;
    chunkLength?: number;
  };
};

const NOT_CHECKED_STATUS: DesktopUpdateStatus = {
  phase: "idle",
  enabled: false,
  available: false,
  message: "Updates have not been checked yet.",
};

export function initialDesktopUpdateStatus(): DesktopUpdateStatus {
  return { ...NOT_CHECKED_STATUS };
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateStatus> {
  const currentVersion = await getCurrentVersion();
  if (!isTauriRuntime()) {
    return {
      phase: "unavailable",
      enabled: false,
      available: false,
      currentVersion,
      message: "Desktop updates are available in the installed Windows app.",
    };
  }

  try {
    const { check } = await import("@tauri-apps/plugin-updater");
    const update = await check();
    if (!update) {
      return {
        phase: "current",
        enabled: true,
        available: false,
        currentVersion,
        message: "Scriber is up to date.",
      };
    }

    return {
      phase: "available",
      enabled: true,
      available: true,
      currentVersion,
      version: update.version,
      date: update.date,
      notes: update.body,
      message: `Scriber ${update.version} is available.`,
    };
  } catch (error) {
    return {
      phase: "error",
      enabled: false,
      available: false,
      currentVersion,
      message: friendlyUpdaterError(error),
    };
  }
}

export async function installDesktopUpdate(
  onProgress?: (progress: DesktopUpdateProgress) => void,
): Promise<DesktopUpdateStatus> {
  const currentVersion = await getCurrentVersion();
  if (!isTauriRuntime()) {
    throw new Error("Desktop updates are available in the installed Windows app.");
  }

  const { check } = await import("@tauri-apps/plugin-updater");
  const { relaunch } = await import("@tauri-apps/plugin-process");
  const update = await check();
  if (!update) {
    return {
      phase: "current",
      enabled: true,
      available: false,
      currentVersion,
      message: "Scriber is up to date.",
    };
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

  await relaunch();
  return {
    phase: "installing",
    enabled: true,
    available: true,
    currentVersion,
    version: update.version,
    date: update.date,
    notes: update.body,
    message: "Update installed. Scriber is restarting.",
  };
}

async function getCurrentVersion(): Promise<string> {
  if (!isTauriRuntime()) {
    return "";
  }
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    return await getVersion();
  } catch {
    return "";
  }
}

function friendlyUpdaterError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  const message = raw.toLowerCase();
  if (
    message.includes("pubkey") ||
    message.includes("public key") ||
    message.includes("signature") ||
    message.includes("endpoint") ||
    message.includes("configuration") ||
    message.includes("configured")
  ) {
    return "Desktop updater is not configured for this build. Configure the Tauri updater public key, endpoint, and signing key before enabling release updates.";
  }
  if (raw.trim()) {
    return raw;
  }
  return "Update check failed.";
}

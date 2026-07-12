import { invoke } from "@tauri-apps/api/core";
import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  meetingExportApiPath,
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
} from "@/lib/meeting-export-utils";

export { meetingExportFolderName } from "@/lib/meeting-export-utils";

const EXPORT_TIMEOUT_MS = 60_000;

export interface SavedDesktopMeetingExport {
  status: "saved";
  desktop: true;
  token: string;
  path: string;
  directory: string;
  filename: string;
}

export interface SavedBrowserMeetingExport {
  status: "saved";
  desktop: false;
  filename: string;
}

export interface CancelledMeetingExport {
  status: "cancelled";
}

export type MeetingExportResult =
  | SavedDesktopMeetingExport
  | SavedBrowserMeetingExport
  | CancelledMeetingExport;

interface NativeSavedMeetingExport {
  token: string;
  path: string;
  directory: string;
  filename: string;
}

function downloadInBrowser(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  }
}

export async function saveMeetingExport(
  path: string,
  fallbackName: string,
): Promise<MeetingExportResult> {
  const desktop = isTauriRuntime();
  const response = await fetchWithTimeout(
    apiUrl(meetingExportApiPath(path)),
    { credentials: "include" },
    EXPORT_TIMEOUT_MS,
  );
  if (!response.ok) throw new Error(`Export failed (${response.status})`);

  const advertisedSize = Number(response.headers.get("Content-Length"));
  if (
    desktop
    && Number.isFinite(advertisedSize)
    && advertisedSize >= 0
    && !meetingExportFitsNativeLimit(advertisedSize)
  ) {
    throw new Error("The meeting export exceeds the 64 MiB desktop save limit.");
  }

  const filename = meetingExportFilename(
    response.headers.get("Content-Disposition"),
    fallbackName,
  );
  const blob = await response.blob();
  if (!desktop) {
    downloadInBrowser(blob, filename);
    return { status: "saved", desktop: false, filename };
  }
  if (!meetingExportFitsNativeLimit(blob.size)) {
    throw new Error("The meeting export exceeds the 64 MiB desktop save limit.");
  }

  const saved = await invoke<NativeSavedMeetingExport | null>("save_meeting_export", {
    filename,
    extension: meetingExportExtension(filename),
    bytes: new Uint8Array(await blob.arrayBuffer()),
  });
  if (!saved) return { status: "cancelled" };
  return { status: "saved", desktop: true, ...saved };
}

export async function openMeetingExport(token: string): Promise<void> {
  await invoke("open_meeting_export", { token });
}

export async function revealMeetingExport(token: string): Promise<void> {
  await invoke("reveal_meeting_export", { token });
}

import { invoke } from "@tauri-apps/api/core";
import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  meetingAudioExportMeetingId,
  meetingExportApiPath,
  meetingExportDownloadErrorMessage,
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
  meetingExportNativeCommandError,
  meetingExportNativeLimitErrorMessage,
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
  const safePath = meetingExportApiPath(path);
  const audioMeetingId = meetingAudioExportMeetingId(safePath);
  if (desktop && audioMeetingId) {
    let saved: NativeSavedMeetingExport | null;
    try {
      saved = await invoke<NativeSavedMeetingExport | null>("save_meeting_audio_export", {
        meetingId: audioMeetingId,
        filename: fallbackName,
      });
    } catch (error) {
      throw meetingExportNativeCommandError(
        error,
        "Scriber could not export the compressed meeting audio. Please try again.",
      );
    }
    if (!saved) return { status: "cancelled" };
    return { status: "saved", desktop: true, ...saved };
  }
  let response: Response;
  try {
    response = await fetchWithTimeout(
      apiUrl(safePath),
      { credentials: "include" },
      EXPORT_TIMEOUT_MS,
    );
  } catch {
    throw new Error(meetingExportDownloadErrorMessage());
  }
  if (!response.ok) {
    throw new Error(meetingExportDownloadErrorMessage(response.status));
  }

  const advertisedSize = Number(response.headers.get("Content-Length"));
  if (
    desktop
    && Number.isFinite(advertisedSize)
    && advertisedSize >= 0
    && !meetingExportFitsNativeLimit(advertisedSize)
  ) {
    throw new Error(meetingExportNativeLimitErrorMessage());
  }

  const filename = meetingExportFilename(
    response.headers.get("Content-Disposition"),
    fallbackName,
  );
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new Error(meetingExportDownloadErrorMessage());
  }
  if (!desktop) {
    try {
      downloadInBrowser(blob, filename);
    } catch {
      throw new Error(meetingExportDownloadErrorMessage());
    }
    return { status: "saved", desktop: false, filename };
  }
  if (!meetingExportFitsNativeLimit(blob.size)) {
    throw new Error(meetingExportNativeLimitErrorMessage());
  }

  let bytes: Uint8Array;
  try {
    bytes = new Uint8Array(await blob.arrayBuffer());
  } catch {
    throw new Error(meetingExportDownloadErrorMessage());
  }
  let saved: NativeSavedMeetingExport | null;
  try {
    saved = await invoke<NativeSavedMeetingExport | null>("save_meeting_export", {
      filename,
      extension: meetingExportExtension(filename),
      bytes,
    });
  } catch (error) {
    throw meetingExportNativeCommandError(
      error,
      "Scriber could not save the meeting export. Please try again.",
    );
  }
  if (!saved) return { status: "cancelled" };
  return { status: "saved", desktop: true, ...saved };
}

export async function openMeetingExport(token: string): Promise<void> {
  try {
    await invoke("open_meeting_export", { token });
  } catch (error) {
    throw meetingExportNativeCommandError(error, "Scriber could not open the saved file.");
  }
}

export async function revealMeetingExport(token: string): Promise<void> {
  try {
    await invoke("reveal_meeting_export", { token });
  } catch (error) {
    throw meetingExportNativeCommandError(error, "Scriber could not open the folder.");
  }
}

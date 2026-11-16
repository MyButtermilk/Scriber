import { invoke } from "@tauri-apps/api/core";
import type { MeetingExportDocument } from "@/lib/api-types";
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
  meetingPdfExportMeetingId,
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

export type MeetingExportResult = SavedDesktopMeetingExport | SavedBrowserMeetingExport | CancelledMeetingExport;

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

async function saveMeetingBytes(blob: Blob, filename: string, desktop: boolean): Promise<MeetingExportResult> {
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
    throw meetingExportNativeCommandError(error, "Scriber could not save the meeting export. Please try again.");
  }
  if (!saved) return { status: "cancelled" };
  return { status: "saved", desktop: true, ...saved };
}

function isMeetingExportDocument(value: unknown): value is MeetingExportDocument {
  const candidate = value as Partial<MeetingExportDocument> | null;
  return (
    Boolean(candidate) &&
    candidate?.apiVersion === "1" &&
    candidate.kind === "meeting" &&
    typeof candidate.title === "string" &&
    typeof candidate.filename === "string" &&
    typeof candidate.labels === "object" &&
    candidate.labels !== null &&
    Array.isArray(candidate.sections) &&
    Array.isArray(candidate.notes) &&
    Array.isArray(candidate.segments)
  );
}

/**
 * Typeset the Meeting PDF in the WebView with the pdfcn document components.
 * Returns null when that is not possible, so the caller downloads the
 * backend-rendered PDF instead.
 */
async function typesetMeetingPdf(meetingId: string): Promise<{ blob: Blob; filename: string } | null> {
  try {
    const path = meetingExportApiPath(`/api/meetings/${meetingId}/export/document`);
    const response = await fetchWithTimeout(apiUrl(path), { credentials: "include" }, EXPORT_TIMEOUT_MS);
    if (!response.ok) return null;
    const model: unknown = await response.json();
    if (!isMeetingExportDocument(model)) return null;
    // The typesetting engine is a separate chunk that loads on the first export.
    const { renderMeetingPdf } = await import("@/lib/pdf-export");
    const bytes = await renderMeetingPdf(model);
    return { blob: new Blob([bytes as BlobPart], { type: "application/pdf" }), filename: model.filename };
  } catch {
    return null;
  }
}

export async function saveMeetingExport(path: string, fallbackName: string): Promise<MeetingExportResult> {
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

  const pdfMeetingId = meetingPdfExportMeetingId(safePath);
  if (pdfMeetingId) {
    const typeset = await typesetMeetingPdf(pdfMeetingId);
    if (typeset) return saveMeetingBytes(typeset.blob, typeset.filename, desktop);
  }

  let response: Response;
  try {
    response = await fetchWithTimeout(apiUrl(safePath), { credentials: "include" }, EXPORT_TIMEOUT_MS);
  } catch {
    throw new Error(meetingExportDownloadErrorMessage());
  }
  if (!response.ok) {
    throw new Error(meetingExportDownloadErrorMessage(response.status));
  }

  const advertisedSize = Number(response.headers.get("Content-Length"));
  if (
    desktop &&
    Number.isFinite(advertisedSize) &&
    advertisedSize >= 0 &&
    !meetingExportFitsNativeLimit(advertisedSize)
  ) {
    throw new Error(meetingExportNativeLimitErrorMessage());
  }

  const filename = meetingExportFilename(response.headers.get("Content-Disposition"), fallbackName);
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new Error(meetingExportDownloadErrorMessage());
  }
  return saveMeetingBytes(blob, filename, desktop);
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

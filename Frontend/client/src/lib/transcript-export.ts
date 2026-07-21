import { invoke } from "@tauri-apps/api/core";

import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
  meetingExportNativeCommandError,
} from "@/lib/meeting-export-utils";
import {
  transcriptExportApiPath,
  transcriptExportDownloadErrorMessage,
  transcriptExportNativeLimitErrorMessage,
} from "@/lib/transcript-export-utils";

const EXPORT_TIMEOUT_MS = 60_000;

export type TranscriptExportResult =
  | { status: "saved"; desktop: true; filename: string }
  | { status: "saved"; desktop: false; filename: string }
  | { status: "cancelled" };

interface NativeSavedTranscriptExport {
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

export async function saveTranscriptExport(
  path: string,
  fallbackName: string,
): Promise<TranscriptExportResult> {
  const desktop = isTauriRuntime();
  const safePath = transcriptExportApiPath(path);
  let response: Response;
  try {
    response = await fetchWithTimeout(
      apiUrl(safePath),
      { credentials: "include" },
      EXPORT_TIMEOUT_MS,
    );
  } catch {
    throw new Error(transcriptExportDownloadErrorMessage());
  }
  if (!response.ok) {
    throw new Error(transcriptExportDownloadErrorMessage(response.status));
  }

  const advertisedSize = Number(response.headers.get("Content-Length"));
  if (
    desktop
    && Number.isFinite(advertisedSize)
    && advertisedSize >= 0
    && !meetingExportFitsNativeLimit(advertisedSize)
  ) {
    throw new Error(transcriptExportNativeLimitErrorMessage());
  }

  const filename = meetingExportFilename(
    response.headers.get("Content-Disposition"),
    fallbackName,
  );
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new Error(transcriptExportDownloadErrorMessage());
  }

  if (!desktop) {
    try {
      downloadInBrowser(blob, filename);
    } catch {
      throw new Error(transcriptExportDownloadErrorMessage());
    }
    return { status: "saved", desktop: false, filename };
  }
  if (!meetingExportFitsNativeLimit(blob.size)) {
    throw new Error(transcriptExportNativeLimitErrorMessage());
  }

  let bytes: Uint8Array;
  try {
    bytes = new Uint8Array(await blob.arrayBuffer());
  } catch {
    throw new Error(transcriptExportDownloadErrorMessage());
  }
  let saved: NativeSavedTranscriptExport | null;
  try {
    saved = await invoke<NativeSavedTranscriptExport | null>("save_transcript_export", {
      filename,
      extension: meetingExportExtension(filename),
      bytes,
    });
  } catch (error) {
    throw meetingExportNativeCommandError(
      error,
      "Scriber could not save the transcript export. Please try again.",
    );
  }
  if (!saved) return { status: "cancelled" };
  return { status: "saved", desktop: true, filename: saved.filename };
}

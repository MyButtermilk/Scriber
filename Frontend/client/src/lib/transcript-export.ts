import { invoke } from "@tauri-apps/api/core";

import { translateNow } from "@/i18n";
import type { TranscriptExportDocument } from "@/lib/api-types";
import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  meetingExportExtension,
  meetingExportFilename,
  meetingExportFitsNativeLimit,
  meetingExportNativeCommandError,
} from "@/lib/meeting-export-utils";
import type { TranscriptPdfLabels, TranscriptPdfVariant } from "@/lib/pdf-export";
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

async function saveTranscriptBytes(blob: Blob, filename: string, desktop: boolean): Promise<TranscriptExportResult> {
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
    throw meetingExportNativeCommandError(error, "Scriber could not save the transcript export. Please try again.");
  }
  if (!saved) return { status: "cancelled" };
  return { status: "saved", desktop: true, filename: saved.filename };
}

export async function saveTranscriptExport(path: string, fallbackName: string): Promise<TranscriptExportResult> {
  const desktop = isTauriRuntime();
  const safePath = transcriptExportApiPath(path);
  let response: Response;
  try {
    response = await fetchWithTimeout(apiUrl(safePath), { credentials: "include" }, EXPORT_TIMEOUT_MS);
  } catch {
    throw new Error(transcriptExportDownloadErrorMessage());
  }
  if (!response.ok) {
    throw new Error(transcriptExportDownloadErrorMessage(response.status));
  }

  const advertisedSize = Number(response.headers.get("Content-Length"));
  if (
    desktop &&
    Number.isFinite(advertisedSize) &&
    advertisedSize >= 0 &&
    !meetingExportFitsNativeLimit(advertisedSize)
  ) {
    throw new Error(transcriptExportNativeLimitErrorMessage());
  }

  const filename = meetingExportFilename(response.headers.get("Content-Disposition"), fallbackName);
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new Error(transcriptExportDownloadErrorMessage());
  }
  return saveTranscriptBytes(blob, filename, desktop);
}

export interface TranscriptPdfExportRequest {
  transcriptId: string;
  /** The whole transcript with its summary, or the summary on its own. */
  variant: TranscriptPdfVariant;
  labels: TranscriptPdfLabels;
  fallbackName: string;
}

function isTranscriptExportDocument(value: unknown): value is TranscriptExportDocument {
  const candidate = value as Partial<TranscriptExportDocument> | null;
  return (
    Boolean(candidate) &&
    candidate?.apiVersion === "1" &&
    candidate.kind === "transcript" &&
    typeof candidate.title === "string" &&
    typeof candidate.content === "string" &&
    typeof candidate.summary === "string" &&
    typeof candidate.filenameStem === "string"
  );
}

async function typesetTranscriptPdf(request: TranscriptPdfExportRequest): Promise<{ blob: Blob; filename: string }> {
  const path = transcriptExportApiPath(`/api/transcripts/${request.transcriptId}/export/document`);
  const response = await fetchWithTimeout(apiUrl(path), { credentials: "include" }, EXPORT_TIMEOUT_MS);
  if (!response.ok) throw new Error(transcriptExportDownloadErrorMessage(response.status));
  const model: unknown = await response.json();
  if (!isTranscriptExportDocument(model)) throw new Error(transcriptExportDownloadErrorMessage());
  if (request.variant === "summary" && !model.summary.trim()) throw new Error(transcriptExportDownloadErrorMessage());

  // The typesetting engine is a separate chunk that loads on the first export.
  const { renderTranscriptPdf } = await import("@/lib/pdf-export");
  const bytes = await renderTranscriptPdf(model, request.labels, request.variant);
  const stem = model.filenameStem || "transcript";
  const filename = request.variant === "summary" ? `${stem} - ${request.labels.summary}.pdf` : `${stem}.pdf`;
  return { blob: new Blob([bytes as BlobPart], { type: "application/pdf" }), filename };
}

/**
 * Save a transcript PDF typeset in the WebView with the pdfcn document
 * components. If the full document cannot be typeset here, the backend's
 * plain PDF renderer still produces the export; the summary-only document has
 * no backend equivalent and reports the failure instead.
 */
export async function saveTranscriptPdfExport(request: TranscriptPdfExportRequest): Promise<TranscriptExportResult> {
  let typeset: { blob: Blob; filename: string } | null = null;
  try {
    typeset = await typesetTranscriptPdf(request);
  } catch {
    if (request.variant === "summary") {
      throw new Error(translateNow("Scriber could not create the summary PDF. Please try again."));
    }
  }
  if (!typeset) {
    return saveTranscriptExport(`/api/transcripts/${request.transcriptId}/export/pdf`, request.fallbackName);
  }
  return saveTranscriptBytes(typeset.blob, typeset.filename, isTauriRuntime());
}

import { apiUrl } from "@/lib/backend";
import type { ApiMessageResponse, FileTranscribeResponse } from "@/lib/api-types";
import { translateNow, type TranslationValues } from "@/i18n";

export type FileUploadStatus = "idle" | "uploading" | "server_processing" | "completed" | "failed";
export type FileUploadItemStatus = FileUploadStatus | "queued";

export interface FileUploadLocalizedText {
  key: string;
  values?: TranslationValues;
}

export interface FileUploadQueueItem {
  id: string;
  fileName: string;
  status: FileUploadItemStatus;
  progress: number;
  statusText: string;
  statusValues?: TranslationValues;
  response: FileTranscribeResponse | null;
  error: string;
}

export interface FileUploadBatchFailure {
  fileName: string;
  error: string;
}

export interface FileUploadBatchResult {
  responses: FileTranscribeResponse[];
  failures: FileUploadBatchFailure[];
}

export interface FileUploadSnapshot {
  status: FileUploadStatus;
  progress: number;
  items: FileUploadQueueItem[];
  totalFiles: number;
  completedFiles: number;
  failedFiles: number;
  updatedAt: number;
}

interface StartFileUploadBatchOptions {
  getServerProcessingText: (file: File) => FileUploadLocalizedText;
}

const FILE_UPLOAD_TIMEOUT_MS = 2 * 60 * 60 * 1000;
// Bound simultaneous local copies and ffmpeg preparation; admitted provider jobs
// run independently in the backend's durable job scheduler.
const MAX_PARALLEL_FILE_IMPORTS = 2;
let snapshot: FileUploadSnapshot = {
  status: "idle",
  progress: 0,
  items: [],
  totalFiles: 0,
  completedFiles: 0,
  failedFiles: 0,
  updatedAt: 0,
};
let running = 0;
const pending: Array<() => Promise<void>> = [];
const listeners = new Set<() => void>();

function publishItems(items: FileUploadQueueItem[]) {
  const completedFiles = items.filter((item) => item.status === "completed").length;
  const failedFiles = items.filter((item) => item.status === "failed").length;
  const uploading = items.some((item) => item.status === "uploading" || item.status === "queued");
  snapshot = {
    items,
    completedFiles,
    failedFiles,
    totalFiles: items.length,
    status: uploading
      ? "uploading"
      : items.some((item) => item.status === "server_processing")
        ? "server_processing"
        : failedFiles
          ? "failed"
          : "completed",
    progress: items.length ? Math.round(items.reduce((sum, item) => sum + item.progress, 0) / items.length) : 0,
    updatedAt: Date.now(),
  };
  listeners.forEach((listener) => listener());
}

function updateQueueItem(id: string, patch: Partial<FileUploadQueueItem>) {
  publishItems(snapshot.items.map((item) => (item.id === id ? { ...item, ...patch } : item)));
}

export function subscribeFileUpload(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getFileUploadSnapshot(): FileUploadSnapshot {
  return snapshot;
}

export function isFileUploadActive(): boolean {
  return running > 0 || pending.length > 0;
}

function pumpQueue() {
  while (running < MAX_PARALLEL_FILE_IMPORTS && pending.length > 0) {
    const task = pending.shift()!;
    running += 1;
    // Each task settles its own success/failure before releasing this slot.
    void task().finally(() => {
      running -= 1;
      pumpQueue();
    });
  }
}

function uploadSingleFile(
  file: File,
  itemId: string,
  serverProcessingText: FileUploadLocalizedText,
): Promise<FileTranscribeResponse> {
  updateQueueItem(itemId, { status: "uploading", progress: 0, statusText: "Uploading…" });
  const formData = new FormData();
  formData.append("file", file);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let serverPhase = false;
    const switchToServerPhase = () => {
      if (serverPhase) return;
      serverPhase = true;
      updateQueueItem(itemId, {
        status: "server_processing",
        progress: 100,
        statusText: serverProcessingText.key,
        statusValues: serverProcessingText.values,
      });
    };
    xhr.open("POST", apiUrl("/api/file/transcribe"));
    xhr.withCredentials = true;
    xhr.timeout = FILE_UPLOAD_TIMEOUT_MS;
    xhr.upload.onprogress = (event) => {
      if (serverPhase || !event.lengthComputable || event.total <= 0) return;
      updateQueueItem(itemId, { progress: Math.max(0, Math.min(100, Math.round((event.loaded / event.total) * 100))) });
      if (event.loaded >= event.total) switchToServerPhase();
    };
    xhr.upload.onload = switchToServerPhase;
    xhr.onerror = () => reject(new Error(translateNow("Network error during file upload")));
    xhr.ontimeout = () => reject(new Error(translateNow("File upload timed out")));
    xhr.onabort = () => reject(new Error(translateNow("File upload was canceled")));
    xhr.onload = () => {
      let parsed: Partial<FileTranscribeResponse> & ApiMessageResponse;
      try {
        parsed = JSON.parse(xhr.responseText || "{}");
      } catch {
        parsed = {};
      }
      if (xhr.status >= 200 && xhr.status < 300 && typeof parsed.id === "string" && parsed.id) {
        resolve(parsed as FileTranscribeResponse);
      } else {
        reject(
          new Error(parsed.message ? translateNow(parsed.message) : xhr.statusText || translateNow("Upload failed")),
        );
      }
    };
    xhr.send(formData);
  });
}

export function startFileUploadBatch(
  files: readonly File[],
  { getServerProcessingText }: StartFileUploadBatchOptions,
): Promise<FileUploadBatchResult> {
  if (!files.length) return Promise.reject(new Error(translateNow("No files selected.")));
  const items: FileUploadQueueItem[] = files.map((file) => ({
    id: crypto.randomUUID(),
    fileName: file.name,
    status: "queued",
    progress: 0,
    statusText: "Queued",
    response: null,
    error: "",
  }));
  const previousItems = isFileUploadActive() ? snapshot.items : [];
  const results = files.map(
    (file, index) =>
      new Promise<FileUploadQueueItem>((resolve) => {
        const item = items[index];
        pending.push(async () => {
          try {
            const response = await uploadSingleFile(file, item.id, getServerProcessingText(file));
            updateQueueItem(item.id, {
              status: "completed",
              progress: 100,
              statusText: "Transcription started…",
              response,
            });
          } catch (error: unknown) {
            updateQueueItem(item.id, {
              status: "failed",
              progress: 100,
              statusText: "Upload failed",
              error: error instanceof Error ? error.message : String(error),
            });
          }
          resolve(snapshot.items.find((entry) => entry.id === item.id)!);
        });
      }),
  );
  publishItems([...previousItems, ...items]);
  pumpQueue();
  return Promise.all(results).then((finished) => ({
    responses: finished.flatMap((item) => (item.response ? [item.response] : [])),
    failures: finished
      .filter((item) => item.status === "failed")
      .map((item) => ({ fileName: item.fileName, error: item.error })),
  }));
}

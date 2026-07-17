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
  fileName: string;
  statusText: string;
  statusValues?: TranslationValues;
  response: FileTranscribeResponse | null;
  responses: FileTranscribeResponse[];
  error: string;
  items: FileUploadQueueItem[];
  totalFiles: number;
  completedFiles: number;
  failedFiles: number;
  currentIndex: number;
  updatedAt: number;
}

interface StartFileUploadOptions {
  serverProcessingLabel: string;
  serverProcessingValues?: TranslationValues;
}

interface StartFileUploadBatchOptions {
  getServerProcessingText: (file: File) => FileUploadLocalizedText;
}

const idleSnapshot: FileUploadSnapshot = {
  status: "idle",
  progress: 0,
  fileName: "",
  statusText: "",
  statusValues: undefined,
  response: null,
  responses: [],
  error: "",
  items: [],
  totalFiles: 0,
  completedFiles: 0,
  failedFiles: 0,
  currentIndex: -1,
  updatedAt: 0,
};
const FILE_UPLOAD_TIMEOUT_MS = 2 * 60 * 60 * 1000;

let snapshot: FileUploadSnapshot = idleSnapshot;
let activeUpload: Promise<FileUploadBatchResult> | null = null;
const listeners = new Set<() => void>();

function publish(next: Partial<FileUploadSnapshot>) {
  snapshot = {
    ...snapshot,
    ...next,
    updatedAt: Date.now(),
  };
  listeners.forEach((listener) => listener());
}

function createUploadId(index: number): string {
  const randomPart = Math.random().toString(36).slice(2, 8);
  return `${Date.now().toString(36)}-${index}-${randomPart}`;
}

function updateQueueItem(
  itemId: string,
  itemPatch: Partial<FileUploadQueueItem>,
  snapshotPatch: Partial<FileUploadSnapshot> = {},
) {
  const items = snapshot.items.map((item) => (item.id === itemId ? { ...item, ...itemPatch } : item));
  const completedFiles = items.filter((item) => item.status === "completed").length;
  const failedFiles = items.filter((item) => item.status === "failed").length;
  const progress =
    items.length > 0
      ? Math.round(items.reduce((sum, item) => sum + Math.max(0, Math.min(100, item.progress)), 0) / items.length)
      : 0;

  publish({
    items,
    completedFiles,
    failedFiles,
    progress,
    ...snapshotPatch,
  });
}

export function subscribeFileUpload(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getFileUploadSnapshot(): FileUploadSnapshot {
  return snapshot;
}

export function isFileUploadActive(): boolean {
  return Boolean(activeUpload) || snapshot.status === "uploading" || snapshot.status === "server_processing";
}

export function resetFileUploadSnapshot(): void {
  if (isFileUploadActive()) {
    return;
  }
  snapshot = idleSnapshot;
  listeners.forEach((listener) => listener());
}

function uploadSingleFile(
  file: File,
  {
    itemId,
    currentIndex,
    serverProcessingText,
  }: {
    itemId: string;
    currentIndex: number;
    serverProcessingText: FileUploadLocalizedText;
  },
): Promise<FileTranscribeResponse> {
  const uploadingText: FileUploadLocalizedText = {
    key: "Uploading {{file}}...",
    values: { file: file.name },
  };

  updateQueueItem(
    itemId,
    {
      status: "uploading",
      progress: 0,
      statusText: uploadingText.key,
      statusValues: uploadingText.values,
      error: "",
      response: null,
    },
    {
      status: "uploading",
      fileName: file.name,
      statusText: uploadingText.key,
      statusValues: uploadingText.values,
      response: null,
      error: "",
      currentIndex,
    },
  );

  const formData = new FormData();
  formData.append("file", file);

  return new Promise<FileTranscribeResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let switchedToServerPhase = false;

    const switchToServerPhase = () => {
      if (switchedToServerPhase) return;
      switchedToServerPhase = true;
      updateQueueItem(
        itemId,
        {
          status: "server_processing",
          progress: 96,
          statusText: serverProcessingText.key,
          statusValues: serverProcessingText.values,
        },
        {
          status: "server_processing",
          fileName: file.name,
          statusText: serverProcessingText.key,
          statusValues: serverProcessingText.values,
          currentIndex,
        },
      );
    };

    xhr.open("POST", apiUrl("/api/file/transcribe"));
    xhr.withCredentials = true;
    xhr.timeout = FILE_UPLOAD_TIMEOUT_MS;

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      const percent = Math.max(5, Math.min(95, Math.round((event.loaded / event.total) * 95)));
      updateQueueItem(
        itemId,
        {
          status: "uploading",
          progress: percent,
          statusText: uploadingText.key,
          statusValues: uploadingText.values,
        },
        {
          status: "uploading",
          fileName: file.name,
          statusText: uploadingText.key,
          statusValues: uploadingText.values,
          currentIndex,
        },
      );
      if (event.loaded >= event.total) {
        switchToServerPhase();
      }
    };

    xhr.upload.onload = () => {
      switchToServerPhase();
    };

    xhr.onerror = () => {
      reject(new Error(translateNow("Network error during file upload")));
    };

    xhr.ontimeout = () => {
      reject(new Error(translateNow("File upload timed out")));
    };

    xhr.onabort = () => {
      reject(new Error(translateNow("File upload was canceled")));
    };

    xhr.onload = () => {
      const responseText = xhr.responseText || "";
      let parsed: Partial<FileTranscribeResponse> & ApiMessageResponse = {};
      try {
        parsed = responseText ? (JSON.parse(responseText) as Partial<FileTranscribeResponse> & ApiMessageResponse) : {};
      } catch {
        parsed = {};
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parsed as FileTranscribeResponse);
        return;
      }

      reject(new Error(
        parsed.message
          ? translateNow(parsed.message)
          : xhr.statusText || translateNow("Upload failed"),
      ));
    };

    xhr.send(formData);
  });
}

export function startFileUpload(
  file: File,
  { serverProcessingLabel, serverProcessingValues }: StartFileUploadOptions,
): Promise<FileTranscribeResponse> {
  const batchPromise = startFileUploadBatch([file], {
    getServerProcessingText: () => ({
      key: serverProcessingLabel,
      values: serverProcessingValues,
    }),
  });

  return batchPromise.then((result) => {
    const response = result.responses[0];
    if (response) {
      return response;
    }
    throw new Error(result.failures[0]?.error || translateNow("Upload failed"));
  });
}

export function startFileUploadBatch(
  files: readonly File[],
  { getServerProcessingText }: StartFileUploadBatchOptions,
): Promise<FileUploadBatchResult> {
  const selectedFiles = files.filter(Boolean);
  if (activeUpload || isFileUploadActive()) {
    return Promise.reject(new Error(translateNow("A file upload batch is already in progress.")));
  }
  if (selectedFiles.length === 0) {
    return Promise.reject(new Error(translateNow("No files selected.")));
  }

  const items: FileUploadQueueItem[] = selectedFiles.map((file, index) => ({
    id: createUploadId(index),
    fileName: file.name,
    status: index === 0 ? "uploading" : "queued",
    progress: 0,
    statusText: index === 0 ? "Uploading {{file}}..." : "Queued",
    statusValues: index === 0 ? { file: file.name } : undefined,
    response: null,
    error: "",
  }));

  publish({
    status: "uploading",
    progress: 0,
    fileName: selectedFiles[0]?.name || "",
    statusText: "Uploading {{file}}...",
    statusValues: { file: selectedFiles[0]?.name || "file" },
    response: null,
    responses: [],
    error: "",
    items,
    totalFiles: selectedFiles.length,
    completedFiles: 0,
    failedFiles: 0,
    currentIndex: 0,
  });

  const batchPromise = (async (): Promise<FileUploadBatchResult> => {
    const responses: FileTranscribeResponse[] = [];
    const failures: FileUploadBatchFailure[] = [];

    for (let index = 0; index < selectedFiles.length; index += 1) {
      const file = selectedFiles[index];
      const item = items[index];
      try {
        const response = await uploadSingleFile(file, {
          itemId: item.id,
          currentIndex: index,
          serverProcessingText: getServerProcessingText(file),
        });
        responses.push(response);
        updateQueueItem(
          item.id,
          {
            status: "completed",
            progress: 100,
            statusText: "Transcription started...",
            statusValues: undefined,
            response,
            error: "",
          },
          {
            status: index < selectedFiles.length - 1 ? "uploading" : "completed",
            response,
            responses: [...responses],
            error: "",
          },
        );
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : String(error);
        failures.push({ fileName: file.name, error: message });
        updateQueueItem(
          item.id,
          {
            status: "failed",
            progress: 100,
            statusText: "Upload failed",
            statusValues: undefined,
            response: null,
            error: message,
          },
          {
            status: index < selectedFiles.length - 1 ? "uploading" : "failed",
            error: message,
          },
        );
      }
    }

    const finalStatus: FileUploadStatus = failures.length > 0 ? "failed" : "completed";
    const statusText: FileUploadLocalizedText =
      failures.length > 0
        ? responses.length === 1
          ? { key: "1 of {{total}} transcription started.", values: { total: selectedFiles.length } }
          : {
              key: "{{count}} of {{total}} transcriptions started.",
              values: { count: responses.length, total: selectedFiles.length },
            }
        : responses.length === 1
          ? { key: "1 transcription started." }
          : { key: "{{count}} transcriptions started.", values: { count: responses.length } };

    publish({
      status: finalStatus,
      progress: 100,
      fileName: "",
      statusText: statusText.key,
      statusValues: statusText.values,
      response: responses.at(-1) || null,
      responses,
      error: failures[0]?.error || "",
      completedFiles: responses.length,
      failedFiles: failures.length,
      currentIndex: selectedFiles.length - 1,
    });

    return { responses, failures };
  })();

  activeUpload = batchPromise;
  batchPromise.then(
    () => {
      activeUpload = null;
    },
    () => {
      activeUpload = null;
    },
  );

  return batchPromise;
}

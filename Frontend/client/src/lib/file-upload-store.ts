import { apiUrl } from "@/lib/backend";
import type { ApiMessageResponse, FileTranscribeResponse } from "@/lib/api-types";

export type FileUploadStatus = "idle" | "uploading" | "server_processing" | "completed" | "failed";
export type FileUploadItemStatus = FileUploadStatus | "queued";

export interface FileUploadQueueItem {
  id: string;
  fileName: string;
  status: FileUploadItemStatus;
  progress: number;
  statusText: string;
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
}

interface StartFileUploadBatchOptions {
  getServerProcessingLabel: (file: File) => string;
}

const idleSnapshot: FileUploadSnapshot = {
  status: "idle",
  progress: 0,
  fileName: "",
  statusText: "",
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

function batchPrefix(currentIndex: number, totalFiles: number): string {
  return totalFiles > 1 ? `File ${currentIndex + 1} of ${totalFiles}: ` : "";
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
    totalFiles,
    serverProcessingLabel,
  }: {
    itemId: string;
    currentIndex: number;
    totalFiles: number;
    serverProcessingLabel: string;
  },
): Promise<FileTranscribeResponse> {
  const prefix = batchPrefix(currentIndex, totalFiles);
  const uploadingLabel = `${prefix}Uploading ${file.name}...`;

  updateQueueItem(
    itemId,
    {
      status: "uploading",
      progress: 0,
      statusText: uploadingLabel,
      error: "",
      response: null,
    },
    {
      status: "uploading",
      fileName: file.name,
      statusText: uploadingLabel,
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
      const statusText = `${prefix}${serverProcessingLabel}`;
      updateQueueItem(
        itemId,
        {
          status: "server_processing",
          progress: 96,
          statusText,
        },
        {
          status: "server_processing",
          fileName: file.name,
          statusText,
          currentIndex,
        },
      );
    };

    xhr.open("POST", apiUrl("/api/file/transcribe"));
    xhr.withCredentials = true;

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      const percent = Math.max(5, Math.min(95, Math.round((event.loaded / event.total) * 95)));
      updateQueueItem(
        itemId,
        {
          status: "uploading",
          progress: percent,
          statusText: uploadingLabel,
        },
        {
          status: "uploading",
          fileName: file.name,
          statusText: uploadingLabel,
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
      reject(new Error("Network error during file upload"));
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

      reject(new Error(parsed.message || xhr.statusText || "Upload failed"));
    };

    xhr.send(formData);
  });
}

export function startFileUpload(
  file: File,
  { serverProcessingLabel }: StartFileUploadOptions,
): Promise<FileTranscribeResponse> {
  const batchPromise = startFileUploadBatch([file], {
    getServerProcessingLabel: () => serverProcessingLabel,
  });

  return batchPromise.then((result) => {
    const response = result.responses[0];
    if (response) {
      return response;
    }
    throw new Error(result.failures[0]?.error || "Upload failed");
  });
}

export function startFileUploadBatch(
  files: readonly File[],
  { getServerProcessingLabel }: StartFileUploadBatchOptions,
): Promise<FileUploadBatchResult> {
  const selectedFiles = files.filter(Boolean);
  if (activeUpload || isFileUploadActive()) {
    return Promise.reject(new Error("A file upload batch is already in progress."));
  }
  if (selectedFiles.length === 0) {
    return Promise.reject(new Error("No files selected."));
  }

  const items: FileUploadQueueItem[] = selectedFiles.map((file, index) => ({
    id: createUploadId(index),
    fileName: file.name,
    status: index === 0 ? "uploading" : "queued",
    progress: 0,
    statusText: index === 0 ? `Uploading ${file.name}...` : "Queued",
    response: null,
    error: "",
  }));

  publish({
    status: "uploading",
    progress: 0,
    fileName: selectedFiles[0]?.name || "",
    statusText:
      selectedFiles.length > 1
        ? `File 1 of ${selectedFiles.length}: Uploading ${selectedFiles[0]?.name || "file"}...`
        : `Uploading ${selectedFiles[0]?.name || "file"}...`,
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
          totalFiles: selectedFiles.length,
          serverProcessingLabel: getServerProcessingLabel(file),
        });
        responses.push(response);
        updateQueueItem(
          item.id,
          {
            status: "completed",
            progress: 100,
            statusText: "Transcription started...",
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
    const statusText =
      failures.length > 0
        ? `${responses.length} of ${selectedFiles.length} transcription${responses.length === 1 ? "" : "s"} started.`
        : `${responses.length} transcription${responses.length === 1 ? "" : "s"} started.`;

    publish({
      status: finalStatus,
      progress: 100,
      fileName: "",
      statusText,
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

import { apiUrl } from "@/lib/backend";
import type { ApiMessageResponse, FileTranscribeResponse } from "@/lib/api-types";

export type FileUploadStatus = "idle" | "uploading" | "server_processing" | "completed" | "failed";

export interface FileUploadSnapshot {
  status: FileUploadStatus;
  progress: number;
  fileName: string;
  statusText: string;
  response: FileTranscribeResponse | null;
  error: string;
  updatedAt: number;
}

interface StartFileUploadOptions {
  serverProcessingLabel: string;
}

const idleSnapshot: FileUploadSnapshot = {
  status: "idle",
  progress: 0,
  fileName: "",
  statusText: "",
  response: null,
  error: "",
  updatedAt: 0,
};

let snapshot: FileUploadSnapshot = idleSnapshot;
let activeUpload: Promise<FileTranscribeResponse> | null = null;
const listeners = new Set<() => void>();

function publish(next: Partial<FileUploadSnapshot>) {
  snapshot = {
    ...snapshot,
    ...next,
    updatedAt: Date.now(),
  };
  listeners.forEach((listener) => listener());
}

export function subscribeFileUpload(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getFileUploadSnapshot(): FileUploadSnapshot {
  return snapshot;
}

export function isFileUploadActive(): boolean {
  return snapshot.status === "uploading" || snapshot.status === "server_processing";
}

export function resetFileUploadSnapshot(): void {
  if (isFileUploadActive()) {
    return;
  }
  snapshot = idleSnapshot;
  listeners.forEach((listener) => listener());
}

export function startFileUpload(
  file: File,
  { serverProcessingLabel }: StartFileUploadOptions,
): Promise<FileTranscribeResponse> {
  if (activeUpload || isFileUploadActive()) {
    return Promise.reject(new Error("A file upload is already in progress."));
  }

  publish({
    status: "uploading",
    progress: 0,
    fileName: file.name,
    statusText: `Uploading ${file.name}...`,
    response: null,
    error: "",
  });

  const formData = new FormData();
  formData.append("file", file);

  activeUpload = new Promise<FileTranscribeResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let switchedToServerPhase = false;

    const switchToServerPhase = () => {
      if (switchedToServerPhase) return;
      switchedToServerPhase = true;
      publish({
        status: "server_processing",
        progress: 96,
        statusText: serverProcessingLabel,
      });
    };

    xhr.open("POST", apiUrl("/api/file/transcribe"));
    xhr.withCredentials = true;

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      const percent = Math.max(5, Math.min(95, Math.round((event.loaded / event.total) * 95)));
      publish({
        status: "uploading",
        progress: percent,
        statusText: `Uploading ${file.name}...`,
      });
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

  activeUpload
    .then((response) => {
      publish({
        status: "completed",
        progress: 100,
        statusText: "Transcription started...",
        response,
        error: "",
      });
    })
    .catch((error: unknown) => {
      publish({
        status: "failed",
        progress: 0,
        statusText: "",
        response: null,
        error: error instanceof Error ? error.message : String(error),
      });
    })
    .finally(() => {
      activeUpload = null;
    });

  return activeUpload;
}

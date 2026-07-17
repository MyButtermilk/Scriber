import { translateNow } from "@/i18n";

export const MAX_NATIVE_MEETING_EXPORT_BYTES = 64 * 1024 * 1024;
export type MeetingEmailDraftAttachment = "" | "md" | "pdf" | "docx";

const MEETING_EXPORT_API_PATH = /^\/api\/meetings\/[A-Za-z0-9_-]{1,128}\/(?:export\/(?:json|md|pdf|docx|audio)|export-email(?:\?attachment=(?:pdf|docx|md))?)$/;
const MEETING_AUDIO_EXPORT_API_PATH = /^\/api\/meetings\/([A-Za-z0-9_-]{1,128})\/export\/audio$/;
const NATIVE_MEETING_EXPORT_ERROR_CODE = /^meeting_export_[a-z_]{1,64}$/;
const MAX_NATIVE_ERROR_MESSAGE_LENGTH = 320;

interface NativeMeetingExportError {
  code: string;
  message: string;
}

export function meetingExportApiPath(path: string): string {
  if (!MEETING_EXPORT_API_PATH.test(path)) {
    throw new Error(translateNow("That meeting export address is not allowed."));
  }
  return path;
}

export function meetingExportDownloadErrorMessage(status?: number): string {
  const safeStatus = typeof status === "number"
    && Number.isInteger(status)
    && status >= 100
    && status <= 599
    ? status
    : undefined;
  return safeStatus === undefined
    ? translateNow("The meeting export could not be downloaded. Please try again.")
    : translateNow(
        "The meeting export could not be downloaded (HTTP {{status}}). Please try again.",
        { status: safeStatus },
      );
}

export function meetingExportNativeLimitErrorMessage(): string {
  return translateNow("The meeting export exceeds the 64 MiB desktop save limit.");
}

export function meetingExportNativeCommandError(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const candidate = error as Partial<NativeMeetingExportError>;
    const message = typeof candidate.message === "string" ? candidate.message.trim() : "";
    if (
      typeof candidate.code === "string"
      && NATIVE_MEETING_EXPORT_ERROR_CODE.test(candidate.code)
      && message.length > 0
      && message.length <= MAX_NATIVE_ERROR_MESSAGE_LENGTH
      && !message.includes("/")
      && !message.includes("\\")
      && !message.includes("\r")
      && !message.includes("\n")
    ) {
      return new Error(message);
    }
  }
  return new Error(translateNow(fallback));
}

export function meetingAudioExportMeetingId(path: string): string | null {
  return path.match(MEETING_AUDIO_EXPORT_API_PATH)?.[1] || null;
}

export function meetingEmailDraftPath(
  meetingId: string,
  attachment: MeetingEmailDraftAttachment,
): string {
  const path = `/api/meetings/${meetingId}/export-email${attachment ? `?attachment=${attachment}` : ""}`;
  return meetingExportApiPath(path);
}

export function meetingExportFitsNativeLimit(size: number): boolean {
  return Number.isSafeInteger(size)
    && size >= 0
    && size <= MAX_NATIVE_MEETING_EXPORT_BYTES;
}

export function meetingExportFilename(
  contentDisposition: string | null,
  fallbackName: string,
): string {
  const disposition = contentDisposition || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="([^"]+)"/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      // A malformed server filename should not prevent the user from exporting.
    }
  }
  return plain || fallbackName;
}

export function meetingExportExtension(filename: string): string {
  const extension = filename.trim().match(/\.([a-z0-9]+)$/i)?.[1]?.toLowerCase();
  return extension && ["json", "md", "pdf", "docx", "eml", "opus"].includes(extension)
    ? extension
    : "pdf";
}

export function meetingExportFolderName(directory: string): string | null {
  const segments = directory.split(/[\\/]+/).filter(Boolean);
  return segments.at(-1) || null;
}

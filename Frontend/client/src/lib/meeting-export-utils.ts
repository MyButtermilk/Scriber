export const MAX_NATIVE_MEETING_EXPORT_BYTES = 64 * 1024 * 1024;
export type MeetingEmailDraftAttachment = "" | "md" | "pdf" | "docx";

const MEETING_EXPORT_API_PATH = /^\/api\/meetings\/[A-Za-z0-9_-]{1,128}\/(?:export\/(?:json|md|pdf|docx|audio)|export-email(?:\?attachment=(?:pdf|docx|md))?)$/;
const MEETING_AUDIO_EXPORT_API_PATH = /^\/api\/meetings\/([A-Za-z0-9_-]{1,128})\/export\/audio$/;

export function meetingExportApiPath(path: string): string {
  if (!MEETING_EXPORT_API_PATH.test(path)) {
    throw new Error("That meeting export address is not allowed.");
  }
  return path;
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

export function meetingExportFolderName(directory: string): string {
  const segments = directory.split(/[\\/]+/).filter(Boolean);
  return segments.at(-1) || "the folder you chose";
}

export const MAX_NATIVE_MEETING_EXPORT_BYTES = 64 * 1024 * 1024;

const MEETING_EXPORT_API_PATH = /^\/api\/meetings\/[A-Za-z0-9_-]{1,128}\/(?:export\/(?:json|md|pdf|docx)|export-email(?:\?attachment=(?:pdf|docx|md))?)$/;

export function meetingExportApiPath(path: string): string {
  if (!MEETING_EXPORT_API_PATH.test(path)) {
    throw new Error("That meeting export address is not allowed.");
  }
  return path;
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
  return extension && ["json", "md", "pdf", "docx", "eml"].includes(extension)
    ? extension
    : "pdf";
}

export function meetingExportFolderName(directory: string): string {
  const segments = directory.split(/[\\/]+/).filter(Boolean);
  return segments.at(-1) || "the folder you chose";
}

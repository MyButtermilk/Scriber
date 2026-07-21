import { translateNow } from "@/i18n";

const TRANSCRIPT_EXPORT_API_PATH = /^\/api\/transcripts\/[A-Za-z0-9_-]{1,128}\/export\/(?:pdf|docx)$/;

export function transcriptExportApiPath(path: string): string {
  if (!TRANSCRIPT_EXPORT_API_PATH.test(path)) {
    throw new Error(translateNow("That transcript export address is not allowed."));
  }
  return path;
}

export function transcriptExportDownloadErrorMessage(status?: number): string {
  const safeStatus = typeof status === "number"
    && Number.isInteger(status)
    && status >= 100
    && status <= 599
    ? status
    : undefined;
  return safeStatus === undefined
    ? translateNow("The transcript export could not be downloaded. Please try again.")
    : translateNow(
        "The transcript export could not be downloaded (HTTP {{status}}). Please try again.",
        { status: safeStatus },
      );
}

export function transcriptExportNativeLimitErrorMessage(): string {
  return translateNow("The transcript export exceeds the 64 MiB desktop save limit.");
}

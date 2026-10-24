import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { translateNow } from "@/i18n";

type RecordingErrorMessage = Extract<ScriberWebSocketMessage, { type: "error" }>;
type SessionFinishedMessage = Extract<ScriberWebSocketMessage, { type: "session_finished" }>;

const MAX_REPORTED_ERROR_SESSIONS = 32;
export type RecordingErrorToastMessage = Partial<Omit<RecordingErrorMessage, "message">> & {
  message?: unknown;
};
type ToastFn = (args: { title: string; description: string; variant: "destructive"; duration: number }) => void;

let lastToastKey = "";
let lastToastDescription = "";
let lastToastAt = 0;

function cleanText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function recordingErrorToastMessageFromPayload(
  payload: unknown,
  fallbackMessage = "An error occurred during recording.",
): RecordingErrorToastMessage | null {
  if (!payload || typeof payload !== "object") {
    const message = cleanText(fallbackMessage);
    return message ? { type: "error", apiVersion: "1", message } : null;
  }

  const record = payload as Record<string, unknown>;
  const message = cleanText(record.message) || cleanText(fallbackMessage);
  if (!message) {
    return null;
  }

  return {
    type: "error",
    apiVersion: cleanText(record.apiVersion) || "1",
    message,
    title: cleanText(record.title) || undefined,
    provider: cleanText(record.provider) || undefined,
    providerLabel: cleanText(record.providerLabel) || undefined,
    category: cleanText(record.category) || undefined,
    code: cleanText(record.code) || undefined,
    retryable: typeof record.retryable === "boolean" ? record.retryable : undefined,
    sessionId: cleanText(record.sessionId) || undefined,
  };
}

/** Remember a session whose specific `error` event has already been shown. */
export function rememberReportedErrorSession(reportedSessionIds: Set<string>, sessionId: unknown): void {
  const id = cleanText(sessionId);
  if (!id) return;
  reportedSessionIds.delete(id);
  reportedSessionIds.add(id);
  while (reportedSessionIds.size > MAX_REPORTED_ERROR_SESSIONS) {
    const oldest = reportedSessionIds.values().next().value;
    if (oldest === undefined) break;
    reportedSessionIds.delete(oldest);
  }
}

/**
 * Toast for a failed `session_finished`, unless its `error` event already
 * reported the session. Only one toast is visible at a time, so a generic
 * fallback here would replace the actionable provider message.
 */
export function failedSessionToastMessage(
  msg: SessionFinishedMessage,
  reportedSessionIds: Set<string>,
  fallback: { title: string; message: string },
): RecordingErrorToastMessage | null {
  if (cleanText(msg.session?.status).toLowerCase() !== "failed") return null;
  const sessionId = cleanText(msg.sessionId) || (msg.session?.id != null ? String(msg.session.id) : "");
  if (sessionId && reportedSessionIds.delete(sessionId)) return null;
  // Failed Live Mic records keep their safe failure in `step`; older
  // records embedded it in the content as `[Error] ...`.
  const legacyError = String(msg.session?.content || "").match(/\[Error\]\s*([^\n]+)/i);
  return recordingErrorToastMessageFromPayload({
    type: "error",
    apiVersion: msg.apiVersion,
    message: cleanText(msg.session?.step) || legacyError?.[1]?.trim() || fallback.message,
    title: fallback.title,
    sessionId: sessionId || undefined,
  });
}

export function showRecordingErrorToast(toast: ToastFn, msg: RecordingErrorToastMessage): void {
  const baseDescription = translateNow(cleanText(msg.message) || "An error occurred during recording.");
  const title =
    (cleanText(msg.title) ? translateNow(cleanText(msg.title)) : "") ||
    (cleanText(msg.providerLabel)
      ? translateNow("{{provider}} error", { provider: cleanText(msg.providerLabel) })
      : translateNow("Recording Error"));
  const code = cleanText(msg.code);
  const description = code
    ? translateNow("{{description}} Code: {{code}}.", { description: baseDescription, code })
    : baseDescription;
  const key = [
    cleanText(msg.sessionId),
    cleanText(msg.provider),
    cleanText(msg.category),
    code,
    title,
    baseDescription,
  ].join("|");
  const now = Date.now();

  if ((key === lastToastKey || baseDescription === lastToastDescription) && now - lastToastAt < 2500) {
    return;
  }

  lastToastKey = key;
  lastToastDescription = baseDescription;
  lastToastAt = now;
  toast({
    title,
    description,
    variant: "destructive",
    duration: msg.retryable === true ? 8000 : 7000,
  });
}

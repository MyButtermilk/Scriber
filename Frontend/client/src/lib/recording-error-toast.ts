import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

type RecordingErrorMessage = Extract<ScriberWebSocketMessage, { type: "error" }>;
export type RecordingErrorToastMessage = Partial<Omit<RecordingErrorMessage, "message">> & {
    message?: unknown;
};
type ToastFn = (args: {
    title: string;
    description: string;
    variant: "destructive";
    duration: number;
}) => void;

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

export function showRecordingErrorToast(toast: ToastFn, msg: RecordingErrorToastMessage): void {
    const baseDescription = cleanText(msg.message) || "An error occurred during recording.";
    const title =
        cleanText(msg.title) ||
        (cleanText(msg.providerLabel) ? `${cleanText(msg.providerLabel)} error` : "Recording Error");
    const code = cleanText(msg.code);
    const description = code ? `${baseDescription} Code: ${code}.` : baseDescription;
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

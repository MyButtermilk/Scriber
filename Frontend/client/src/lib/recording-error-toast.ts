import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

type RecordingErrorMessage = Extract<ScriberWebSocketMessage, { type: "error" }>;
type ToastFn = (args: {
    title: string;
    description: string;
    variant: "destructive";
    duration: number;
}) => void;

let lastToastKey = "";
let lastToastAt = 0;

function cleanText(value: unknown): string {
    return typeof value === "string" ? value.trim() : "";
}

export function showRecordingErrorToast(toast: ToastFn, msg: RecordingErrorMessage): void {
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

    if (key === lastToastKey && now - lastToastAt < 2500) {
        return;
    }

    lastToastKey = key;
    lastToastAt = now;
    toast({
        title,
        description,
        variant: "destructive",
        duration: msg.retryable ? 8000 : 7000,
    });
}

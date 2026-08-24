import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

type FallbackMessage = Extract<ScriberWebSocketMessage, { type: "post_processing_fallback_used" }>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
type ToastFn = (args: { title: string; description: string; duration: number }) => void;

const MAX_SEEN_FALLBACK_EVENTS = 100;

const FALLBACK_MODEL_LABELS: Readonly<Record<string, string>> = {
  "cerebras/gemma-4-31b": "Gemma 4 31B",
  "openai/gpt-oss-120b": "GPT-OSS 120B",
  "google/gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
  "minimax/minimax-m3": "MiniMax M3",
  "z-ai/glm-5.2": "GLM 5.2",
};

function fallbackModelLabel(model: string): string {
  const family = model.split(":", 1)[0].toLowerCase();
  return FALLBACK_MODEL_LABELS[family] ?? model;
}

export function showPostProcessingFallbackToast(
  toast: ToastFn,
  t: Translate,
  message: FallbackMessage,
  seenEventIds: Set<string>,
): boolean {
  const eventId = String(message.eventId || "").trim();
  const primaryModel = String(message.primaryModel || "").trim();
  const fallbackModel = String(message.fallbackModel || "").trim();
  if (!eventId || !primaryModel || !fallbackModel || seenEventIds.has(eventId)) {
    return false;
  }

  if (seenEventIds.size >= MAX_SEEN_FALLBACK_EVENTS) {
    const oldestEventId = seenEventIds.values().next().value;
    if (typeof oldestEventId === "string") {
      seenEventIds.delete(oldestEventId);
    }
  }
  seenEventIds.add(eventId);
  if (message.desktopNotificationAccepted === true) {
    return true;
  }
  toast({
    title: t("Fallback model used"),
    description: t("Scriber used {{fallbackModel}} instead of {{primaryModel}} for this dictation.", {
      fallbackModel: fallbackModelLabel(fallbackModel),
      primaryModel: fallbackModelLabel(primaryModel),
    }),
    duration: 6000,
  });
  return true;
}

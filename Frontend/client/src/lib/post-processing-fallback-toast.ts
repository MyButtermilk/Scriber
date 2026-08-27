import type { ToastActionElement } from "@/components/ui/toast";
import type { PostProcessingFallbackReasonCategory, ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

type FallbackMessage = Extract<ScriberWebSocketMessage, { type: "post_processing_fallback_used" }>;
type Translate = (key: string, values?: Record<string, string | number>) => string;
type ToastFn = (args: {
  title: string;
  description: string;
  duration: number;
  variant?: "destructive";
  action?: ToastActionElement;
}) => unknown;

export type CredentialSettingsRequest = {
  provider: string;
  actionLabel: string;
};

type CreateCredentialAction = (request: CredentialSettingsRequest) => ToastActionElement;

const MAX_SEEN_FALLBACK_EVENTS = 100;

const FALLBACK_MODEL_LABELS: Readonly<Record<string, string>> = {
  "cerebras/gemma-4-31b": "Gemma 4 31B",
  "openai/gpt-oss-120b": "GPT-OSS 120B",
  "google/gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
  "minimax/minimax-m3": "MiniMax M3",
  // Keep the legacy label for replayed v1 diagnostics after the catalog migration.
  "z-ai/glm-5.2": "GLM 5.2",
  "z-ai/glm-5.3-flash": "GLM 5.3 Flash",
};

const REASON_CATEGORIES = new Set<PostProcessingFallbackReasonCategory>([
  "authentication",
  "quota_or_payment",
  "rate_limit",
  "provider_unavailable",
  "timeout",
  "output_limit",
  "request_rejected",
  "provider_error",
]);

function fallbackModelLabel(model: string): string {
  const family = model.split(":", 1)[0].toLowerCase();
  return FALLBACK_MODEL_LABELS[family] ?? model;
}

function normalizedHttpStatus(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 100 && value <= 599 ? value : null;
}

function fallbackReasonCategory(message: FallbackMessage): PostProcessingFallbackReasonCategory {
  if (message.reasonCategory && REASON_CATEGORIES.has(message.reasonCategory)) {
    return message.reasonCategory;
  }

  const status = normalizedHttpStatus(message.primaryFailureStatus);
  if (status === 401 || status === 403) return "authentication";
  if (status === 402) return "quota_or_payment";
  if (status === 408) return "timeout";
  if (status === 429) return "rate_limit";
  if (status !== null && status >= 500) return "provider_unavailable";

  const reason = String(message.reason || "")
    .trim()
    .toLowerCase()
    .replaceAll("-", "_")
    .replaceAll(".", "_");
  if (
    [
      "auth_invalid",
      "authentication_error",
      "authentication_failed",
      "credential_rejected",
      "invalid_api_key",
      "unauthorized",
      "forbidden",
      "access_denied",
    ].includes(reason)
  ) {
    return "authentication";
  }
  if (
    [
      "quota_exceeded",
      "quota_cooldown",
      "payment_required",
      "insufficient_credits",
      "insufficient_quota",
      "billing_error",
    ].includes(reason)
  ) {
    return "quota_or_payment";
  }
  if (["rate_limit", "rate_limit_error", "too_many_requests"].includes(reason)) return "rate_limit";
  if (
    [
      "server_error",
      "service_unavailable",
      "provider_unavailable",
      "provider_overloaded",
      "overloaded",
      "internal_server_error",
      "bad_gateway",
      "gateway_timeout",
    ].includes(reason)
  ) {
    return "provider_unavailable";
  }
  if (["timeout", "request_timeout", "deadline_exceeded", "timed_out"].includes(reason)) return "timeout";
  if (
    ["output_limit", "max_tokens", "length", "incomplete_response", "meeting_analysis_incomplete_response"].includes(
      reason,
    )
  ) {
    return "output_limit";
  }
  if (status !== null && status >= 400) return "request_rejected";
  if (["invalid_request", "invalid_request_error", "request_rejected", "model_not_found"].includes(reason)) {
    return "request_rejected";
  }
  return "provider_error";
}

function credentialProviderForModel(model: string): string | null {
  const family = model.split(":", 1)[0].trim().toLowerCase();
  if (family.startsWith("cerebras/")) return "Cerebras";
  if (family.startsWith("gemini-")) return "Gemini";
  if (family.startsWith("gpt-")) return "OpenAI";
  if (family === "muse-spark-1.2" || family === "muse-spark-1.2-contributor") return "Meta Model API";
  if (family === "celeris-1") return "Celeris";
  if (family.includes("/")) return "OpenRouter";
  return null;
}

function fallbackToastPresentation(
  t: Translate,
  message: FallbackMessage,
  primaryModel: string,
  fallbackModel: string,
): {
  title: string;
  description: string;
  duration: number;
  variant?: "destructive";
  credentialRequest?: CredentialSettingsRequest;
} {
  const category = fallbackReasonCategory(message);
  const primaryLabel = fallbackModelLabel(primaryModel);
  const fallbackLabel = fallbackModelLabel(fallbackModel);
  const values = { primaryModel: primaryLabel, fallbackModel: fallbackLabel };

  if (category === "authentication") {
    const provider = credentialProviderForModel(primaryModel);
    const providerLabel = provider ?? primaryLabel;
    return {
      title: t("{{provider}} API key was rejected", { provider: providerLabel }),
      description: t(
        "{{provider}} rejected its API key. Scriber used {{fallbackModel}} for this dictation. Check or replace the key in Settings.",
        { provider: providerLabel, fallbackModel: fallbackLabel },
      ),
      duration: 12000,
      variant: "destructive",
      credentialRequest: provider
        ? {
            provider,
            actionLabel: t("Check {{provider}} key", { provider }),
          }
        : undefined,
    };
  }
  if (category === "quota_or_payment") {
    return {
      title: t("Provider quota or payment issue"),
      description: t(
        "{{primaryModel}} could not process this dictation because its quota or payment access was unavailable. Scriber used {{fallbackModel}} instead.",
        values,
      ),
      duration: 8000,
    };
  }
  if (category === "rate_limit") {
    return {
      title: t("Provider rate limit reached"),
      description: t(
        "{{primaryModel}} was temporarily rate-limited. Scriber used {{fallbackModel}} for this dictation.",
        values,
      ),
      duration: 8000,
    };
  }
  if (category === "provider_unavailable") {
    return {
      title: t("Provider temporarily unavailable"),
      description: t(
        "{{primaryModel}} was unavailable or under heavy load. Scriber used {{fallbackModel}} for this dictation.",
        values,
      ),
      duration: 8000,
    };
  }
  if (category === "timeout") {
    return {
      title: t("Post-processing timed out"),
      description: t(
        "{{primaryModel}} did not answer before the post-processing deadline. Scriber used {{fallbackModel}} instead.",
        values,
      ),
      duration: 8000,
    };
  }
  if (category === "output_limit") {
    return {
      title: t("Provider output limit reached"),
      description: t(
        "{{primaryModel}} reached its output limit before returning a complete result. Scriber used {{fallbackModel}} instead.",
        values,
      ),
      duration: 8000,
    };
  }
  if (category === "request_rejected") {
    return {
      title: t("Provider rejected the request"),
      description: t(
        "{{primaryModel}} rejected this post-processing request. Scriber used {{fallbackModel}} instead; check the provider and model settings if this repeats.",
        values,
      ),
      duration: 8000,
    };
  }
  return {
    title: t("Fallback model used"),
    description: t("Scriber used {{fallbackModel}} instead of {{primaryModel}} for this dictation.", values),
    duration: 6000,
  };
}

export function showPostProcessingFallbackToast(
  toast: ToastFn,
  t: Translate,
  message: FallbackMessage,
  seenEventIds: Set<string>,
  createCredentialAction?: CreateCredentialAction,
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

  const hasReasonDetails = Boolean(
    message.reasonCategory || String(message.reason || "").trim() || normalizedHttpStatus(message.primaryFailureStatus),
  );
  if (message.desktopNotificationAccepted === true && !hasReasonDetails) {
    return true;
  }

  const presentation = fallbackToastPresentation(t, message, primaryModel, fallbackModel);
  const action =
    presentation.credentialRequest && createCredentialAction
      ? createCredentialAction(presentation.credentialRequest)
      : undefined;
  toast({
    title: presentation.title,
    description: presentation.description,
    duration: presentation.duration,
    variant: presentation.variant,
    action,
  });
  return true;
}

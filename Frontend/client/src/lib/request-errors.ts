import { translateNow } from "@/i18n";

const NETWORK_ERROR_TOKENS = [
  "failed to fetch",
  "networkerror",
  "network request failed",
  "err_connection_refused",
  "connection refused",
];

const TIMEOUT_ERROR_TOKENS = ["timeout", "timed out", "aborted", "aborterror"];
const CORS_ERROR_TOKENS = ["cors", "cross-origin"];
const INVALID_ARGUMENT_TOKENS = ["errno 22", "invalid argument"];

const PREFIX_PATTERN = /^\[(error|timeout|download error)\]\s*/i;
const STATUS_PREFIX_PATTERN = /^\d{3}:\s*/;

function stripLowLevelPrefixes(rawMessage: string): string {
  const trimmed = (rawMessage || "").trim();
  if (!trimmed) return "";
  return trimmed.replace(STATUS_PREFIX_PATTERN, "").replace(PREFIX_PATTERN, "").trim();
}

export function friendlyRequestMessage(rawMessage: string, fallback = "Request failed."): string {
  const stripped = stripLowLevelPrefixes(rawMessage);
  const message = stripped || (rawMessage || "").trim();
  if (!message) return translateNow(fallback);

  const normalized = message.toLowerCase();

  if (NETWORK_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("Cannot connect to the Scriber backend. Please start the backend service and try again.");
  }
  if (TIMEOUT_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("The backend is taking too long to respond. It may still be starting. Please try again.");
  }
  if (CORS_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("Connection was blocked by browser security settings. Please check your backend URL and CORS settings.");
  }
  if (INVALID_ARGUMENT_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("The backend rejected the request (invalid argument). Please ensure the backend is running and retry.");
  }

  return translateNow(message);
}

export function friendlyError(error: unknown, fallback = "Request failed."): string {
  if (error instanceof Error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      return translateNow("The backend is taking too long to respond. It may still be starting. Please try again.");
    }
    return friendlyRequestMessage(error.message, fallback);
  }
  if (typeof error === "string") {
    return friendlyRequestMessage(error, fallback);
  }
  return translateNow(fallback);
}

export async function responseDetailMessage(res: Response): Promise<string> {
  const contentType = (res.headers.get("content-type") || "").toLowerCase();

  if (contentType.includes("application/json")) {
    const payload = await res.json().catch(() => null) as Record<string, unknown> | null;
    const message =
      (typeof payload?.message === "string" && payload.message) ||
      (typeof payload?.error === "string" && payload.error) ||
      "";
    if (message) {
      return message;
    }
  }

  return ((await res.text().catch(() => "")) || "").trim();
}

export async function responseErrorMessage(res: Response): Promise<string> {
  const detail = await responseDetailMessage(res);
  if (detail) {
    return `${res.status}: ${detail}`;
  }
  return `${res.status}: ${res.statusText || translateNow("Request failed")}`;
}

export function extractFailureMessage(content: string, step: string): string {
  const rawContent = (content || "").trim();
  if (rawContent) {
    const matches = Array.from(rawContent.matchAll(/\[(error|timeout|download error)\]\s*([^\n]+)/gi));
    if (matches.length > 0) {
      const last = matches[matches.length - 1];
      const reason = (last?.[2] || "").trim();
      if (reason) return reason;
    }
    return rawContent;
  }
  return (step || "").trim();
}


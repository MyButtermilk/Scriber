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

const PREFIX_PATTERN = /^\[(error|timeout|download error|storage error)\]\s*/i;
const STATUS_PREFIX_PATTERN = /^\d{3}:\s*/;

/** Optional content-free backend reference; never carry arbitrary response metadata. */
export class ApiRequestError extends Error {
  readonly reference?: string;

  constructor(message: string, reference?: unknown, options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiRequestError";
    this.reference = typeof reference === "string" && /^[0-9a-f]{32}$/.test(reference) ? reference : undefined;
  }
}

export function requiresYouTubeSignIn(message: string): boolean {
  const normalized = message.toLowerCase().replaceAll("’", "'");
  return (
    normalized.includes("sign in to confirm you're not a bot") ||
    normalized.includes("sign in to confirm your age") ||
    normalized.includes("youtube requires sign-in.") ||
    normalized.includes("the page needs to be reloaded") ||
    normalized.includes("youtube rejected playback extraction.")
  );
}

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

  if (
    normalized.includes("the page needs to be reloaded") ||
    normalized.includes("youtube rejected playback extraction.")
  ) {
    return translateNow("YouTube rejected playback extraction. Connect a fresh YouTube sign-in below to try again.");
  }
  if (requiresYouTubeSignIn(message)) {
    return translateNow("YouTube requires sign-in. Connect your YouTube account below to continue.");
  }

  if (NETWORK_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("Cannot connect to the Scriber backend. Please start the backend service and try again.");
  }
  if (TIMEOUT_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow("The backend is taking too long to respond. It may still be starting. Please try again.");
  }
  if (CORS_ERROR_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow(
      "Connection was blocked by browser security settings. Please check your backend URL and CORS settings.",
    );
  }
  if (INVALID_ARGUMENT_TOKENS.some((token) => normalized.includes(token))) {
    return translateNow(
      "The backend rejected the request (invalid argument). Please ensure the backend is running and retry.",
    );
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

async function responseDetails(res: Response): Promise<{ message: string; reference?: unknown }> {
  const contentType = (res.headers.get("content-type") || "").toLowerCase();

  if (contentType.includes("application/json")) {
    const payload = (await res.json().catch(() => null)) as Record<string, unknown> | null;
    const message =
      (typeof payload?.message === "string" && payload.message) ||
      (typeof payload?.error === "string" && payload.error) ||
      "";
    if (message) {
      return { message, reference: payload?.reference };
    }
  }

  return { message: ((await res.text().catch(() => "")) || "").trim() };
}

export async function responseDetailMessage(res: Response): Promise<string> {
  return (await responseDetails(res)).message;
}

export async function responseRequestError(res: Response): Promise<ApiRequestError> {
  const detail = await responseDetails(res);
  return new ApiRequestError(
    `${res.status}: ${detail.message || res.statusText || translateNow("Request failed")}`,
    detail.reference,
  );
}

export async function responseErrorMessage(res: Response): Promise<string> {
  return (await responseRequestError(res)).message;
}

/** Legacy failed records stored only their failure text as transcript content. */
export function isFailureOnlyContent(status: unknown, content: unknown): boolean {
  return status === "failed" && /^\[(error|timeout|download error|storage error)\]/i.test(String(content || "").trim());
}

export function extractFailureMessage(content: string, step: string): string {
  const rawContent = (content || "").trim();
  if (rawContent) {
    const matches = Array.from(rawContent.matchAll(/\[(error|timeout|download error|storage error)\]\s*([^\n]+)/gi));
    if (matches.length > 0) {
      const last = matches[matches.length - 1];
      const reason = (last?.[2] || "").trim();
      if (reason) return reason;
    }
    return rawContent;
  }
  return (step || "").trim();
}

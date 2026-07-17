import { translateNow } from "@/i18n";

const DEFAULT_FETCH_TIMEOUT_MS = 15_000;

/**
 * Fetch with a hard deadline while preserving a caller-provided abort signal.
 * This prevents local backend requests from remaining pending forever when a
 * supervised worker is wedged rather than fully disconnected.
 */
export async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = DEFAULT_FETCH_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const callerSignal = init.signal;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);

  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timeoutId = globalThis.setTimeout(
    () => controller.abort(new DOMException(translateNow("Request timed out"), "TimeoutError")),
    Math.max(1, timeoutMs),
  );
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    globalThis.clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

export function withPromiseTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  label = "Operation",
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timeoutId = globalThis.setTimeout(
      () => {
        const error = new Error(translateNow("{{label}} timed out", { label: translateNow(label) }));
        error.name = "TimeoutError";
        reject(error);
      },
      Math.max(1, timeoutMs),
    );
    promise.then(
      (value) => {
        globalThis.clearTimeout(timeoutId);
        resolve(value);
      },
      (error) => {
        globalThis.clearTimeout(timeoutId);
        reject(error);
      },
    );
  });
}

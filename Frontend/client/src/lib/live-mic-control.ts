import {
  apiUrl,
  isBenchmarkActivationEnabled,
  isTauriRuntime,
} from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { recordingErrorToastMessageFromPayload, showRecordingErrorToast } from "@/lib/recording-error-toast";
import { friendlyError, friendlyRequestMessage } from "@/lib/request-errors";
import type { LiveMicRuntimeErrorResponse } from "@/lib/api-types";
import { translateNow } from "@/i18n";

export const LIVE_MIC_START_PATH = "/api/live-mic/start";
export const LIVE_MIC_STOP_REQUEST_PATH = "/api/live-mic/stop-request";

type LiveMicControlFailureKind = "backend" | "network";

type LiveMicErrorPayload = Partial<LiveMicRuntimeErrorResponse> & Record<string, unknown>;

export interface BenchmarkActivationMarker {
  schemaVersion: 1;
  marker: "button_received";
  source: "tauri_ui_command";
  runId: string;
  sampleId: string;
  processId: number;
  qpcTicks: number;
  qpcFrequency: number;
  timestampNs: number;
}

type DestructiveToast = (args: {
  title: string;
  description: string;
  variant: "destructive";
  duration: number;
}) => unknown;

export class LiveMicControlError extends Error {
  readonly kind: LiveMicControlFailureKind;
  readonly status: number | null;
  readonly payload: LiveMicErrorPayload | null;

  constructor(
    message: string,
    options: {
      kind: LiveMicControlFailureKind;
      status?: number | null;
      payload?: LiveMicErrorPayload | null;
    },
  ) {
    super(message);
    this.name = "LiveMicControlError";
    this.kind = options.kind;
    this.status = options.status ?? null;
    this.payload = options.payload ?? null;
  }
}

function payloadMessage(payload: LiveMicErrorPayload | null): string {
  if (!payload) return "";
  if (typeof payload.message === "string") return payload.message.trim();
  if (typeof payload.error === "string") return payload.error.trim();
  return "";
}

async function responseFailure(response: Response): Promise<LiveMicControlError> {
  const rawText = ((await response.text().catch(() => "")) || "").trim();
  let payload: LiveMicErrorPayload | null = null;
  if (rawText) {
    try {
      const parsed = JSON.parse(rawText) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed as LiveMicErrorPayload;
      }
    } catch {
      // A legacy backend may still return plain text. Keep it as a readable fallback.
    }
  }

  const detail = payloadMessage(payload) || rawText || response.statusText || "Request failed.";
  return new LiveMicControlError(friendlyRequestMessage(detail), {
    kind: "backend",
    status: response.status,
    payload,
  });
}

async function requestLiveMicControl(
  path: string,
  timeoutMs: number,
  body?: Record<string, unknown>,
): Promise<Response> {
  let response: Response;
  try {
    response = await fetchWithTimeout(
      apiUrl(path),
      {
        method: "POST",
        credentials: "include",
        ...(body
          ? {
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            }
          : {}),
      },
      timeoutMs,
    );
  } catch (error) {
    throw new LiveMicControlError(
      friendlyError(error, "Cannot connect to the Scriber backend. Please try again."),
      { kind: "network" },
    );
  }

  if (!response.ok) {
    throw await responseFailure(response);
  }
  return response;
}

/** Start a normal Live Mic session. This function never retries a failed start. */
export function requestLiveMicStart(
  benchmarkActivationMarker?: BenchmarkActivationMarker | null,
): Promise<Response> {
  return requestLiveMicControl(
    LIVE_MIC_START_PATH,
    15_000,
    benchmarkActivationMarker ? { benchmarkActivationMarker } : undefined,
  );
}

/** Capture the benchmark-only native boundary at the primary button handler. */
export async function captureBenchmarkButtonActivationMarker(): Promise<
  BenchmarkActivationMarker | null
> {
  if (!isTauriRuntime() || !isBenchmarkActivationEnabled()) {
    return null;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const marker = await invoke<BenchmarkActivationMarker | null>(
      "capture_benchmark_button_marker",
    );
    if (!marker) {
      throw new Error("native benchmark activation is not armed");
    }
    return marker;
  } catch (error) {
    throw new LiveMicControlError(
      friendlyError(error, "Benchmark button activation could not be captured."),
      { kind: "backend" },
    );
  }
}

/**
 * Ask the backend to stop Live Mic and return as soon as finalization has been
 * scheduled. Completion is delivered by the existing state/WebSocket stream.
 */
export function requestLiveMicStop(): Promise<Response> {
  return requestLiveMicControl(LIVE_MIC_STOP_REQUEST_PATH, 5_000);
}

/**
 * Show one consistent Live Mic error. Only a genuine transport failure asks
 * the backend-status provider for a fresh health check; the recording request
 * itself is deliberately never retried.
 */
export function presentLiveMicControlFailure(
  error: unknown,
  options: {
    toast: DestructiveToast;
    checkBackendStatus: () => Promise<boolean>;
  },
): void {
  const failure = error instanceof LiveMicControlError ? error : null;
  if (failure?.kind === "network") {
    void options.checkBackendStatus().catch(() => false);
    options.toast({
      title: translateNow("Backend unavailable"),
      description: translateNow(failure.message),
      variant: "destructive",
      duration: 7000,
    });
    return;
  }

  const message = failure?.message || friendlyError(error, "Live Mic could not start.");
  const recordingError = recordingErrorToastMessageFromPayload(failure?.payload, message);
  if (recordingError) {
    showRecordingErrorToast(options.toast, recordingError);
    return;
  }

  options.toast({
    title: translateNow("Action failed"),
    description: translateNow(message),
    variant: "destructive",
    duration: 7000,
  });
}

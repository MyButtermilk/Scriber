import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

export const LIVE_MIC_STOP_REQUEST_PATH = "/api/live-mic/stop-request";

/**
 * Ask the backend to stop Live Mic and return as soon as finalization has been
 * scheduled. Completion is delivered by the existing state/WebSocket stream.
 */
export function requestLiveMicStop(): Promise<Response> {
  return fetchWithTimeout(
    apiUrl(LIVE_MIC_STOP_REQUEST_PATH),
    { method: "POST", credentials: "include" },
    5_000,
  );
}

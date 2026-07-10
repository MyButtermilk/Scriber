import type { SettingsResponse } from "@/lib/api-types";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

export const DEFAULT_VISUALIZER_BAR_COUNT = 45;
export const MIN_VISUALIZER_BAR_COUNT = 16;
export const MAX_VISUALIZER_BAR_COUNT = 128;

export function normalizeVisualizerBarCount(
  value: unknown,
  fallback = DEFAULT_VISUALIZER_BAR_COUNT,
): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.max(
    MIN_VISUALIZER_BAR_COUNT,
    Math.min(MAX_VISUALIZER_BAR_COUNT, Math.round(numeric)),
  );
}

export async function loadVisualizerBarCount(signal?: AbortSignal): Promise<number> {
  const res = await fetchWithTimeout(
    apiUrl("/api/settings"),
    { credentials: "include", signal },
    10_000,
  );
  if (!res.ok) {
    throw new Error(res.statusText || "Failed to load visualizer settings");
  }
  const settings = (await res.json()) as SettingsResponse;
  return normalizeVisualizerBarCount(settings.visualizerBarCount);
}

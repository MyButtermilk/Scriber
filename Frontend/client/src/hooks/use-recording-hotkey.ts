import { useQuery } from "@tanstack/react-query";
import type { SettingsResponse } from "@/lib/api-types";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

export interface RecordingHotkey {
  /** Display form from settings, for example "Ctrl + Alt + X". */
  hotkey: string;
  mode: "toggle" | "push_to_talk";
}

/**
 * The configured global recording hotkey, or null while settings are unknown.
 * Shares the ["/api/settings"] cache that Settings updates after a save.
 */
export function useRecordingHotkey(): RecordingHotkey | null {
  const { data } = useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(apiUrl("/api/settings"), { credentials: "include", signal }, 10_000);
      if (!res.ok) throw new Error(res.statusText || "Failed to load settings");
      return (await res.json()) as SettingsResponse;
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const hotkey = String(data?.hotkey || data?.hotkeyRaw || "").trim();
  if (!hotkey) return null;
  return { hotkey, mode: data?.mode === "push_to_talk" ? "push_to_talk" : "toggle" };
}

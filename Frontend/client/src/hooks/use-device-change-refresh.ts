import { useEffect } from "react";

import { apiUrl } from "@/lib/backend";
import type { MicrophonesRefreshResponse } from "@/lib/api-types";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

export function useDeviceChangeRefresh(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || typeof navigator === "undefined" || !navigator.mediaDevices) {
      return;
    }

    const mediaDevices = navigator.mediaDevices;
    if (typeof mediaDevices.addEventListener !== "function") {
      return;
    }

    let refreshTimer: number | undefined;
    const requestRefresh = () => {
      if (refreshTimer) {
        window.clearTimeout(refreshTimer);
      }
      refreshTimer = window.setTimeout(() => {
        void fetchWithTimeout(apiUrl("/api/microphones/refresh"), {
          method: "POST",
          credentials: "include",
        }, 8_000).then(async (res): Promise<MicrophonesRefreshResponse | null> => {
          if (!res.ok) {
            return null;
          }
          return (await res.json()) as MicrophonesRefreshResponse;
        }).catch(() => {
          // Best-effort hint; backend DeviceMonitor polling/native callbacks remain authoritative.
        });
      }, 500);
    };

    mediaDevices.addEventListener("devicechange", requestRefresh);
    return () => {
      if (refreshTimer) {
        window.clearTimeout(refreshTimer);
      }
      mediaDevices.removeEventListener("devicechange", requestRefresh);
    };
  }, [enabled]);
}

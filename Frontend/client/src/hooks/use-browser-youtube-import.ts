import { useEffect, useRef } from "react";
import { useSearch } from "wouter";

import type { YouTubeSearchItem } from "@/lib/api-types";
import { parseBrowserYoutubeImport, stripBrowserYoutubeImportParams } from "@/lib/browser-youtube-import";
import { apiRequest } from "@/lib/queryClient";

interface BrowserYoutubeImportOptions {
  busy: boolean;
  onImport: (item: YouTubeSearchItem) => void | Promise<void>;
}

export function useBrowserYoutubeImport({ busy, onImport }: BrowserYoutubeImportOptions): void {
  const search = useSearch();
  const handledRequestRef = useRef<string | null>(null);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || busy) {
      return;
    }
    const imported = parseBrowserYoutubeImport(search);
    if (!imported || imported.requestId === handledRequestRef.current) {
      return;
    }

    handledRequestRef.current = imported.requestId;
    const nextSearch = stripBrowserYoutubeImportParams(search);
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`,
    );
    // Let the optional browser session arrive before starting extraction.
    // Old extensions and denied cookie permission still use the public path.
    void apiRequest("POST", "/api/youtube/session/browser-accept", { videoId: imported.item.videoId })
      .catch(() => undefined)
      .then(() => {
        if (mountedRef.current && handledRequestRef.current === imported.requestId) {
          return onImport(imported.item);
        }
      });
  }, [busy, onImport, search]);
}

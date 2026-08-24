import { useEffect, useRef } from "react";
import { useSearch } from "wouter";

import type { YouTubeSearchItem } from "@/lib/api-types";
import { parseBrowserYoutubeImport, stripBrowserYoutubeImportParams } from "@/lib/browser-youtube-import";

interface BrowserYoutubeImportOptions {
  busy: boolean;
  onImport: (item: YouTubeSearchItem) => void | Promise<void>;
}

export function useBrowserYoutubeImport({ busy, onImport }: BrowserYoutubeImportOptions): void {
  const search = useSearch();
  const handledRequestRef = useRef<string | null>(null);

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
    void onImport(imported.item);
  }, [busy, onImport, search]);
}

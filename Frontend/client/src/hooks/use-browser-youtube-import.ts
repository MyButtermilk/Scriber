import { useEffect, useRef, useState } from "react";
import { useSearch } from "wouter";

import type { YouTubeSearchItem } from "@/lib/api-types";
import {
  parseBrowserYoutubeImport,
  stripBrowserYoutubeImportParams,
  type BrowserYoutubeImport,
} from "@/lib/browser-youtube-import";
import { apiRequest } from "@/lib/queryClient";

interface BrowserYoutubeImportOptions {
  busy: boolean;
  onImport: (item: YouTubeSearchItem) => void | Promise<void>;
}

export function useBrowserYoutubeImport({ busy, onImport }: BrowserYoutubeImportOptions): void {
  const search = useSearch();
  const seenRequestsRef = useRef(new Set<string>());
  const queueRef = useRef<BrowserYoutubeImport[]>([]);
  const mountedRef = useRef(false);
  const processingRef = useRef(false);
  const [completed, setCompleted] = useState(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const pending = parseBrowserYoutubeImport(search);
    if (pending && !seenRequestsRef.current.has(pending.requestId)) {
      seenRequestsRef.current.add(pending.requestId);
      queueRef.current.push(pending);
    }
    if (busy || processingRef.current) return;
    const imported = queueRef.current.shift();
    if (!imported) return;
    processingRef.current = true;
    // Let the optional browser session arrive before starting extraction.
    // Old extensions and denied cookie permission still use the public path.
    void apiRequest("POST", "/api/youtube/session/browser-accept", { videoId: imported.item.videoId })
      .catch(() => undefined)
      .then(() => {
        if (!mountedRef.current) return;
        // A newer request stays in the URL until this import settles. It also
        // keeps the caller from navigating away before the next video starts.
        if (parseBrowserYoutubeImport(window.location.search)?.requestId === imported.requestId) {
          const nextSearch = stripBrowserYoutubeImportParams(window.location.search);
          window.history.replaceState(
            window.history.state,
            "",
            `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`,
          );
        }
        return onImport(imported.item);
      })
      .finally(() => {
        processingRef.current = false;
        if (mountedRef.current) setCompleted((value) => value + 1);
      });
  }, [busy, onImport, search, completed]);
}

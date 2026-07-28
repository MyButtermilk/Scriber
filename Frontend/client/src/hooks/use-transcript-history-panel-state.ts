import { useEffect, useRef, useState } from "react";

import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { TRANSCRIPT_HISTORY_VIEW_MODE_STORAGE_KEY } from "@/lib/storage-keys";

export type TranscriptHistoryViewMode = "list" | "grid";

export function parseTranscriptHistoryViewMode(
  value: string | null | undefined,
  fallback: TranscriptHistoryViewMode,
): TranscriptHistoryViewMode {
  return value === "list" || value === "grid" ? value : fallback;
}

export function serializeTranscriptHistorySearch(value: string): string | null {
  const trimmed = value.trim();
  return trimmed || null;
}

function readStoredViewMode(fallback: TranscriptHistoryViewMode): TranscriptHistoryViewMode {
  if (typeof window === "undefined") {
    return fallback;
  }
  try {
    return parseTranscriptHistoryViewMode(
      window.localStorage.getItem(TRANSCRIPT_HISTORY_VIEW_MODE_STORAGE_KEY),
      fallback,
    );
  } catch {
    return fallback;
  }
}

interface UseTranscriptHistoryPanelStateOptions {
  defaultViewMode: TranscriptHistoryViewMode;
  debounceMs?: number;
}

export function useTranscriptHistoryPanelState({
  defaultViewMode,
  debounceMs = 300,
}: UseTranscriptHistoryPanelStateOptions) {
  const initialViewModeRef = useRef<TranscriptHistoryViewMode>(readStoredViewMode(defaultViewMode));
  const initialViewMode = initialViewModeRef.current;
  const [viewMode, setViewMode] = useUrlQueryState<TranscriptHistoryViewMode>("view", initialViewMode, {
    parse: (raw) => parseTranscriptHistoryViewMode(raw, initialViewMode),
  });
  const [searchValue, setSearchValue] = useUrlQueryState("q", "", {
    parse: (raw) => raw ?? "",
    serialize: serializeTranscriptHistorySearch,
    syncDelayMs: 250,
  });
  const [debouncedSearch, setDebouncedSearch] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(searchValue), debounceMs);
    return () => window.clearTimeout(timer);
  }, [debounceMs, searchValue]);

  useEffect(() => {
    try {
      window.localStorage.setItem(TRANSCRIPT_HISTORY_VIEW_MODE_STORAGE_KEY, viewMode);
    } catch {
      // URL state remains authoritative when persistent storage is unavailable.
    }
  }, [viewMode]);

  return {
    debouncedSearch,
    searchValue,
    setSearchValue,
    setViewMode,
    viewMode,
  };
}

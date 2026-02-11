import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSharedWebSocket } from "@/contexts/WebSocketContext";

type TranscriptType = "mic" | "file" | "youtube";

interface UseTranscriptAutoRefreshOptions {
  type?: TranscriptType;
  transcriptId?: string;
  queryKey?: readonly unknown[];
  debounceMs?: number;
  onError?: (message: string) => void;
}

export function useTranscriptAutoRefresh({
  type,
  transcriptId,
  queryKey,
  debounceMs = 250,
  onError,
}: UseTranscriptAutoRefreshOptions = {}) {
  const queryClient = useQueryClient();
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshNow = useCallback(() => {
    if (transcriptId) {
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts", transcriptId] });
      return;
    }

    if (queryKey) {
      queryClient.invalidateQueries({ queryKey: [...queryKey] });
      return;
    }

    queryClient.invalidateQueries({
      predicate: (query) => {
        if (query.queryKey[0] !== "/api/transcripts") {
          return false;
        }
        if (!type) {
          return true;
        }

        const scope = query.queryKey[1];
        if (typeof scope !== "object" || scope === null) {
          return false;
        }
        return (scope as { type?: string }).type === type;
      },
    });
  }, [queryClient, queryKey, transcriptId, type]);

  const handleWsMessage = useCallback((msg: any) => {
    if (!msg || typeof msg !== "object") {
      return;
    }

    if (msg.type === "history_updated") {
      if (refreshTimerRef.current) {
        return;
      }
      refreshTimerRef.current = setTimeout(() => {
        refreshNow();
        refreshTimerRef.current = null;
      }, debounceMs);
      return;
    }

    if (msg.type === "error" && onError) {
      onError(msg.message || "An error occurred.");
    }
  }, [debounceMs, onError, refreshNow]);

  const { isConnected, send } = useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    };
  }, []);

  return {
    isWsConnected: isConnected,
    send,
    refreshNow,
  };
}

import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

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
  debounceMs,
  onError,
}: UseTranscriptAutoRefreshOptions = {}) {
  const queryClient = useQueryClient();
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queryScope = queryKey?.[1];
  const queryType =
    typeof queryScope === "object" && queryScope !== null && "type" in queryScope
      ? String((queryScope as { type?: unknown }).type || "")
      : "";
  const effectiveType = type || (queryType === "mic" || queryType === "file" || queryType === "youtube" ? queryType : undefined);
  const effectiveDebounceMs = debounceMs ?? (transcriptId ? 750 : 250);

  const refreshNow = useCallback(() => {
    if (transcriptId) {
      const detailKey = ["/api/transcripts", transcriptId] as const;
      queryClient.invalidateQueries({ queryKey: detailKey, exact: true });
      void queryClient.refetchQueries({ queryKey: detailKey, exact: true, type: "active" });
      return;
    }

    if (queryKey) {
      const exactKey = [...queryKey];
      queryClient.invalidateQueries({ queryKey: exactKey, exact: true });
      void queryClient.refetchQueries({ queryKey: exactKey, exact: true, type: "active" });
      return;
    }

    queryClient.invalidateQueries({
      predicate: (query) => {
        if (query.queryKey[0] !== "/api/transcripts") {
          return false;
        }
        if (!effectiveType) {
          return true;
        }

        const scope = query.queryKey[1];
        if (typeof scope !== "object" || scope === null) {
          return false;
        }
        return (scope as { type?: string }).type === effectiveType;
      },
    });
  }, [effectiveType, queryClient, queryKey, transcriptId]);

  const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    if (!msg || typeof msg !== "object") {
      return;
    }

    if (msg.type === "history_updated") {
      if (transcriptId && msg.transcriptId && msg.transcriptId !== transcriptId) {
        return;
      }
      if (!transcriptId && effectiveType && msg.transcriptType && msg.transcriptType !== effectiveType) {
        return;
      }
      if (refreshTimerRef.current) {
        return;
      }
      refreshTimerRef.current = setTimeout(() => {
        refreshNow();
        refreshTimerRef.current = null;
      }, effectiveDebounceMs);
      return;
    }

    if (msg.type === "error" && onError) {
      onError(msg.message || "An error occurred.");
    }
  }, [effectiveDebounceMs, effectiveType, onError, refreshNow, transcriptId]);

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

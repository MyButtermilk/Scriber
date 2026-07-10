import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";

type TranscriptType = "mic" | "file" | "youtube";

interface UseTranscriptAutoRefreshOptions {
  type?: TranscriptType;
  transcriptId?: string;
  queryKey?: readonly unknown[];
  onError?: (message: string) => void;
}

export function useTranscriptAutoRefresh({
  type,
  transcriptId,
  queryKey,
  onError,
}: UseTranscriptAutoRefreshOptions = {}) {
  const queryClient = useQueryClient();
  const queryScope = queryKey?.[1];
  const queryType =
    typeof queryScope === "object" && queryScope !== null && "type" in queryScope
      ? String((queryScope as { type?: unknown }).type || "")
      : "";
  const effectiveType = type || (queryType === "mic" || queryType === "file" || queryType === "youtube" ? queryType : undefined);
  const refreshNow = useCallback(() => {
    if (transcriptId) {
      const detailKey = ["/api/transcripts", transcriptId] as const;
      queryClient.invalidateQueries({ queryKey: detailKey, exact: true });
      return;
    }

    if (queryKey) {
      const exactKey = [...queryKey];
      queryClient.invalidateQueries({ queryKey: exactKey, exact: true });
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

    if (msg.type === "error" && onError) {
      onError(msg.message || "An error occurred.");
    }
  }, [onError]);

  const { isConnected, send } = useSharedWebSocket(handleWsMessage);

  return {
    isWsConnected: isConnected,
    send,
    refreshNow,
  };
}

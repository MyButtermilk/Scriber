import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWebSocketContext } from "@/contexts/WebSocketContext";

type TranscriptType = "mic" | "file" | "youtube";

interface UseTranscriptAutoRefreshOptions {
  type?: TranscriptType;
  transcriptId?: string;
  queryKey?: readonly unknown[];
}

export function useTranscriptAutoRefresh({
  type,
  transcriptId,
  queryKey,
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

  // The app-global recording-error toast is owned by RecordingErrorToastBridge.
  // Transcript pages only need connection state and explicit refresh support.
  const { isConnected, send } = useWebSocketContext();

  return {
    isWsConnected: isConnected,
    send,
    refreshNow,
  };
}

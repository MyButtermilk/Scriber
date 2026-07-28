import type { ReactNode } from "react";

import { QueryErrorState } from "@/components/ui/query-error-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import type { TranscriptHistoryViewMode } from "@/hooks/use-transcript-history-panel-state";
import { cn } from "@/lib/utils";

interface TranscriptHistoryPanelProps {
  children: ReactNode;
  className?: string;
  emptyState: ReactNode;
  errorDescription: string;
  errorTitle: string;
  isEmpty: boolean;
  isError: boolean;
  isLoading: boolean;
  noMatchesState: ReactNode;
  onRetry: () => void;
  searchTerm: string;
  viewMode: TranscriptHistoryViewMode;
}

export function TranscriptHistoryPanel({
  children,
  className,
  emptyState,
  errorDescription,
  errorTitle,
  isEmpty,
  isError,
  isLoading,
  noMatchesState,
  onRetry,
  searchTerm,
  viewMode,
}: TranscriptHistoryPanelProps) {
  return (
    <div className={cn("min-w-0", className)}>
      {isLoading ? (
        <SkeletonList count={3} variant={viewMode} />
      ) : isError ? (
        <QueryErrorState title={errorTitle} description={errorDescription} onRetry={onRetry} />
      ) : isEmpty ? (
        searchTerm ? (
          noMatchesState
        ) : (
          emptyState
        )
      ) : (
        children
      )}
    </div>
  );
}

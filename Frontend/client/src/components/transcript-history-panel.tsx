import type { ReactNode } from "react";

import { QueryErrorState } from "@/components/ui/query-error-state";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";
import { useI18n } from "@/i18n";
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
}: TranscriptHistoryPanelProps) {
  const { t } = useI18n();
  return (
    <div className={cn("min-w-0", className)}>
      {isLoading ? (
        <div className="flex min-h-32 items-center justify-center">
          <WavePhysicsLoader size="panel" label={t("Loading transcripts")} />
        </div>
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

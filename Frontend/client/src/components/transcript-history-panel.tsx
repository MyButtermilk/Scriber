import { useLayoutEffect, useRef, type ReactNode } from "react";

import { QueryErrorState } from "@/components/ui/query-error-state";
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
  viewMode,
}: TranscriptHistoryPanelProps) {
  const { t } = useI18n();
  const revealRef = useRef<HTMLDivElement>(null);
  const skeletonRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const reveal = revealRef.current;
    const skeleton = skeletonRef.current;
    if (!reveal || !skeleton) return;

    if (isLoading) {
      reveal.classList.add("is-resetting");
      reveal.classList.remove("is-revealed");
      skeleton.classList.remove("is-pulsing");
      void skeleton.offsetWidth;
      reveal.classList.remove("is-resetting");
      skeleton.classList.add("is-pulsing");
      return;
    }
    reveal.classList.add("is-revealed");
  }, [isLoading]);

  const content = isLoading ? null : isError ? (
    <QueryErrorState title={errorTitle} description={errorDescription} onRetry={onRetry} />
  ) : isEmpty ? (
    searchTerm ? (
      noMatchesState
    ) : (
      emptyState
    )
  ) : (
    children
  );

  return (
    <div
      ref={revealRef}
      className={cn("t-skel transcript-history-reveal min-w-0", viewMode === "grid" && "is-grid", className)}
      aria-busy={isLoading}
    >
      <div
        ref={skeletonRef}
        className="t-skel-skeleton is-pulsing"
        role={isLoading ? "status" : undefined}
        aria-label={isLoading ? t("Loading transcripts") : undefined}
        aria-hidden={isLoading ? undefined : true}
      >
        <div className={cn("transcript-history-skeleton", viewMode === "grid" && "is-grid")}>
          {Array.from({ length: viewMode === "grid" ? 4 : 3 }, (_, index) => (
            <div className="transcript-history-skeleton-item" key={index}>
              <span className="transcript-history-skeleton-mark" />
              <span className="transcript-history-skeleton-lines">
                <span />
                <span />
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="t-skel-content" aria-hidden={isLoading} inert={isLoading}>
        {content}
      </div>
    </div>
  );
}

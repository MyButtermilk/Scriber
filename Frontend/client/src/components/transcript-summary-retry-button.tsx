import type { MouseEvent } from "react";
import { useState } from "react";
import { Loader2, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

interface TranscriptSummaryRetryButtonProps {
  transcriptId: string;
  transcriptTitle: string;
  onComplete?: (transcriptId: string) => void;
  className?: string;
}

export function TranscriptSummaryRetryButton({
  transcriptId,
  transcriptTitle,
  onComplete,
  className,
}: TranscriptSummaryRetryButtonProps) {
  const { t } = useI18n();
  const [isRetrying, setIsRetrying] = useState(false);
  const { toast } = useToast();

  const retrySummary = async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (isRetrying) return;

    setIsRetrying(true);
    try {
      const response = await fetchWithTimeout(
        apiUrl(`/api/transcripts/${transcriptId}/summarize`),
        {
          method: "POST",
          credentials: "include",
        },
        15 * 60_000,
      );
      if (!response.ok) {
        throw new Error(await responseErrorMessage(response));
      }

      toast({
        title: t("Summary ready"),
        description: t("A new summary for “{{title}}” is ready.", { title: transcriptTitle }),
        duration: 3000,
      });
    } catch (error) {
      toast({
        title: t("Summary retry failed"),
        description: friendlyError(error, t("Scriber could not create the summary. Please try again.")),
        variant: "destructive",
        duration: 5000,
      });
    } finally {
      onComplete?.(transcriptId);
      setIsRetrying(false);
    }
  };

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn(
        "min-h-7 gap-1.5 rounded-full border-red-200 bg-red-50/95 px-2.5 text-[10px] font-semibold text-red-700 dark:border-red-800 dark:bg-red-950/75 dark:text-red-300",
        className,
      )}
      onClick={retrySummary}
      disabled={isRetrying}
      aria-busy={isRetrying}
      aria-live="polite"
      aria-label={isRetrying
        ? t("Retrying summary for {{title}}", { title: transcriptTitle })
        : t("Retry summary for {{title}}", { title: transcriptTitle })}
      title={isRetrying ? t("Creating a new summary") : t("Summary failed. Try again")}
    >
      {isRetrying ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
      ) : (
        <RotateCcw className="h-3 w-3" aria-hidden="true" />
      )}
      <span>{isRetrying ? t("Retrying…") : t("Retry summary")}</span>
    </Button>
  );
}

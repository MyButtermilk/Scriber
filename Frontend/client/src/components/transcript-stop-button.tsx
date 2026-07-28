import { useState } from "react";
import { Loader2, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useI18n } from "@/i18n";

export function TranscriptStopButton({
  transcriptId,
  onStop,
  compact = false,
}: {
  transcriptId: string;
  onStop: () => void;
  compact?: boolean;
}) {
  const [isStopping, setIsStopping] = useState(false);
  const { toast } = useToast();
  const { t } = useI18n();

  const handleStop = async () => {
    if (isStopping) return;
    setIsStopping(true);
    try {
      const res = await fetchWithTimeout(
        apiUrl(`/api/transcripts/${transcriptId}/cancel`),
        {
          method: "POST",
          credentials: "include",
        },
        15_000,
      );
      if (!res.ok) throw new Error(t("Failed to stop"));

      toast({ title: t("Stopping…"), description: t("Task cancellation requested.") });
      onStop();
    } catch {
      toast({ title: t("Error"), description: t("Failed to stop task."), variant: "destructive" });
      setIsStopping(false);
    }
  };

  return (
    <Button
      size="sm"
      variant="destructive"
      onClick={handleStop}
      disabled={isStopping}
      type="button"
      className={compact ? "h-9 gap-1.5 px-2.5" : undefined}
      aria-label={t("Stop")}
    >
      {isStopping ? (
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
      ) : (
        <Square className="h-3 w-3 fill-current" aria-hidden="true" />
      )}
      {t("Stop")}
    </Button>
  );
}

import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/i18n";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";

export function FileTranscriptResumeButton({ transcriptId }: { transcriptId: string }) {
  const { t } = useI18n();
  const { toast } = useToast();
  const client = useQueryClient();
  const busy = useRef(false);
  const [state, setState] = useState<"idle" | "pending" | "queued">("idle");

  const resume = async () => {
    if (busy.current || state === "queued") return;
    busy.current = true;
    setState("pending");
    try {
      const response = await fetchWithTimeout(
        apiUrl(`/api/transcripts/${encodeURIComponent(transcriptId)}/resume-file`),
        { method: "POST", credentials: "include" },
        60_000,
      );
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      setState("queued");
      toast({
        title: t("Transcription resumed"),
        description: t("Continuing from saved progress. Completed parts will be reused."),
      });
    } catch (error) {
      setState("idle");
      toast({
        title: t("Could not resume transcription"),
        description: friendlyError(error, t("Failed to resume transcription.")),
        variant: "destructive",
      });
    } finally {
      busy.current = false;
      void client.invalidateQueries({ queryKey: ["/api/transcripts"] });
    }
  };

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="gap-1.5"
      onClick={() => void resume()}
      disabled={state !== "idle"}
      aria-busy={state === "pending"}
    >
      <Play className="h-3.5 w-3.5" aria-hidden="true" />
      {state === "queued" ? t("Queued") : state === "pending" ? t("Resuming...") : t("Resume transcription")}
    </Button>
  );
}

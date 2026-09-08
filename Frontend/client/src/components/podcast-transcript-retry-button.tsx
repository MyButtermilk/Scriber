import { useRef, useState, type MouseEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/i18n";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { responseErrorMessage } from "@/lib/request-errors";

export function PodcastTranscriptRetryButton({ transcriptId }: { transcriptId: string }) {
  const { t } = useI18n();
  const { toast } = useToast();
  const client = useQueryClient();
  const busy = useRef(false);
  const [state, setState] = useState<"idle" | "pending" | "queued">("idle");
  const { data } = useQuery<{ episode: { id: string; status: string } | null }>({
    queryKey: ["/api/podcasts/transcripts", transcriptId],
    queryFn: async () => {
      const response = await fetchWithTimeout(apiUrl(`/api/podcasts/transcripts/${transcriptId}`), {
        credentials: "include",
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      return response.json();
    },
    staleTime: 10_000,
  });

  if (!data?.episode || (data.episode.status !== "failed" && state === "idle")) return null;

  const retry = async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (busy.current || state === "queued") return;
    busy.current = true;
    setState("pending");
    try {
      const response = await fetchWithTimeout(apiUrl(`/api/podcasts/episodes/${data.episode!.id}/queue`), {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      setState("queued");
      toast({ title: t("Podcast retry queued"), description: t("The new attempt will appear in File history.") });
      void client.invalidateQueries({ queryKey: ["/api/transcripts"] });
      void client.invalidateQueries({ queryKey: ["/api/podcasts"] });
    } catch {
      setState("idle");
      toast({ title: t("Podcast retry failed"), description: t("Please try again."), variant: "destructive" });
    } finally {
      busy.current = false;
    }
  };

  return (
    <Button
      variant="outline"
      size="sm"
      className="gap-1.5 rounded-full"
      onClick={retry}
      disabled={state !== "idle"}
      aria-busy={state === "pending"}
      aria-label={state === "idle" ? t("Retry transcription") : undefined}
    >
      <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
      {state === "queued" ? t("Queued") : state === "pending" ? t("Retrying…") : t("Retry")}
    </Button>
  );
}

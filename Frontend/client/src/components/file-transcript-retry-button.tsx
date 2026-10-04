import { useRef, useState, type ChangeEvent, type MouseEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { useLocation } from "wouter";
import { Button } from "@/components/ui/button";
import { FileTranscriptResumeButton } from "@/components/file-transcript-resume-button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/i18n";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { friendlyError, responseErrorMessage } from "@/lib/request-errors";
import { startFileUploadBatch } from "@/lib/file-upload-store";
import type { TranscriptDetailResponse } from "@/lib/api-types";

export function FileTranscriptRetryButton({
  transcriptId,
  compact = false,
  resumeAvailable,
}: {
  transcriptId: string;
  compact?: boolean;
  resumeAvailable?: boolean;
}) {
  const { t } = useI18n();
  const { toast } = useToast();
  const client = useQueryClient();
  const [, setLocation] = useLocation();
  const busy = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [chooseFileOpen, setChooseFileOpen] = useState(false);
  const [state, setState] = useState<"idle" | "pending" | "queued">("idle");
  // History metadata does not include checkpoint eligibility. Share the detail
  // cache and fail closed before offering a fresh, potentially paid upload.
  const detailQuery = useQuery<TranscriptDetailResponse>({
    queryKey: ["/api/transcripts", transcriptId],
    enabled: resumeAvailable === undefined,
    queryFn: async () => {
      const response = await fetchWithTimeout(apiUrl(`/api/transcripts/${encodeURIComponent(transcriptId)}`), {
        credentials: "include",
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      return response.json();
    },
    staleTime: 10_000,
  });
  const canResume = resumeAvailable ?? detailQuery.data?.resumeAvailable;
  const podcastQuery = useQuery<{ episode: { id: string; status: string } | null }>({
    queryKey: ["/api/podcasts/transcripts", transcriptId],
    enabled: canResume !== true,
    queryFn: async () => {
      const response = await fetchWithTimeout(apiUrl(`/api/podcasts/transcripts/${transcriptId}`), {
        credentials: "include",
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      return response.json();
    },
    staleTime: 10_000,
  });

  const episodeAlreadyQueued = Boolean(podcastQuery.data?.episode && podcastQuery.data.episode.status !== "failed");

  const retry = async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (busy.current || state === "queued" || episodeAlreadyQueued) return;
    busy.current = true;
    setState("pending");
    try {
      if (resumeAvailable === undefined) {
        const detail = detailQuery.data ? { data: detailQuery.data, error: null } : await detailQuery.refetch();
        if (detail.error || !detail.data) throw detail.error || new Error(t("Could not restart transcription."));
        if (detail.data.resumeAvailable === true) return;
      }
      // A failed lookup must not misclassify a podcast as an uploaded file.
      const lookup = podcastQuery.data ? { data: podcastQuery.data, error: null } : await podcastQuery.refetch();
      if (lookup.error || !lookup.data) throw lookup.error || new Error(t("Could not restart transcription."));
      const episode = lookup.data.episode;
      if (!episode) {
        setChooseFileOpen(true);
        return;
      }
      if (episode.status !== "failed") return;
      const response = await fetchWithTimeout(apiUrl(`/api/podcasts/episodes/${episode.id}/queue`), {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response));
      setState("queued");
      toast({ title: t("Podcast retry queued"), description: t("The new attempt will appear in File history.") });
      void client.invalidateQueries({ queryKey: ["/api/transcripts"] });
      void client.invalidateQueries({ queryKey: ["/api/podcasts"] });
    } catch (error) {
      toast({
        title: t("Retry failed"),
        description: t(friendlyError(error, t("Could not restart transcription."))),
        variant: "destructive",
      });
    } finally {
      busy.current = false;
      setState((current) => (current === "queued" ? current : "idle"));
    }
  };

  const uploadFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || busy.current) return;
    const originPath = window.location.pathname;
    busy.current = true;
    setState("pending");
    try {
      const result = await startFileUploadBatch([file], {
        getServerProcessingText: (selected) => ({ key: "Preparing {{file}}…", values: { file: selected.name } }),
      });
      if (result.failures.length) throw new Error(result.failures[0].error);
      const transcript = result.responses[0];
      if (!transcript?.id) throw new Error(t("Retry started, but no transcript ID was returned."));
      void client.invalidateQueries({ queryKey: ["/api/transcripts"] });
      toast({ title: t("Retry started"), description: t("A new file transcription attempt has been queued.") });
      setChooseFileOpen(false);
      if (window.location.pathname === originPath) setLocation(`/transcript/${transcript.id}`);
    } catch (error) {
      toast({
        title: t("Retry failed"),
        description: t(friendlyError(error, t("Could not restart transcription."))),
        variant: "destructive",
      });
    } finally {
      busy.current = false;
      setState("idle");
    }
  };

  if (canResume === true) {
    return (
      <div className="contents" onClick={(event) => event.stopPropagation()}>
        <FileTranscriptResumeButton key={transcriptId} transcriptId={transcriptId} />
      </div>
    );
  }

  const isLookupPending = podcastQuery.isPending || (resumeAvailable === undefined && detailQuery.isPending);

  return (
    <div className="contents" onClick={(event) => event.stopPropagation()}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="min-w-0 max-w-full gap-1.5 rounded-full"
        onClick={retry}
        disabled={state !== "idle" || isLookupPending || episodeAlreadyQueued}
        aria-busy={state === "pending" || isLookupPending}
        aria-label={state === "queued" || episodeAlreadyQueued ? t("Queued") : t("Retry transcription")}
        data-transcript-retry={transcriptId}
      >
        <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
        <span className="min-w-0 truncate">
          {state === "queued" || episodeAlreadyQueued
            ? t("Queued")
            : state === "pending"
              ? t("Retrying…")
              : compact
                ? t("Retry")
                : t("Retry transcription")}
        </span>
      </Button>
      <input
        ref={fileInput}
        type="file"
        className="hidden"
        accept=".mp3,.m4a,.wav,.ogg,.flac,.aac,.mp4,.mov,.webm,.avi,.mkv,.m4v,.flv,.wmv"
        aria-label={t("Original audio or video file")}
        onChange={(event) => void uploadFile(event)}
      />
      <Dialog open={chooseFileOpen} onOpenChange={setChooseFileOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("Retry transcription")}</DialogTitle>
            <DialogDescription>
              {t(
                "Choose the original audio or video file again to start a new attempt. Automatic summaries follow your settings.",
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setChooseFileOpen(false)}>
              {t("Cancel")}
            </Button>
            <Button type="button" disabled={state === "pending"} onClick={() => fileInput.current?.click()}>
              {state === "pending" ? t("Preparing files") : t("Choose file")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
